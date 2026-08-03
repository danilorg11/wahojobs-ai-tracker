"""Dormant durable browser-session authentication for persistent profiles."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
import hashlib
import hmac
import json
import re
import sqlite3

from wahojobs.account_reconciliation import (
    attest_account_schema,
    authoritative_account_row_valid,
    authoritative_auth_identity_row_valid,
    authoritative_session_row_valid,
)
from wahojobs.ownership import validate_environment_namespace
from wahojobs.persistent_profiles_application import BrowserRequestContext


SESSION_COOKIE_NAME = "wahojobs_session"
MAX_COOKIE_HEADER_BYTES = 4096
MAX_BROWSER_HEADER_COUNT = 64
MAX_AUTHENTICATION_IDENTITIES = 16
MAX_SESSION_ROTATION_DEPTH = 32

_SESSION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ACTOR_KEY = "durable-browser-session"


class BrowserSessionAuthenticationUnavailable(Exception):
    """Sanitized infrastructure failure with no durable-state detail."""

    def __init__(self):
        super().__init__("Browser authentication is temporarily unavailable.")


class _OpaqueSessionCredential:
    __slots__ = ("_value",)

    def __init__(self, value):
        object.__setattr__(self, "_value", value)

    def consume(self):
        value = self._value
        object.__setattr__(self, "_value", None)
        return value

    def __repr__(self):
        return "_OpaqueSessionCredential(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("session_credential_not_serializable")

    def __copy__(self):
        raise TypeError("session_credential_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("session_credential_not_copyable")


class DurableBrowserSessionAuthenticationGateway:
    """Authenticate one opaque cookie against attested Migration-002 state."""

    __slots__ = ("_trusted_environment", "_clock")

    def __init__(self, *, trusted_environment_namespace, clock):
        invalid_environment = False
        try:
            validate_environment_namespace(trusted_environment_namespace)
        except Exception:
            invalid_environment = True
        if invalid_environment:
            raise ValueError("invalid_browser_session_authentication_configuration")
        if not callable(clock):
            raise ValueError("invalid_browser_session_authentication_configuration")
        self._trusted_environment = trusted_environment_namespace
        self._clock = clock

    def authenticate_browser_request(
        self,
        connection: sqlite3.Connection,
        request_context: BrowserRequestContext,
        *,
        now=None,
    ):
        failed = False
        result = None
        try:
            result = self._authenticate(
                connection,
                request_context,
                now=now,
            )
        except Exception:
            failed = True
        if failed:
            raise BrowserSessionAuthenticationUnavailable()
        return result

    def _authenticate(self, connection, request_context, *, now):
        from wahojobs.authenticated_profile_matches import (
            DurableMatchesRequestContext,
        )
        from wahojobs.persistent_profile_creation import (
            ProfileCreateRequestContext,
        )
        from wahojobs.persistent_profile_corrections import (
            ProfileCorrectionRequestContext,
        )

        if (
            not isinstance(connection, sqlite3.Connection)
            or type(request_context)
            not in {
                BrowserRequestContext,
                DurableMatchesRequestContext,
                ProfileCreateRequestContext,
                ProfileCorrectionRequestContext,
            }
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0] != 1
            or not attest_account_schema(connection)
        ):
            raise BrowserSessionAuthenticationUnavailable()

        credential = _extract_session_credential(
            request_context.authentication_input_for_gateway()
        )
        if credential is None:
            return None
        raw_token = credential.consume()
        credential = None
        try:
            digest = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        finally:
            raw_token = None

        session_rows = _rows(
            connection,
            "SELECT session_id, user_id, token_hash, token_hash_version, "
            "csrf_secret_hash, csrf_hash_version, created_at, last_seen_at, "
            "idle_expires_at, absolute_expires_at, rotated_at, revoked_at, "
            "revoke_reason, session_version, creation_idempotency_key, "
            "request_fingerprint FROM account_sessions WHERE token_hash = ? "
            "ORDER BY session_id LIMIT 2",
            (digest,),
        )
        if not session_rows:
            digest = None
            return None
        if len(session_rows) != 1:
            digest = None
            raise BrowserSessionAuthenticationUnavailable()
        session = session_rows[0]
        if not hmac.compare_digest(session.get("token_hash", ""), digest):
            digest = None
            return None
        digest = None
        if not authoritative_session_row_valid(session):
            raise BrowserSessionAuthenticationUnavailable()

        current_time = _trusted_now(now if now is not None else self._clock())
        created = _timestamp(session["created_at"])
        last_seen = _timestamp(session["last_seen_at"])
        idle_expiry = _timestamp(session["idle_expires_at"])
        absolute_expiry = _timestamp(session["absolute_expires_at"])
        if not _rotation_relationship_valid(
            connection,
            session,
            current_time=current_time,
        ):
            raise BrowserSessionAuthenticationUnavailable()
        if current_time < created:
            return None
        if last_seen > current_time:
            raise BrowserSessionAuthenticationUnavailable()
        if any(
            value is not None and _timestamp(value) > current_time
            for value in (session["rotated_at"], session["revoked_at"])
        ):
            raise BrowserSessionAuthenticationUnavailable()
        if (
            idle_expiry <= current_time
            or absolute_expiry <= current_time
            or session["revoked_at"] is not None
            or session["rotated_at"] is not None
        ):
            return None

        account_rows = _rows(
            connection,
            "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
            "deletion_requested_at, deactivated_at FROM users WHERE user_id = ? LIMIT 2",
            (session["user_id"],),
        )
        if len(account_rows) != 1:
            raise BrowserSessionAuthenticationUnavailable()
        account = account_rows[0]
        if not authoritative_account_row_valid(
            account,
            expected_user_id=session["user_id"],
        ):
            raise BrowserSessionAuthenticationUnavailable()
        if (
            _timestamp(session["created_at"]) < _timestamp(account["created_at"])
            or _timestamp(account["updated_at"]) > current_time
            or any(
                value is not None and _timestamp(value) > current_time
                for value in (
                    account["deletion_requested_at"],
                    account["deactivated_at"],
                )
            )
        ):
            raise BrowserSessionAuthenticationUnavailable()
        if account["lifecycle_status"] != "active":
            return None

        identities = _rows(
            connection,
            "SELECT auth_identity_id, user_id, provider, provider_subject, "
            "verified_email, email_verified, created_at, last_authenticated_at, "
            "disabled_at, link_idempotency_key, request_fingerprint "
            "FROM auth_identities WHERE user_id = ? "
            "ORDER BY auth_identity_id LIMIT ?",
            (session["user_id"], MAX_AUTHENTICATION_IDENTITIES + 1),
        )
        if not identities or len(identities) > MAX_AUTHENTICATION_IDENTITIES:
            raise BrowserSessionAuthenticationUnavailable()
        if any(
            not authoritative_auth_identity_row_valid(
                row,
                expected_user_id=session["user_id"],
                account_created_at=account["created_at"],
            )
            or _timestamp(row["last_authenticated_at"]) < _timestamp(row["created_at"])
            or _timestamp(row["last_authenticated_at"]) > current_time
            or (
                row["disabled_at"] is not None
                and _timestamp(row["disabled_at"]) > current_time
            )
            for row in identities
        ):
            raise BrowserSessionAuthenticationUnavailable()
        supporting = [
            row
            for row in identities
            if row["disabled_at"] is None
            and _timestamp(row["created_at"]) <= _timestamp(session["created_at"])
        ]
        if not supporting:
            return None

        return _issue_actor(
            account_id=session["user_id"],
            environment_namespace=self._trusted_environment,
        )

    def __repr__(self):
        return "DurableBrowserSessionAuthenticationGateway(<configured>)"


def _extract_session_credential(authentication_input):
    if authentication_input is None:
        return None
    if isinstance(authentication_input, Message):
        try:
            authentication_input = tuple(authentication_input.raw_items())
        except Exception:
            return None
    if type(authentication_input) is not tuple or len(authentication_input) > MAX_BROWSER_HEADER_COUNT:
        return None
    cookie_headers = []
    for item in authentication_input:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            return None
        name, value = item
        if any(ord(char) < 32 or ord(char) == 127 for char in name + value):
            return None
        if name.casefold() == "cookie":
            cookie_headers.append(value)
    if len(cookie_headers) != 1:
        return None
    header = cookie_headers[0]
    try:
        if len(header.encode("utf-8")) > MAX_COOKIE_HEADER_BYTES:
            return None
    except UnicodeError:
        return None
    matches = []
    for index, segment in enumerate(header.split(";")):
        if index and segment.startswith(" "):
            segment = segment[1:]
        if not segment or "=" not in segment:
            return None
        name, value = segment.split("=", 1)
        if not name or not value:
            return None
        if name == SESSION_COOKIE_NAME:
            matches.append(value)
        elif name.strip(" \t") == SESSION_COOKIE_NAME:
            return None
    if len(matches) != 1 or _SESSION_TOKEN.fullmatch(matches[0]) is None:
        return None
    return _OpaqueSessionCredential(matches[0])


def _rotation_relationship_valid(connection, session, *, current_time):
    incoming = _rows(
        connection,
        "SELECT rotation_id, user_id, predecessor_session_id, replacement_session_id, "
        "rotated_at, created_at FROM account_session_rotations "
        "WHERE replacement_session_id = ? ORDER BY rotation_id LIMIT 2",
        (session["session_id"],),
    )
    outgoing = _rows(
        connection,
        "SELECT rotation_id, user_id, predecessor_session_id, replacement_session_id, "
        "rotated_at, created_at FROM account_session_rotations "
        "WHERE predecessor_session_id = ? ORDER BY rotation_id LIMIT 2",
        (session["session_id"],),
    )
    if len(incoming) > 1 or len(outgoing) > 1:
        return False
    if any(
        _timestamp(edge["created_at"]) > current_time
        for edge in incoming + outgoing
    ):
        return False
    if not _ancestor_lineage_valid(
        connection,
        session,
        depth=0,
        current_time=current_time,
    ):
        return False
    if incoming and not _rotation_edge_and_counterpart_valid(
        connection,
        incoming[0],
        session,
        incoming=True,
    ):
        return False
    if outgoing and not _rotation_edge_and_counterpart_valid(
        connection,
        outgoing[0],
        session,
        incoming=False,
    ):
        return False
    if session["rotated_at"] is None:
        return not outgoing
    return len(outgoing) == 1


def _ancestor_lineage_valid(connection, session, *, depth, current_time):
    if depth > MAX_SESSION_ROTATION_DEPTH:
        return False
    incoming = _rows(
        connection,
        "SELECT rotation_id, user_id, predecessor_session_id, replacement_session_id, "
        "rotated_at, created_at FROM account_session_rotations "
        "WHERE replacement_session_id = ? ORDER BY rotation_id LIMIT 2",
        (session["session_id"],),
    )
    if not incoming:
        return _initial_session_fingerprint_valid(session)
    if (
        len(incoming) != 1
        or depth == MAX_SESSION_ROTATION_DEPTH
        or _timestamp(incoming[0]["created_at"]) > current_time
    ):
        return False
    edge = incoming[0]
    if not _rotation_edge_valid(edge, session, incoming=True):
        return False
    predecessor_rows = _session_rows_by_id(
        connection,
        edge["predecessor_session_id"],
    )
    if len(predecessor_rows) != 1:
        return False
    predecessor = predecessor_rows[0]
    if (
        not authoritative_session_row_valid(
            predecessor,
            expected_user_id=session["user_id"],
        )
        or _timestamp(predecessor["created_at"]) > _timestamp(session["created_at"])
        or predecessor["rotated_at"] != edge["rotated_at"]
        or predecessor["revoked_at"] != edge["rotated_at"]
        or predecessor["revoke_reason"] != "session_rotated"
        or not _replacement_session_fingerprint_valid(
            predecessor=predecessor,
            replacement=session,
        )
    ):
        return False
    return _ancestor_lineage_valid(
        connection,
        predecessor,
        depth=depth + 1,
        current_time=current_time,
    )


def _rotation_edge_and_counterpart_valid(connection, edge, session, *, incoming):
    if not _rotation_edge_valid(edge, session, incoming=incoming):
        return False
    counterpart_id = (
        edge["predecessor_session_id"]
        if incoming
        else edge["replacement_session_id"]
    )
    counterpart_rows = _session_rows_by_id(connection, counterpart_id)
    if len(counterpart_rows) != 1:
        return False
    counterpart = counterpart_rows[0]
    if not authoritative_session_row_valid(
        counterpart,
        expected_user_id=session["user_id"],
    ):
        return False
    counterpart_created = _timestamp(counterpart["created_at"])
    session_created = _timestamp(session["created_at"])
    if (
        (incoming and counterpart_created > session_created)
        or (not incoming and counterpart_created < session_created)
    ):
        return False
    if incoming:
        return (
            counterpart["rotated_at"] == edge["rotated_at"]
            and counterpart["revoked_at"] == edge["rotated_at"]
            and counterpart["revoke_reason"] == "session_rotated"
            and _replacement_session_fingerprint_valid(
                predecessor=counterpart,
                replacement=session,
            )
        )
    return (
        counterpart["rotated_at"] is None
        and counterpart["revoked_at"] is None
        and _timestamp(counterpart["created_at"]) <= _timestamp(edge["rotated_at"])
        and _replacement_session_fingerprint_valid(
            predecessor=session,
            replacement=counterpart,
        )
    )


def _initial_session_fingerprint_valid(session):
    created = _timestamp(session["created_at"])
    expected = _request_digest(
        {
            "user_id": session["user_id"],
            "idle_seconds": int(
                (_timestamp(session["idle_expires_at"]) - created).total_seconds()
            ),
            "absolute_seconds": int(
                (_timestamp(session["absolute_expires_at"]) - created).total_seconds()
            ),
        }
    )
    return hmac.compare_digest(session["request_fingerprint"], expected)


def _replacement_session_fingerprint_valid(*, predecessor, replacement):
    if predecessor["absolute_expires_at"] != replacement["absolute_expires_at"]:
        return False
    created = _timestamp(replacement["created_at"])
    idle = _timestamp(replacement["idle_expires_at"])
    absolute = _timestamp(replacement["absolute_expires_at"])
    if idle == absolute:
        # M002 does not persist the requested idle TTL when absolute expiry clamps it.
        return True
    expected_version = predecessor["session_version"] - 1
    if expected_version < 1:
        return False
    expected = _request_digest(
        {
            "old_token_digest": predecessor["token_hash"],
            "expected_session_version": expected_version,
            "idle_seconds": int((idle - created).total_seconds()),
        }
    )
    return hmac.compare_digest(replacement["request_fingerprint"], expected)


def _request_digest(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _session_rows_by_id(connection, session_id):
    return _rows(
        connection,
        "SELECT session_id, user_id, token_hash, token_hash_version, "
        "csrf_secret_hash, csrf_hash_version, created_at, last_seen_at, "
        "idle_expires_at, absolute_expires_at, rotated_at, revoked_at, "
        "revoke_reason, session_version, creation_idempotency_key, "
        "request_fingerprint FROM account_sessions WHERE session_id = ? LIMIT 2",
        (session_id,),
    )


def _rotation_edge_valid(edge, session, *, incoming):
    try:
        rotation_id = edge["rotation_id"]
        user_id = edge["user_id"]
        predecessor = edge["predecessor_session_id"]
        replacement = edge["replacement_session_id"]
        rotated = _timestamp(edge["rotated_at"])
        created = _timestamp(edge["created_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        type(rotation_id) is str
        and re.fullmatch(r"rot_[0-9a-f]{32}", rotation_id) is not None
        and user_id == session["user_id"]
        and type(predecessor) is str
        and re.fullmatch(r"ses_[0-9a-f]{32}", predecessor) is not None
        and type(replacement) is str
        and re.fullmatch(r"ses_[0-9a-f]{32}", replacement) is not None
        and predecessor != replacement
        and created >= rotated
        and (
            (incoming and replacement == session["session_id"])
            or (
                not incoming
                and predecessor == session["session_id"]
                and edge["rotated_at"] == session["rotated_at"]
            )
        )
    )


def _trusted_now(value):
    if type(value) is not datetime or value.tzinfo is None:
        raise BrowserSessionAuthenticationUnavailable()
    return value.astimezone(timezone.utc)


def _timestamp(value):
    if type(value) is not str or len(value) != 25:
        raise ValueError("invalid_timestamp")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+00:00")
    return parsed.replace(tzinfo=timezone.utc)


def _rows(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _issue_actor(*, account_id, environment_namespace):
    from wahojobs.persistent_profiles_application import (
        _TRUSTED_AUTHENTICATION_ACTOR_ISSUER,
    )

    return _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue(
        _ACTOR_KEY,
        account_id=account_id,
        environment_namespace=environment_namespace,
    )
