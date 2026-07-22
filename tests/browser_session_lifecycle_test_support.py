import contextlib
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import types
import weakref
from collections.abc import Mapping

from tests.accounts_test_support import create_user, install_accounts
from wahojobs.browser_session_lifecycle import (
    CreateBrowserSessionCommand,
    RequestScopedSessionSecretVault,
    RevokeBrowserSessionCommand,
    RotateBrowserSessionCommand,
    _ACCOUNT_ID,
    _COMMAND_ISSUANCE_CAPABILITY,
    _IDENTITY_ID,
    _RESPONSE_COMPOSITION_CAPABILITY,
    _REQUEST_SECRET_VAULT_CAPABILITY,
    _SESSION_ID,
    _trusted_time,
    _validated_expected_version,
    _validated_id,
    _validated_idempotency_key,
    _validated_revoke_reason,
    _validated_ttl,
)
from wahojobs.browser_session_lifecycle import (
    MAX_ABSOLUTE_TTL,
    MAX_IDLE_TTL,
    MIN_ABSOLUTE_TTL,
    MIN_IDLE_TTL,
    create_browser_session as _create_browser_session,
    close_request_scoped_secret_vault as _close_request_scoped_secret_vault,
    finalize_pending_issued_session as _finalize_pending_issued_session,
    revoke_browser_session as _revoke_browser_session,
    rotate_browser_session as _rotate_browser_session,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
_COMMAND_ACCEPTED_TIMES = weakref.WeakKeyDictionary()
_RESULT_VAULTS = weakref.WeakKeyDictionary()
_RESULT_VAULTS_LOCK = threading.Lock()


class _TestBrowserSessionCommandIssuer:
    def create(
        self,
        *,
        account_id,
        supporting_identity_id,
        idempotency_key,
        accepted_at,
        idle_ttl,
        absolute_ttl,
    ):
        command = CreateBrowserSessionCommand._issue(
            _COMMAND_ISSUANCE_CAPABILITY,
            account_id=_validated_id(account_id, _ACCOUNT_ID),
            supporting_identity_id=_validated_id(supporting_identity_id, _IDENTITY_ID),
            idempotency_key=_validated_idempotency_key(idempotency_key),
            accepted_at=_trusted_time(accepted_at),
            idle_ttl=_validated_ttl(
                idle_ttl,
                minimum=MIN_IDLE_TTL,
                maximum=MAX_IDLE_TTL,
            ),
            absolute_ttl=_validated_ttl(
                absolute_ttl,
                minimum=MIN_ABSOLUTE_TTL,
                maximum=MAX_ABSOLUTE_TTL,
            ),
        )
        _COMMAND_ACCEPTED_TIMES[command] = accepted_at
        return command

    def rotate(
        self,
        *,
        account_id,
        session_id,
        expected_session_version,
        idempotency_key,
        accepted_at,
        idle_ttl,
    ):
        command = RotateBrowserSessionCommand._issue(
            _COMMAND_ISSUANCE_CAPABILITY,
            account_id=_validated_id(account_id, _ACCOUNT_ID),
            session_id=_validated_id(session_id, _SESSION_ID),
            expected_session_version=_validated_expected_version(
                expected_session_version
            ),
            idempotency_key=_validated_idempotency_key(idempotency_key),
            accepted_at=_trusted_time(accepted_at),
            idle_ttl=_validated_ttl(
                idle_ttl,
                minimum=MIN_IDLE_TTL,
                maximum=MAX_IDLE_TTL,
            ),
        )
        _COMMAND_ACCEPTED_TIMES[command] = accepted_at
        return command

    def revoke(
        self,
        *,
        account_id,
        session_id,
        expected_session_version,
        accepted_at,
        reason,
    ):
        command = RevokeBrowserSessionCommand._issue(
            _COMMAND_ISSUANCE_CAPABILITY,
            account_id=_validated_id(account_id, _ACCOUNT_ID),
            session_id=_validated_id(session_id, _SESSION_ID),
            expected_session_version=_validated_expected_version(
                expected_session_version
            ),
            accepted_at=_trusted_time(accepted_at),
            reason=_validated_revoke_reason(reason),
        )
        _COMMAND_ACCEPTED_TIMES[command] = accepted_at
        return command


_TEST_COMMAND_ISSUER = _TestBrowserSessionCommandIssuer()


@contextlib.contextmanager
def lifecycle_database(*, suffix="lifecycle"):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{suffix}.sqlite"
        connection = install_accounts(path)
        try:
            _invitation, created = create_user(
                connection,
                suffix=suffix,
                now=NOW,
            )
            yield path, connection, created
        finally:
            connection.close()


def connect(path, *, timeout=2.0):
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_command(
    created,
    *,
    account_id=None,
    supporting_identity_id=None,
    key="browser-session-create-001",
    accepted_at=NOW,
    idle_ttl=timedelta(hours=1),
    absolute_ttl=timedelta(days=7),
):
    return _TEST_COMMAND_ISSUER.create(
        account_id=account_id or created.user.user_id,
        supporting_identity_id=supporting_identity_id or created.identity.auth_identity_id,
        idempotency_key=key,
        accepted_at=accepted_at,
        idle_ttl=idle_ttl,
        absolute_ttl=absolute_ttl,
    )


def rotate_command(
    account_id,
    session_id,
    *,
    key="browser-session-rotate-001",
    accepted_at=NOW + timedelta(minutes=5),
    idle_ttl=timedelta(hours=1),
    expected_session_version=1,
):
    return _TEST_COMMAND_ISSUER.rotate(
        account_id=account_id,
        session_id=session_id,
        expected_session_version=expected_session_version,
        idempotency_key=key,
        accepted_at=accepted_at,
        idle_ttl=idle_ttl,
    )


def revoke_command(
    account_id,
    session_id,
    *,
    accepted_at=NOW + timedelta(minutes=5),
    expected_session_version=1,
    reason="explicit_revoke",
):
    return _TEST_COMMAND_ISSUER.revoke(
        account_id=account_id,
        session_id=session_id,
        expected_session_version=expected_session_version,
        accepted_at=accepted_at,
        reason=reason,
    )


def session_row(connection, *, key=None, session_id=None):
    if key is not None:
        return connection.execute(
            "SELECT * FROM account_sessions WHERE creation_idempotency_key = ?",
            (key,),
        ).fetchone()
    return connection.execute(
        "SELECT * FROM account_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()


def token_from_cookie_header(header):
    assignment = header.split(";", 1)[0]
    name, token = assignment.split("=", 1)
    if name != "wahojobs_session":
        raise AssertionError("unexpected cookie name")
    return token


def create_browser_session(connection, command, **kwargs):
    kwargs.setdefault("_clock", lambda: _COMMAND_ACCEPTED_TIMES[command])
    vault = kwargs.pop("secret_vault", None)
    owned = vault is None
    if vault is None:
        vault = request_secret_vault()
    try:
        result = _create_browser_session(connection, command, vault, **kwargs)
    except BaseException:
        if owned:
            close_secret_vault(vault)
        raise
    with _RESULT_VAULTS_LOCK:
        _RESULT_VAULTS[result] = (vault, owned)
    return result


def rotate_browser_session(connection, command, **kwargs):
    kwargs.setdefault("_clock", lambda: _COMMAND_ACCEPTED_TIMES[command])
    vault = kwargs.pop("secret_vault", None)
    owned = vault is None
    if vault is None:
        vault = request_secret_vault()
    try:
        result = _rotate_browser_session(connection, command, vault, **kwargs)
    except BaseException:
        if owned:
            close_secret_vault(vault)
        raise
    with _RESULT_VAULTS_LOCK:
        _RESULT_VAULTS[result] = (vault, owned)
    return result


def revoke_browser_session(connection, command, **kwargs):
    kwargs.setdefault("_clock", lambda: _COMMAND_ACCEPTED_TIMES[command])
    return _revoke_browser_session(connection, command, **kwargs)


def request_secret_vault(*, max_entries=None, max_secret_bytes=None):
    kwargs = {}
    if max_entries is not None:
        kwargs["max_entries"] = max_entries
    if max_secret_bytes is not None:
        kwargs["max_secret_bytes"] = max_secret_bytes
    return RequestScopedSessionSecretVault._issue(
        _REQUEST_SECRET_VAULT_CAPABILITY,
        **kwargs,
    )


def vault_for_result(result):
    with _RESULT_VAULTS_LOCK:
        return _RESULT_VAULTS[result][0]


def vault_entry_count(vault):
    return vault._entry_count(_REQUEST_SECRET_VAULT_CAPABILITY)


def vault_is_closed_and_empty(vault):
    return vault._is_closed_and_empty(_REQUEST_SECRET_VAULT_CAPABILITY)


def corrupt_issuance_handle(result, handle):
    original = result._issuance_handle
    object.__setattr__(result, "_issuance_handle", handle)
    return original


def corrupt_effective_expiry(result, value):
    original = result._effective_expires_at
    object.__setattr__(result, "_effective_expires_at", value)
    return original


def close_secret_vault(vault, **kwargs):
    return _close_request_scoped_secret_vault(
        vault,
        _RESPONSE_COMPOSITION_CAPABILITY,
        **kwargs,
    )


def finalize_issued(connection, result, *, vault=None):
    vault = vault or vault_for_result(result)
    return _finalize_pending_issued_session(
        connection,
        result,
        vault,
        _RESPONSE_COMPOSITION_CAPABILITY,
    )


def consume_issued(result, *, now=NOW, vault=None, **kwargs):
    vault = vault or vault_for_result(result)
    response = result.consume_for_response(
        vault,
        _RESPONSE_COMPOSITION_CAPABILITY,
        now=now,
        **kwargs,
    )
    with _RESULT_VAULTS_LOCK:
        tracked = _RESULT_VAULTS.get(result)
    if tracked is not None and tracked[1]:
        close_secret_vault(vault)
    return response


def recursively_reachable_objects(root):
    """Walk ordinary instance, callable, closure, and container state safely."""
    pending = [root]
    seen = set()
    reached = []
    scalar_types = (str, bytes, bytearray, int, float, bool, type(None), datetime)
    while pending:
        value = pending.pop()
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        reached.append(value)
        if isinstance(value, scalar_types):
            continue
        if isinstance(value, Mapping):
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
            continue
        if isinstance(value, weakref.ReferenceType):
            target = value()
            if target is not None:
                pending.append(target)
            continue
        if isinstance(value, types.MethodType):
            pending.extend((value.__self__, value.__func__))
            continue
        if isinstance(value, types.FunctionType):
            pending.extend(value.__dict__.values())
            if value.__defaults__:
                pending.extend(value.__defaults__)
            if value.__kwdefaults__:
                pending.extend(value.__kwdefaults__.values())
            if value.__closure__:
                for cell in value.__closure__:
                    try:
                        pending.append(cell.cell_contents)
                    except ValueError:
                        pass
            continue
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, field.name) for field in dataclasses.fields(value))
        try:
            pending.extend(vars(value).values())
        except TypeError:
            pass
        for candidate_type in type(value).__mro__:
            slots = candidate_type.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot == "__weakref__":
                    continue
                attribute = (
                    f"_{candidate_type.__name__}{slot}"
                    if slot.startswith("__") and not slot.endswith("__")
                    else slot
                )
                try:
                    pending.append(getattr(value, attribute))
                except (AttributeError, TypeError):
                    pass
            for name, descriptor in candidate_type.__dict__.items():
                if name.startswith("__"):
                    continue
                if isinstance(descriptor, property) or isinstance(
                    descriptor,
                    (types.FunctionType, classmethod, staticmethod),
                ):
                    try:
                        pending.append(getattr(value, name))
                    except BaseException:
                        pass
    return reached
