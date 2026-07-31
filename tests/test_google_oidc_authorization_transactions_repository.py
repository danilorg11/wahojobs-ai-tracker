from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import FrameType, TracebackType
import unittest
from unittest import mock

import scripts.google_oidc_authorization_transactions_migration as migration_006
from tests.google_oidc_authorization_transactions_test_support import (
    ManualClock,
    NOW,
    authorization_parameters,
    durable_transaction_database,
    key_authority,
    make_real_gateway,
    reconstructed_gateway,
    sockets_blocked,
    transaction_rows,
)
from tests.persistent_profile_canonical_v2_test_support import (
    install_canonical_v2_profiles,
)
import wahojobs.google_oidc_authorization_transaction_repository as repository
import wahojobs.google_oidc_authorization_transactions as transaction_domain
import wahojobs.google_oidc_authorization_transaction_reconciliation as reconciliation
import wahojobs.google_oidc_authorization_transaction_schema as transaction_schema
import wahojobs.google_oidc_transaction_protection as transaction_protection
from wahojobs.google_oidc_authorization_transaction_schema import (
    attest_google_oidc_authorization_transaction_schema,
)
from wahojobs.google_oidc_authorization_transaction_repository import (
    GoogleOidcAuthorizationTransactionRepositoryError,
    claim_google_oidc_authorization_transaction,
    cleanup_google_oidc_authorization_transactions,
    prepare_google_oidc_authorization_transaction,
)
from wahojobs.google_oidc_authorization_transactions import (
    GOOGLE_OIDC_CLEANUP_CONTRACT,
)


def _reachable_failure_graph(root):
    pending = [root]
    visited = set()
    binary_values = set()
    text_values = set()
    while pending and len(visited) < 4096:
        value = pending.pop()
        identity = id(value)
        if identity in visited:
            continue
        visited.add(identity)
        if type(value) in {bytes, bytearray}:
            binary_values.add(bytes(value))
            continue
        if type(value) is str:
            text_values.add(value)
            continue
        if isinstance(value, BaseException):
            pending.extend(
                item
                for item in (
                    value.__traceback__,
                    value.__cause__,
                    value.__context__,
                    value.args,
                )
                if item is not None
            )
            try:
                pending.append(vars(value))
            except TypeError:
                pass
            continue
        if isinstance(value, TracebackType):
            pending.append(value.tb_frame)
            if value.tb_next is not None:
                pending.append(value.tb_next)
            continue
        if isinstance(value, FrameType):
            pending.extend(tuple(value.f_locals.values()))
            continue
        if type(value) is dict:
            pending.extend(tuple(value.keys()))
            pending.extend(tuple(value.values()))
            continue
        if type(value) in {tuple, list, set, frozenset}:
            pending.extend(tuple(value))
            continue
        if type(value) is sqlite3.Row:
            pending.extend(tuple(value))
            continue
        value_type = type(value)
        if value_type.__module__.startswith("wahojobs"):
            for owner in value_type.__mro__:
                slots = owner.__dict__.get("__slots__", ())
                if type(slots) is str:
                    slots = (slots,)
                for slot in slots:
                    if slot in {"__dict__", "__weakref__"}:
                        continue
                    try:
                        pending.append(object.__getattribute__(value, slot))
                    except (AttributeError, TypeError):
                        pass
    return visited, binary_values, text_values


def _chained_failure():
    try:
        raise ValueError("retained dependency cause")
    except ValueError as cause:
        try:
            raise RuntimeError("retained dependency failure") from cause
        except RuntimeError as failure:
            return failure, cause


def _execute_transaction_insert(connection, row, *, replace=False):
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    connection.execute(
        f"{verb} INTO google_oidc_authorization_transactions "
        f"({repository._SELECT_PROJECTION}) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(row[name] for name in repository._SELECT_COLUMNS),
    )


def _create_terminal_row(
    connection,
    gateway,
    authority,
    lifecycle,
    *,
    terminal_at=None,
):
    with sockets_blocked():
        prepared = prepare_google_oidc_authorization_transaction(
            connection,
            gateway,
            authority,
        )
    transaction_id = prepared.transaction_id
    row = next(
        item
        for item in transaction_rows(connection)
        if item["transaction_id"] == transaction_id
    )
    terminal_text = terminal_at
    if terminal_text is None:
        terminal_text = (
            row["expires_at"] if lifecycle == "expired" else row["created_at"]
        )
    elif type(terminal_text) is datetime:
        terminal_text = terminal_text.isoformat()
    claimed_text = terminal_text if lifecycle == "consumed" else None
    connection.execute(
        "UPDATE google_oidc_authorization_transactions "
        "SET lifecycle=?, claimed_at=?, terminal_at=?, row_version=2 "
        "WHERE transaction_id=? AND lifecycle='prepared' AND row_version=1",
        (lifecycle, claimed_text, terminal_text, transaction_id),
    )
    connection.commit()
    prepared.close()
    return next(
        item
        for item in transaction_rows(connection)
        if item["transaction_id"] == transaction_id
    )


def _mutate_rows_bypassing_update_guard(connection, mutations):
    trigger_name = (
        "trg_google_oidc_authorization_transactions_update_guard"
    )
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()[0]
    connection.execute(f'DROP TRIGGER "{trigger_name}"')
    connection.execute("PRAGMA ignore_check_constraints = ON")
    try:
        for sql, parameters in mutations:
            connection.execute(sql, parameters)
        connection.execute("PRAGMA ignore_check_constraints = OFF")
        connection.execute(trigger_sql)
        connection.commit()
    except BaseException:
        connection.execute("PRAGMA ignore_check_constraints = OFF")
        if (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema "
                "WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()[0]
            == 0
        ):
            connection.execute(trigger_sql)
        connection.rollback()
        raise


def _insert_valid_terminal_rows_without_insert_guard(connection, count):
    trigger_name = (
        "trg_google_oidc_authorization_transactions_insert_guard"
    )
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()[0]
    connection.execute(f'DROP TRIGGER "{trigger_name}"')
    created_at = NOW.isoformat()
    expires_at = (NOW + timedelta(seconds=600)).isoformat()
    rows = (
        (
            f"oidctx_{index:032x}",
            1,
            "google",
            "test",
            b"c" * 32,
            1,
            1,
            index.to_bytes(32, "big"),
            created_at,
            expires_at,
            "invalidated",
            None,
            created_at,
            2,
            1,
            11,
            index.to_bytes(12, "big"),
            index.to_bytes(32, "big"),
        )
        for index in range(1, count + 1)
    )
    try:
        connection.executemany(
            "INSERT INTO google_oidc_authorization_transactions VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.execute(trigger_sql)
        connection.commit()
    except BaseException:
        if (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema "
                "WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()[0]
            == 0
        ):
            connection.execute(trigger_sql)
        connection.rollback()
        raise


def _run_cleanup(
    connection,
    gateway,
    authority,
    *,
    limit,
    terminal_retention_seconds,
):
    with sockets_blocked():
        return cleanup_google_oidc_authorization_transactions(
            connection,
            gateway,
            authority,
            limit=limit,
            terminal_retention_seconds=terminal_retention_seconds,
        )


def _capture_prepare_failure(connection, gateway, authority):
    try:
        prepare_google_oidc_authorization_transaction(
            connection,
            gateway,
            authority,
        )
    except GoogleOidcAuthorizationTransactionRepositoryError as failure:
        connection = None
        gateway = None
        authority = None
        return failure
    raise AssertionError("repository_failure_required")


def _capture_claim_failure(connection, gateway, authority, state):
    try:
        claim_google_oidc_authorization_transaction(
            connection,
            gateway,
            authority,
            state,
        )
    except GoogleOidcAuthorizationTransactionRepositoryError as failure:
        connection = None
        gateway = None
        authority = None
        state = None
        return failure
    raise AssertionError("repository_failure_required")


def _capture_cleanup_failure(connection, gateway, authority):
    try:
        cleanup_google_oidc_authorization_transactions(
            connection,
            gateway,
            authority,
            limit=1,
            terminal_retention_seconds=1,
        )
    except GoogleOidcAuthorizationTransactionRepositoryError as failure:
        connection = None
        gateway = None
        authority = None
        return failure
    raise AssertionError("repository_failure_required")


class GoogleOidcAuthorizationTransactionRepositoryTests(unittest.TestCase):
    def assertSanitizedFailureGraph(
        self,
        failure,
        *,
        forbidden_objects=(),
        forbidden_binary=(),
        forbidden_text=(),
    ):
        self.assertIsNone(failure.__cause__)
        self.assertIsNone(failure.__context__)
        frames = []
        traceback = failure.__traceback__
        while traceback is not None:
            frames.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        self.assertFalse(
            any(name.endswith("_sensitive") for name in frames),
            frames,
        )
        identities, binary_values, text_values = _reachable_failure_graph(
            failure
        )
        self.assertLess(len(identities), 4096)
        for value in forbidden_objects:
            self.assertNotIn(
                id(value),
                identities,
                (type(value).__name__, repr(value)),
            )
        for value in forbidden_binary:
            self.assertNotIn(bytes(value), binary_values)
        for value in forbidden_text:
            self.assertNotIn(value, text_values)

    def test_preparation_commits_before_return_and_persists_only_protected_material(self):
        with durable_transaction_database(suffix="repository-prepare") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-repository-prepare"
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                rows = transaction_rows(database.connection)
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertFalse(database.connection.in_transaction)
                self.assertEqual(row["lifecycle"], "prepared")
                self.assertEqual(row["row_version"], 1)
                self.assertEqual(row["record_version"], 1)
                self.assertEqual(row["provider"], "google")
                self.assertEqual(len(row["configuration_fingerprint"]), 32)
                self.assertEqual(len(row["state_lookup_digest"]), 32)
                self.assertEqual(len(row["protection_nonce"]), 12)
                self.assertLessEqual(len(row["protected_material"]), 528)
                self.assertNotIn(state.encode("ascii"), row["protected_material"])
                self.assertNotIn(state, repr(row))
                self.assertEqual(
                    prepared.expires_at - prepared.created_at,
                    timedelta(seconds=600),
                )
                prepared.close()
            finally:
                harness.close()
                authority.close()

    def test_process_reconstructed_claim_is_one_use_and_terminal_before_decryption(self):
        with durable_transaction_database(suffix="repository-reconstruct") as database:
            authority = key_authority()
            first = reconstructed_gateway(
                subject="google-subject-repository-reconstruct"
            )
            second = None
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        first.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                first.close()
                second = reconstructed_gateway(
                    subject="google-subject-repository-reconstruct"
                )
                observed = {}
                original = repository._unprotect_material

                def observe(*args, **kwargs):
                    observed["in_transaction"] = database.connection.in_transaction
                    observed["lifecycle"] = transaction_rows(
                        database.connection
                    )[0]["lifecycle"]
                    return original(*args, **kwargs)

                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_unprotect_material",
                    side_effect=observe,
                ):
                    claimed = claim_google_oidc_authorization_transaction(
                        database.connection,
                        second.gateway,
                        authority,
                        state,
                    )
                self.assertEqual(
                    observed,
                    {"in_transaction": False, "lifecycle": "consumed"},
                )
                self.assertTrue(claimed.available)
                self.assertFalse(database.connection.in_transaction)
                with sockets_blocked(), self.assertRaises(
                    GoogleOidcAuthorizationTransactionRepositoryError
                ) as replay:
                    claim_google_oidc_authorization_transaction(
                        database.connection,
                        second.gateway,
                        authority,
                        state,
                    )
                self.assertEqual(
                    replay.exception.reason_code,
                    "invalid_or_expired_transaction",
                )
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
                claimed.close()
                prepared.close()
            finally:
                if second is not None:
                    second.close()
                else:
                    first.close()
                authority.close()

    def test_invitation_is_ciphertext_bound_reconstructed_and_one_use(self):
        invitation_text = "inv_" + ("c" * 32) + "." + ("D" * 43)
        invitation_bytes = invitation_text.encode("ascii")
        with durable_transaction_database(suffix="repository-invitation") as database:
            authority = key_authority()
            first = reconstructed_gateway(
                subject="google-subject-repository-invitation"
            )
            second = None
            invitation = bytearray(invitation_bytes)
            unchanged_tables = (
                "users",
                "auth_identities",
                "account_invitations",
                "account_sessions",
                "product_principals",
                "principal_account_bindings",
                "product_profiles",
            )
            before = {
                name: database.connection.execute(
                    f'SELECT COUNT(*) FROM "{name}"'
                ).fetchone()[0]
                for name in unchanged_tables
            }
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        first.gateway,
                        authority,
                        invitation_credential=invitation,
                    )
                self.assertEqual(invitation, bytearray())
                state = authorization_parameters(prepared)["state"]
                row = transaction_rows(database.connection)[0]
                self.assertNotIn(invitation_bytes, row["protected_material"])
                self.assertNotIn(invitation_text, repr(row))
                first.close()
                second = reconstructed_gateway(
                    subject="google-subject-repository-invitation"
                )
                with sockets_blocked():
                    claimed = claim_google_oidc_authorization_transaction(
                        database.connection,
                        second.gateway,
                        authority,
                        state,
                    )
                values = transaction_domain._take_claimed_material(claimed)
                retained = values["invitation_credential"]
                try:
                    self.assertEqual(bytes(retained), invitation_bytes)
                finally:
                    transaction_domain._clear_claimed_material_values(values)
                self.assertEqual(retained, bytearray())
                with sockets_blocked(), self.assertRaises(
                    GoogleOidcAuthorizationTransactionRepositoryError
                ):
                    claim_google_oidc_authorization_transaction(
                        database.connection,
                        second.gateway,
                        authority,
                        state,
                    )
                self.assertEqual(
                    {
                        name: database.connection.execute(
                            f'SELECT COUNT(*) FROM "{name}"'
                        ).fetchone()[0]
                        for name in unchanged_tables
                    },
                    before,
                )
                claimed.close()
                prepared.close()
            finally:
                if second is not None:
                    second.close()
                else:
                    first.close()
                authority.close()

    def test_legacy_material_reconstructs_and_invalid_invitation_never_commits(self):
        with durable_transaction_database(suffix="repository-legacy-invitation") as database:
            authority = key_authority()
            first = reconstructed_gateway(
                subject="google-subject-repository-legacy-invitation"
            )
            second = None
            def legacy_serializer(**values):
                values.pop("invitation_credential", None)
                return transaction_domain._serialize_protected_material_v1(
                    **values
                )

            try:
                with sockets_blocked(), mock.patch.object(
                    transaction_protection,
                    "_serialize_protected_material",
                    side_effect=legacy_serializer,
                ):
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        first.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                first.close()
                second = reconstructed_gateway(
                    subject="google-subject-repository-legacy-invitation"
                )
                with sockets_blocked():
                    claimed = claim_google_oidc_authorization_transaction(
                        database.connection,
                        second.gateway,
                        authority,
                        state,
                    )
                values = transaction_domain._take_claimed_material(claimed)
                try:
                    self.assertIsNone(values["invitation_credential"])
                finally:
                    transaction_domain._clear_claimed_material_values(values)
                claimed.close()
                prepared.close()

                oversized = bytearray(
                    b"x" * (
                        transaction_domain.MAX_INVITATION_CREDENTIAL_BYTES + 1
                    )
                )
                with sockets_blocked(), self.assertRaises(
                    GoogleOidcAuthorizationTransactionRepositoryError
                ):
                    prepare_google_oidc_authorization_transaction(
                        database.connection,
                        second.gateway,
                        authority,
                        invitation_credential=oversized,
                    )
                self.assertEqual(oversized, bytearray())
                self.assertEqual(len(transaction_rows(database.connection)), 1)
            finally:
                if second is not None:
                    second.close()
                else:
                    first.close()
                authority.close()

    def test_configuration_substitution_terminally_invalidates_without_decryption(self):
        with durable_transaction_database(suffix="repository-config") as database:
            authority = key_authority()
            original = reconstructed_gateway(
                subject="google-subject-repository-config"
            )
            substituted = make_real_gateway(
                client_id="different-client-id.example",
                client_secret=bytearray(
                    b"different-client-secret-material-000000"
                ),
                subject="google-subject-repository-config",
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        original.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_unprotect_material",
                    side_effect=AssertionError("decrypt_must_not_run"),
                ), self.assertRaises(
                    GoogleOidcAuthorizationTransactionRepositoryError
                ) as failure:
                    claim_google_oidc_authorization_transaction(
                        database.connection,
                        substituted.gateway,
                        authority,
                        state,
                    )
                self.assertEqual(
                    failure.exception.reason_code,
                    "invalid_or_expired_transaction",
                )
                row = transaction_rows(database.connection)[0]
                self.assertEqual(row["lifecycle"], "invalidated")
                self.assertIsNone(row["claimed_at"])
                self.assertEqual(row["row_version"], 2)
                prepared.close()
            finally:
                original.close()
                substituted.close()
                authority.close()

    def test_expiry_equality_and_clock_rollback_are_terminal(self):
        for mode in ("expiry", "rollback"):
            with self.subTest(mode=mode), durable_transaction_database(
                suffix=f"repository-{mode}"
            ) as database:
                clock = ManualClock(NOW)
                authority = key_authority()
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=f"google-subject-repository-{mode}",
                )
                try:
                    with sockets_blocked():
                        prepared = prepare_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                        )
                    state = authorization_parameters(prepared)["state"]
                    if mode == "expiry":
                        clock.advance(600)
                    else:
                        clock.advance_wall(-1)
                    with sockets_blocked(), self.assertRaises(
                        GoogleOidcAuthorizationTransactionRepositoryError
                    ):
                        claim_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                            state,
                        )
                    row = transaction_rows(database.connection)[0]
                    self.assertEqual(
                        row["lifecycle"],
                        "expired" if mode == "expiry" else "invalidated",
                    )
                    self.assertEqual(row["row_version"], 2)
                    prepared.close()
                finally:
                    harness.close()
                    authority.close()

    def test_failure_before_commit_rolls_back_and_retry_uses_fresh_material(self):
        with durable_transaction_database(suffix="repository-crash") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-repository-crash"
            )
            failed_once = False

            def crash(boundary):
                nonlocal failed_once
                if boundary == "prepare.after_insert" and not failed_once:
                    failed_once = True
                    raise RuntimeError("private crash marker")

            try:
                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_failure_boundary",
                    side_effect=crash,
                ), self.assertRaises(
                    GoogleOidcAuthorizationTransactionRepositoryError
                ):
                    prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                self.assertEqual(transaction_rows(database.connection), ())
                self.assertFalse(database.connection.in_transaction)
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                self.assertEqual(len(transaction_rows(database.connection)), 1)
                prepared.close()
            finally:
                harness.close()
                authority.close()

    def test_cleanup_is_bounded_and_never_expires_then_deletes_same_row(self):
        with durable_transaction_database(suffix="repository-cleanup") as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-repository-cleanup",
            )
            try:
                with sockets_blocked():
                    first = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                    second = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                    claimed = claim_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                        authorization_parameters(first)["state"],
                    )
                clock.advance(600)
                with sockets_blocked():
                    one = cleanup_google_oidc_authorization_transactions(
                        database.connection,
                        harness.gateway,
                        authority,
                        limit=1,
                        terminal_retention_seconds=1,
                    )
                self.assertEqual(one.expired_count, 1)
                self.assertEqual(one.deleted_count, 0)
                self.assertEqual(
                    {row["lifecycle"] for row in transaction_rows(database.connection)},
                    {"consumed", "expired"},
                )
                clock.advance(1)
                with sockets_blocked():
                    two = cleanup_google_oidc_authorization_transactions(
                        database.connection,
                        harness.gateway,
                        authority,
                        limit=2,
                        terminal_retention_seconds=1,
                    )
                self.assertEqual(two.expired_count, 0)
                self.assertEqual(two.deleted_count, 2)
                self.assertEqual(transaction_rows(database.connection), ())
                claimed.close()
                first.close()
                second.close()
            finally:
                harness.close()
                authority.close()

    def test_cleanup_requires_exact_explicit_terminal_retention_contract(self):
        class IntegerSubclass(int):
            pass

        class HostileTruthy:
            def __init__(self):
                self.calls = 0

            def __bool__(self):
                self.calls += 1
                raise AssertionError("truth conversion must not run")

            def __int__(self):
                self.calls += 1
                raise AssertionError("integer conversion must not run")

        with durable_transaction_database(
            suffix="repository-cleanup-retention-contract"
        ) as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-cleanup-retention-contract"
            )
            hostile = HostileTruthy()
            try:
                with self.assertRaises(TypeError):
                    cleanup_google_oidc_authorization_transactions(
                        database.connection,
                        harness.gateway,
                        authority,
                        limit=1,
                    )
                for retention in (
                    True,
                    False,
                    0,
                    -1,
                    (
                        GOOGLE_OIDC_CLEANUP_CONTRACT
                        .max_terminal_retention_seconds
                        + 1
                    ),
                    1.0,
                    "1",
                    None,
                    object(),
                    IntegerSubclass(1),
                    hostile,
                ):
                    with self.subTest(retention=type(retention).__name__), (
                        self.assertRaises(
                            GoogleOidcAuthorizationTransactionRepositoryError
                        )
                    ):
                        _run_cleanup(
                            database.connection,
                            harness.gateway,
                            authority,
                            limit=1,
                            terminal_retention_seconds=retention,
                        )
                self.assertEqual(hostile.calls, 0)
                for retention in (
                    GOOGLE_OIDC_CLEANUP_CONTRACT
                    .min_terminal_retention_seconds,
                    GOOGLE_OIDC_CLEANUP_CONTRACT
                    .max_terminal_retention_seconds,
                ):
                    result = (
                        _run_cleanup(
                            database.connection,
                            harness.gateway,
                            authority,
                            limit=1,
                            terminal_retention_seconds=retention,
                        )
                    )
                    self.assertEqual(
                        result.terminal_retention_seconds,
                        retention,
                    )
                    self.assertTrue(result.complete)
                    self.assertEqual(result.commit_outcome, "committed")
            finally:
                harness.close()
                authority.close()

        for suffix, now, retention, succeeds in (
            (
                "underflow",
                datetime(1, 1, 1, tzinfo=timezone.utc),
                1,
                False,
            ),
            (
                "far-future",
                datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
                GOOGLE_OIDC_CLEANUP_CONTRACT.max_terminal_retention_seconds,
                True,
            ),
        ):
            with self.subTest(clock=suffix), durable_transaction_database(
                suffix=f"repository-cleanup-clock-{suffix}"
            ) as database:
                clock = ManualClock(now)
                authority = key_authority()
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=f"google-subject-cleanup-clock-{suffix}",
                )
                try:
                    if succeeds:
                        setup_harness = reconstructed_gateway(
                            clock=ManualClock(NOW),
                            subject=(
                                "google-subject-cleanup-clock-"
                                f"{suffix}-setup"
                            ),
                        )
                        try:
                            _create_terminal_row(
                                database.connection,
                                setup_harness.gateway,
                                authority,
                                "invalidated",
                            )
                        finally:
                            setup_harness.close()
                        result = (
                            _run_cleanup(
                                database.connection,
                                harness.gateway,
                                authority,
                                limit=1,
                                terminal_retention_seconds=retention,
                            )
                        )
                        self.assertTrue(result.complete)
                        self.assertEqual(result.deleted_count, 1)
                    else:
                        with self.assertRaises(
                            GoogleOidcAuthorizationTransactionRepositoryError
                        ):
                            _run_cleanup(
                                database.connection,
                                harness.gateway,
                                authority,
                                limit=1,
                                terminal_retention_seconds=retention,
                            )
                    self.assertFalse(database.connection.in_transaction)
                finally:
                    harness.close()
                    authority.close()

    def test_cleanup_exact_expiry_and_terminal_retention_boundaries(self):
        for offset, expected_expired in ((599, 0), (600, 1), (601, 1)):
            with self.subTest(
                prepared_offset=offset
            ), durable_transaction_database(
                suffix=f"repository-cleanup-prepared-{offset}"
            ) as database:
                clock = ManualClock(NOW)
                authority = key_authority()
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=f"google-subject-cleanup-prepared-{offset}",
                )
                try:
                    with sockets_blocked():
                        prepared = (
                            prepare_google_oidc_authorization_transaction(
                                database.connection,
                                harness.gateway,
                                authority,
                            )
                        )
                    clock.advance(offset)
                    result = _run_cleanup(
                        database.connection,
                        harness.gateway,
                        authority,
                        limit=1,
                        terminal_retention_seconds=60,
                    )
                    self.assertEqual(
                        result.expired_count,
                        expected_expired,
                    )
                    self.assertEqual(result.deleted_count, 0)
                    lifecycle = transaction_rows(database.connection)[0][
                        "lifecycle"
                    ]
                    self.assertEqual(
                        lifecycle,
                        "expired" if expected_expired else "prepared",
                    )
                    prepared.close()
                finally:
                    harness.close()
                    authority.close()

        retention = 60
        for lifecycle in ("consumed", "expired", "invalidated"):
            for age_offset, expected_deleted in ((-1, 0), (0, 1), (1, 1)):
                with self.subTest(
                    lifecycle=lifecycle,
                    age_offset=age_offset,
                ), durable_transaction_database(
                    suffix=(
                        "repository-cleanup-retention-"
                        f"{lifecycle}-{age_offset}"
                    )
                ) as database:
                    clock = ManualClock(NOW)
                    authority = key_authority()
                    harness = reconstructed_gateway(
                        clock=clock,
                        subject=(
                            "google-subject-cleanup-retention-"
                            f"{lifecycle}-{age_offset}"
                        ),
                    )
                    try:
                        row = _create_terminal_row(
                            database.connection,
                            harness.gateway,
                            authority,
                            lifecycle,
                        )
                        terminal_at = datetime.fromisoformat(
                            row["terminal_at"]
                        )
                        clock.advance(
                            int((terminal_at - NOW).total_seconds())
                            + retention
                            + age_offset
                        )
                        result = (
                            _run_cleanup(
                                database.connection,
                                harness.gateway,
                                authority,
                                limit=1,
                                terminal_retention_seconds=retention,
                            )
                        )
                        self.assertEqual(
                            result.deleted_count,
                            expected_deleted,
                        )
                        self.assertEqual(
                            len(transaction_rows(database.connection)),
                            0 if expected_deleted else 1,
                        )
                        self.assertEqual(
                            result.skipped_too_recent,
                            0 if expected_deleted else 1,
                        )
                    finally:
                        harness.close()
                        authority.close()

        with durable_transaction_database(
            suffix="repository-cleanup-new-expiry-survives"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-cleanup-new-expiry-survives",
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                clock.advance(600)
                first = _run_cleanup(
                    database.connection,
                    harness.gateway,
                    authority,
                    limit=2,
                    terminal_retention_seconds=1,
                )
                self.assertEqual(
                    (first.expired_count, first.deleted_count),
                    (1, 0),
                )
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "expired",
                )
                clock.advance(1)
                second = _run_cleanup(
                    database.connection,
                    harness.gateway,
                    authority,
                    limit=2,
                    terminal_retention_seconds=1,
                )
                self.assertEqual(
                    (second.expired_count, second.deleted_count),
                    (0, 1),
                )
                prepared.close()
            finally:
                harness.close()
                authority.close()

    def test_cleanup_retains_malformed_future_and_unaccepted_terminals(self):
        with durable_transaction_database(
            suffix="repository-cleanup-invalid-terminals"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-cleanup-invalid-terminals",
            )
            labels = (
                "future",
                "before_creation",
                "multiple_timestamps",
                "missing_terminal",
                "wrong_row_version",
                "unknown_lifecycle",
                "unknown_lookup",
                "unknown_protection",
                "wrong_storage",
                "invalid_environment",
                "invalid_configuration",
                "invalid_nonce",
                "invalid_material",
                "length_contradiction",
                "copied_material_a",
                "copied_material_b",
                "valid_neighbor",
            )
            rows = {}
            try:
                for label in labels:
                    rows[label] = _create_terminal_row(
                        database.connection,
                        harness.gateway,
                        authority,
                        "invalidated",
                    )
                copied_material = rows["copied_material_a"][
                    "protected_material"
                ]
                mutations = (
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET terminal_at=? WHERE transaction_id=?",
                        (
                            (NOW + timedelta(seconds=100)).isoformat(),
                            rows["future"]["transaction_id"],
                        ),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET terminal_at=? WHERE transaction_id=?",
                        (
                            (NOW - timedelta(seconds=1)).isoformat(),
                            rows["before_creation"]["transaction_id"],
                        ),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET claimed_at=terminal_at WHERE transaction_id=?",
                        (rows["multiple_timestamps"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET terminal_at=NULL WHERE transaction_id=?",
                        (rows["missing_terminal"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET row_version=1 WHERE transaction_id=?",
                        (rows["wrong_row_version"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET lifecycle='archived' WHERE transaction_id=?",
                        (rows["unknown_lifecycle"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET lookup_key_version=2 WHERE transaction_id=?",
                        (rows["unknown_lookup"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET protection_key_version=12 WHERE transaction_id=?",
                        (rows["unknown_protection"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET protection_nonce='abcdefghijkl' "
                        "WHERE transaction_id=?",
                        (rows["wrong_storage"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET environment_namespace='INVALID!' "
                        "WHERE transaction_id=?",
                        (rows["invalid_environment"]["transaction_id"],),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET configuration_fingerprint=? "
                        "WHERE transaction_id=?",
                        (
                            b"c" * 31,
                            rows["invalid_configuration"]["transaction_id"],
                        ),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET protection_nonce=? WHERE transaction_id=?",
                        (
                            b"n" * 11,
                            rows["invalid_nonce"]["transaction_id"],
                        ),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET protected_material=? WHERE transaction_id=?",
                        (
                            b"m" * 16,
                            rows["invalid_material"]["transaction_id"],
                        ),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET configuration_fingerprint=? "
                        "WHERE transaction_id=?",
                        (
                            b"l" * 33,
                            rows["length_contradiction"]["transaction_id"],
                        ),
                    ),
                    (
                        "UPDATE google_oidc_authorization_transactions "
                        "SET protected_material=? WHERE transaction_id=?",
                        (
                            copied_material,
                            rows["copied_material_b"]["transaction_id"],
                        ),
                    ),
                )
                _mutate_rows_bypassing_update_guard(
                    database.connection,
                    mutations,
                )
                clock.advance(2)
                result = _run_cleanup(
                    database.connection,
                    harness.gateway,
                    authority,
                    limit=10,
                    terminal_retention_seconds=1,
                )
                self.assertEqual(result.terminal_candidates_inspected, 17)
                self.assertEqual(result.deleted_count, 1)
                self.assertEqual(result.skipped_chronology_invalid, 4)
                self.assertEqual(result.skipped_unsupported_version, 2)
                self.assertEqual(result.skipped_structurally_invalid, 10)
                remaining = transaction_rows(database.connection)
                self.assertEqual(len(remaining), 16)
                self.assertNotIn(
                    rows["valid_neighbor"]["transaction_id"],
                    {row["transaction_id"] for row in remaining},
                )
                for label in labels[:-1]:
                    self.assertIn(
                        rows[label]["transaction_id"],
                        {row["transaction_id"] for row in remaining},
                    )
                rendered = repr(result)
                for row in rows.values():
                    self.assertNotIn(row["transaction_id"], rendered)
                    self.assertNotIn(row["terminal_at"], rendered)

                report = (
                    reconciliation
                    .reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        accepted_lookup_key_versions=(1,),
                        accepted_protection_key_versions=(11,),
                        source_guarantees_no_sidecar_creation=True,
                    )
                )
                self.assertTrue(report.blocking)
                self.assertNotEqual(report.status, "clean")
                report_text = (
                    report.to_json_bytes() + report.to_human_bytes()
                ).decode("utf-8")
                for row in remaining:
                    self.assertNotIn(row["transaction_id"], report_text)
            finally:
                harness.close()
                authority.close()

    def test_cleanup_candidate_and_mutation_limits_are_truthful(self):
        for row_count, expected_truncated, expected_deleted in (
            (2, False, 2),
            (3, True, 0),
        ):
            with self.subTest(
                candidate_rows=row_count
            ), durable_transaction_database(
                suffix=f"repository-cleanup-candidate-{row_count}"
            ) as database:
                clock = ManualClock(NOW)
                authority = key_authority()
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=f"google-subject-cleanup-candidate-{row_count}",
                )
                try:
                    for _index in range(row_count):
                        _create_terminal_row(
                            database.connection,
                            harness.gateway,
                            authority,
                            "invalidated",
                        )
                    clock.advance(1)
                    with mock.patch.object(
                        repository,
                        "_CLEANUP_CANDIDATE_INSPECTION_LIMIT",
                        2,
                    ):
                        result = (
                            _run_cleanup(
                                database.connection,
                                harness.gateway,
                                authority,
                                limit=2,
                                terminal_retention_seconds=1,
                            )
                        )
                    self.assertEqual(
                        result.candidate_inspection_truncated,
                        expected_truncated,
                    )
                    self.assertEqual(
                        result.deleted_count,
                        expected_deleted,
                    )
                    self.assertEqual(
                        len(transaction_rows(database.connection)),
                        row_count - expected_deleted,
                    )
                    self.assertEqual(
                        result.complete,
                        not expected_truncated,
                    )
                    self.assertEqual(
                        result.remaining_exact,
                        not expected_truncated,
                    )
                    self.assertGreaterEqual(
                        result.known_remaining,
                        1 if expected_truncated else 0,
                    )
                finally:
                    harness.close()
                    authority.close()

        actual_limit = (
            GOOGLE_OIDC_CLEANUP_CONTRACT.max_candidate_inspections
        )
        for row_count, expected_truncated in (
            (actual_limit, False),
            (actual_limit + 1, True),
        ):
            with self.subTest(
                production_candidate_rows=row_count
            ), durable_transaction_database(
                suffix=f"repository-cleanup-production-candidate-{row_count}"
            ) as database:
                clock = ManualClock(NOW)
                authority = key_authority()
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=(
                        "google-subject-cleanup-production-candidate-"
                        f"{row_count}"
                    ),
                )
                try:
                    _insert_valid_terminal_rows_without_insert_guard(
                        database.connection,
                        row_count,
                    )
                    result = _run_cleanup(
                        database.connection,
                        harness.gateway,
                        authority,
                        limit=1,
                        terminal_retention_seconds=1,
                    )
                    self.assertEqual(
                        result.terminal_candidates_inspected,
                        actual_limit,
                    )
                    self.assertEqual(
                        result.candidate_inspection_truncated,
                        expected_truncated,
                    )
                    self.assertEqual(result.deleted_count, 0)
                    self.assertEqual(
                        result.skipped_too_recent,
                        actual_limit,
                    )
                    self.assertEqual(
                        result.complete,
                        not expected_truncated,
                    )
                    self.assertEqual(
                        len(transaction_rows(database.connection)),
                        row_count,
                    )
                finally:
                    harness.close()
                    authority.close()

        for row_count, expected_remaining in ((1000, 0), (1001, 1)):
            with self.subTest(
                production_mutation_rows=row_count
            ), durable_transaction_database(
                suffix=f"repository-cleanup-production-mutation-{row_count}"
            ) as database:
                clock = ManualClock(NOW)
                authority = key_authority()
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=(
                        "google-subject-cleanup-production-mutation-"
                        f"{row_count}"
                    ),
                )
                try:
                    _insert_valid_terminal_rows_without_insert_guard(
                        database.connection,
                        row_count,
                    )
                    clock.advance(1)
                    result = _run_cleanup(
                        database.connection,
                        harness.gateway,
                        authority,
                        limit=1000,
                        terminal_retention_seconds=1,
                    )
                    self.assertEqual(result.deleted_count, 1000)
                    self.assertEqual(
                        result.known_remaining,
                        expected_remaining,
                    )
                    self.assertEqual(
                        result.complete,
                        expected_remaining == 0,
                    )
                    self.assertEqual(
                        len(transaction_rows(database.connection)),
                        expected_remaining,
                    )
                finally:
                    harness.close()
                    authority.close()

        with durable_transaction_database(
            suffix="repository-cleanup-mutation-over"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-cleanup-mutation-over",
            )
            try:
                for _index in range(3):
                    _create_terminal_row(
                        database.connection,
                        harness.gateway,
                        authority,
                        "invalidated",
                    )
                clock.advance(1)
                result = _run_cleanup(
                    database.connection,
                    harness.gateway,
                    authority,
                    limit=2,
                    terminal_retention_seconds=1,
                )
                self.assertEqual(result.deleted_count, 2)
                self.assertEqual(result.known_remaining, 1)
                self.assertTrue(result.remaining_exact)
                self.assertFalse(result.complete)
                self.assertEqual(
                    len(transaction_rows(database.connection)),
                    1,
                )
                self.assertEqual(
                    sum(
                        value
                        for key, value in result.as_dict().items()
                        if key.startswith("skipped_")
                    ),
                    0,
                )
                rendered = repr(result)
                for name, value in result.as_dict().items():
                    self.assertIn(f"{name}={value!r}", rendered)
                with self.assertRaises(AttributeError):
                    result.complete = True
                with self.assertRaises(TypeError):
                    type(result)()
            finally:
                harness.close()
                authority.close()

    def test_insert_replace_and_repository_recursive_trigger_matrix(self):
        for initial_recursive_triggers in (0, 1):
            with self.subTest(
                initial_recursive_triggers=initial_recursive_triggers
            ), durable_transaction_database(
                suffix=f"repository-lifecycle-{initial_recursive_triggers}"
            ) as database:
                authority = key_authority()
                clock = ManualClock(NOW)
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=(
                        "google-subject-repository-lifecycle-"
                        f"{initial_recursive_triggers}"
                    )
                )
                prepared = None
                claimed = None
                try:
                    database.connection.execute(
                        f"PRAGMA recursive_triggers = "
                        f"{initial_recursive_triggers}"
                    )
                    with sockets_blocked():
                        prepared = prepare_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                        )
                    self.assertEqual(
                        database.connection.execute(
                            "PRAGMA recursive_triggers"
                        ).fetchone()[0],
                        1,
                    )
                    state = authorization_parameters(prepared)["state"]
                    prepared_row = transaction_rows(database.connection)[0]

                    for index, lifecycle in enumerate(
                        ("consumed", "expired", "invalidated"),
                        start=1,
                    ):
                        terminal = dict(prepared_row)
                        terminal["transaction_id"] = (
                            "oidctx_" + f"{index:032x}"
                        )
                        terminal["state_lookup_digest"] = bytes([index]) * 32
                        terminal["protection_nonce"] = bytes([index]) * 12
                        terminal["lifecycle"] = lifecycle
                        terminal["row_version"] = 2
                        terminal["claimed_at"] = (
                            terminal["created_at"]
                            if lifecycle == "consumed"
                            else None
                        )
                        terminal["terminal_at"] = (
                            terminal["expires_at"]
                            if lifecycle == "expired"
                            else terminal["created_at"]
                        )
                        database.connection.execute(
                            f"PRAGMA recursive_triggers = "
                            f"{initial_recursive_triggers}"
                        )
                        with self.assertRaises(sqlite3.IntegrityError):
                            _execute_transaction_insert(
                                database.connection,
                                terminal,
                            )
                        database.connection.rollback()
                    self.assertEqual(
                        transaction_rows(database.connection),
                        (prepared_row,),
                    )

                    replacement_cases = []
                    replacement_cases.append(dict(prepared_row))
                    changed_material = dict(prepared_row)
                    changed_material["protected_material"] = (
                        prepared_row["protected_material"][:-1]
                        + bytes(
                            [
                                prepared_row["protected_material"][-1]
                                ^ 1
                            ]
                        )
                    )
                    replacement_cases.append(changed_material)
                    lookup_collision = dict(prepared_row)
                    lookup_collision["transaction_id"] = (
                        "oidctx_" + "d" * 32
                    )
                    lookup_collision["protection_nonce"] = b"\xd1" * 12
                    replacement_cases.append(lookup_collision)
                    for replacement in replacement_cases:
                        database.connection.execute(
                            f"PRAGMA recursive_triggers = "
                            f"{initial_recursive_triggers}"
                        )
                        with self.assertRaises(sqlite3.IntegrityError):
                            _execute_transaction_insert(
                                database.connection,
                                replacement,
                                replace=True,
                            )
                        database.connection.rollback()
                        self.assertEqual(
                            transaction_rows(database.connection),
                            (prepared_row,),
                        )

                    database.connection.execute(
                        f"PRAGMA recursive_triggers = "
                        f"{initial_recursive_triggers}"
                    )
                    with sockets_blocked():
                        claimed = claim_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                            state,
                        )
                    self.assertEqual(
                        database.connection.execute(
                            "PRAGMA recursive_triggers"
                        ).fetchone()[0],
                        1,
                    )
                    terminal_row = transaction_rows(database.connection)[0]
                    terminal_as_prepared = dict(terminal_row)
                    terminal_as_prepared.update(
                        {
                            "lifecycle": "prepared",
                            "claimed_at": None,
                            "terminal_at": None,
                            "row_version": 1,
                        }
                    )
                    for replacement in (
                        dict(terminal_row),
                        terminal_as_prepared,
                    ):
                        database.connection.execute(
                            f"PRAGMA recursive_triggers = "
                            f"{initial_recursive_triggers}"
                        )
                        with self.assertRaises(sqlite3.IntegrityError):
                            _execute_transaction_insert(
                                database.connection,
                                replacement,
                                replace=True,
                            )
                        database.connection.rollback()
                        self.assertEqual(
                            transaction_rows(database.connection),
                            (terminal_row,),
                        )

                    database.connection.execute(
                        f"PRAGMA recursive_triggers = "
                        f"{initial_recursive_triggers}"
                    )
                    clock.advance(1)
                    cleanup = (
                        _run_cleanup(
                            database.connection,
                            harness.gateway,
                            authority,
                            limit=1,
                            terminal_retention_seconds=1,
                        )
                    )
                    self.assertEqual(cleanup.expired_count, 0)
                    self.assertEqual(cleanup.deleted_count, 1)
                    self.assertEqual(
                        database.connection.execute(
                            "PRAGMA recursive_triggers"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        transaction_rows(database.connection),
                        (),
                    )
                finally:
                    if claimed is not None:
                        claimed.close()
                    if prepared is not None:
                        prepared.close()
                    harness.close()
                    authority.close()

    def test_independent_m001_m002_m003_definition_drift_is_rejected(self):
        cases = (
            (
                "001_pipeline_state",
                "DROP INDEX idx_user_pipeline_transitions_occurred; "
                "CREATE INDEX idx_user_pipeline_transitions_occurred "
                "ON user_pipeline_transitions(id);",
            ),
            (
                "002_accounts_sessions",
                "DROP INDEX idx_account_sessions_user_active; "
                "CREATE INDEX idx_account_sessions_user_active "
                "ON account_sessions(session_id);",
            ),
            (
                "003_product_principals",
                "DROP INDEX idx_product_principals_environment_type; "
                "CREATE INDEX idx_product_principals_environment_type "
                "ON product_principals(principal_id);",
            ),
        )
        for version, mutation in cases:
            with self.subTest(version=version), durable_transaction_database(
                suffix=f"repository-prerequisite-{version[:3]}"
            ) as database:
                database.connection.executescript(mutation)
                attestation = (
                    attest_google_oidc_authorization_transaction_schema(
                        database.connection
                    )
                )
                self.assertEqual(
                    attestation["state"],
                    "invalid_prerequisite",
                )
                self.assertIn(
                    version,
                    attestation["prerequisite_schema_attestation"][
                        "invalid_migrations"
                    ],
                )
                authority = key_authority()
                harness = reconstructed_gateway(
                    subject=f"google-subject-prerequisite-{version[:3]}"
                )
                try:
                    with sockets_blocked(), self.assertRaises(
                        GoogleOidcAuthorizationTransactionRepositoryError
                    ):
                        prepare_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                        )
                    self.assertEqual(
                        transaction_rows(database.connection),
                        (),
                    )
                finally:
                    harness.close()
                    authority.close()

    def test_prerequisite_missing_renamed_additional_conflicting_and_temp_matrix(self):
        cases = (
            (
                "missing",
                "001_pipeline_state",
                "DROP TRIGGER trg_user_pipeline_transitions_no_update;",
            ),
            (
                "renamed",
                "002_accounts_sessions",
                "DROP INDEX idx_account_sessions_user_active; "
                "CREATE INDEX idx_account_sessions_user_active_renamed "
                "ON account_sessions(user_id);",
            ),
            (
                "additional",
                "003_product_principals",
                "CREATE INDEX idx_product_principals_phase1_extra "
                "ON product_principals(principal_id);",
            ),
            (
                "conflicting",
                "002_accounts_sessions",
                "DROP INDEX idx_auth_identities_user; "
                "CREATE VIEW idx_auth_identities_user AS "
                "SELECT user_id FROM auth_identities;",
            ),
            (
                "temp_residue",
                "003_product_principals",
                "CREATE TEMP VIEW idx_product_principals_phase1_temp AS "
                "SELECT principal_id FROM main.product_principals;",
            ),
        )
        for category, version, mutation in cases:
            with self.subTest(
                category=category,
                version=version,
            ), durable_transaction_database(
                suffix=f"repository-prerequisite-{category}"
            ) as database:
                database.connection.executescript(mutation)
                attestation = (
                    attest_google_oidc_authorization_transaction_schema(
                        database.connection
                    )
                )
                self.assertEqual(
                    attestation["state"],
                    "invalid_prerequisite",
                )
                self.assertIn(
                    version,
                    attestation["prerequisite_schema_attestation"][
                        "invalid_migrations"
                    ],
                )

    def test_prerequisite_ownership_closure_namespace_and_dependency_matrix(self):
        cases = (
            (
                "main_m001_prefixed_table",
                "001_pipeline_state",
                "main",
                "table",
                "user_pipeline_state_phase1_extra",
                "unexpected_prerequisite_owned_object",
                "CREATE TABLE user_pipeline_state_phase1_extra(value TEXT);",
            ),
            (
                "temp_m001_prefixed_table",
                "001_pipeline_state",
                "temp",
                "table",
                "user_pipeline_state_phase1_extra",
                "unexpected_prerequisite_owned_object",
                "CREATE TEMP TABLE user_pipeline_state_phase1_extra(value TEXT);",
            ),
            (
                "main_m003_prefixed_table",
                "003_product_principals",
                "main",
                "table",
                "product_principals_phase1_extra",
                "unexpected_prerequisite_owned_object",
                "CREATE TABLE product_principals_phase1_extra(value TEXT);",
            ),
            (
                "temp_m003_prefixed_table",
                "003_product_principals",
                "temp",
                "table",
                "product_principals_phase1_extra",
                "unexpected_prerequisite_owned_object",
                "CREATE TEMP TABLE product_principals_phase1_extra(value TEXT);",
            ),
            (
                "main_m001_view_dependency",
                "001_pipeline_state",
                "main",
                "view",
                "audit_m001_relation",
                "unexpected_prerequisite_view_dependency",
                "CREATE VIEW audit_m001_relation AS "
                "SELECT pipeline_item_id FROM user_pipeline_state;",
            ),
            (
                "temp_main_m001_view_dependency",
                "001_pipeline_state",
                "temp",
                "view",
                "audit_temp_m001_relation",
                "unexpected_prerequisite_view_dependency",
                "CREATE TEMP VIEW audit_temp_m001_relation AS "
                "SELECT pipeline_item_id FROM main.user_pipeline_state;",
            ),
            (
                "main_m003_view_dependency",
                "003_product_principals",
                "main",
                "view",
                "audit_m003_relation",
                "unexpected_prerequisite_view_dependency",
                "CREATE VIEW audit_m003_relation AS "
                "SELECT principal_id FROM product_principals;",
            ),
            (
                "transitive_m003_view_dependency",
                "003_product_principals",
                "main",
                "view",
                "audit_m003_outer",
                "unexpected_prerequisite_view_dependency",
                "CREATE VIEW audit_m003_inner AS "
                "SELECT principal_id FROM product_principals; "
                "CREATE VIEW audit_m003_outer AS "
                "SELECT principal_id FROM audit_m003_inner;",
            ),
            (
                "index_attached_to_m001",
                "001_pipeline_state",
                "main",
                "index",
                "audit_m001_attached_index",
                "unexpected_prerequisite_owned_object",
                "CREATE INDEX audit_m001_attached_index "
                "ON user_pipeline_state(pipeline_item_id);",
            ),
            (
                "trigger_attached_to_m003",
                "003_product_principals",
                "main",
                "trigger",
                "audit_m003_attached_trigger",
                "unexpected_prerequisite_owned_object",
                "CREATE TRIGGER audit_m003_attached_trigger "
                "AFTER INSERT ON product_principals BEGIN SELECT 1; END;",
            ),
        )
        for (
            category,
            version,
            schema,
            object_type,
            object_name,
            reason,
            mutation,
        ) in cases:
            with self.subTest(category=category), durable_transaction_database(
                suffix=f"repository-closure-{category}"
            ) as database:
                database.connection.executescript(mutation)
                attestation = (
                    attest_google_oidc_authorization_transaction_schema(
                        database.connection
                    )
                )
                self.assertEqual(
                    attestation["state"],
                    "invalid_prerequisite",
                )
                prerequisite = attestation[
                    "prerequisite_schema_attestation"
                ]
                self.assertIn(version, prerequisite["invalid_migrations"])
                self.assertTrue(
                    any(
                        finding.get("migration") == version
                        and finding.get("schema") == schema
                        and finding.get("object_type") == object_type
                        and finding.get("object") == object_name
                        and finding.get("reason") == reason
                        for finding in prerequisite[
                            "ownership_closure_findings"
                        ]
                    ),
                    prerequisite["ownership_closure_findings"],
                )
                self.assertTrue(
                    all(
                        "sql" not in finding
                        and "definition" not in finding
                        for finding in prerequisite[
                            "ownership_closure_findings"
                        ]
                    )
                )

    def test_ascii_case_equivalent_reserved_namespaces_and_object_types(self):
        contract = (
            transaction_schema._expected_m001_m003_ownership_contract()
        )
        empty_schema = {"objects": (), "columns": {}}
        for version, details in contract["migrations"].items():
            for schema_name in ("main", "temp"):
                objects = tuple(
                    (
                        "table",
                        prefix.swapcase() + f"CaSeAuDiT{ordinal}",
                        prefix.swapcase() + f"CaSeAuDiT{ordinal}",
                        None,
                    )
                    for ordinal, prefix in enumerate(
                        details["reserved_prefixes"]
                    )
                )
                snapshot = {
                    "main": empty_schema,
                    "temp": empty_schema,
                }
                snapshot[schema_name] = {
                    "objects": objects,
                    "columns": {},
                }
                findings = (
                    transaction_schema._reserved_prerequisite_namespace_findings(
                        snapshot,
                        contract,
                    )
                )
                observed = {
                    (
                        finding["migration"],
                        finding["schema"],
                        finding["object"],
                    )
                    for finding in findings
                }
                self.assertEqual(
                    observed,
                    {
                        (version, schema_name, name)
                        for _, name, _, _ in objects
                    },
                )

        accepted_objects = tuple(
            (
                kind,
                name.swapcase(),
                table_name.swapcase(),
                None,
            )
            for details in contract["migrations"].values()
            for kind, name, table_name in details["records"]
        )
        self.assertEqual(
            transaction_schema._reserved_prerequisite_namespace_findings(
                {
                    "main": {
                        "objects": accepted_objects,
                        "columns": {},
                    },
                    "temp": empty_schema,
                },
                contract,
            ),
            [],
        )

        real_connection = sqlite3.connect(":memory:")
        try:
            expected_real = set()
            ordinal = 0
            for version, details in contract["migrations"].items():
                for prefix in details["reserved_prefixes"]:
                    if prefix.startswith("sqlite_"):
                        continue
                    for schema_name in ("main", "temp"):
                        object_name = (
                            prefix.swapcase()
                            + f"QuOtEdReAlCaSe{ordinal}"
                        )
                        temporary = (
                            "TEMP " if schema_name == "temp" else ""
                        )
                        real_connection.execute(
                            f"CREATE {temporary}TABLE "
                            f"{transaction_schema._quote(object_name)}"
                            "(value TEXT)"
                        )
                        expected_real.add(
                            (version, schema_name, object_name)
                        )
                        ordinal += 1
            real_findings = (
                transaction_schema._attest_m001_m003_ownership_closure(
                    real_connection
                )
            )
            observed_real = {
                (
                    finding["migration"],
                    finding["schema"],
                    finding["object"],
                )
                for finding in real_findings
                if finding.get("reason")
                == "unexpected_prerequisite_owned_object"
            }
            self.assertTrue(
                expected_real <= observed_real,
                expected_real - observed_real,
            )
        finally:
            real_connection.close()

        with durable_transaction_database(
            suffix="repository-closure-ascii-case"
        ) as database:
            database.connection.executescript(
                'CREATE TABLE audit_case_target(value TEXT); '
                'CREATE TABLE "UsEr_PiPeLiNe_StAtE_CaSeTaBlE"'
                "(value TEXT); "
                'CREATE INDEX "IdX_UsEr_PiPeLiNe_TrAnSiTiOnS_CaSeInDeX" '
                "ON audit_case_target(value); "
                'CREATE TRIGGER "TrG_PrOdUcT_PrInCiPaLs_CaSeTrIgGeR" '
                "AFTER INSERT ON audit_case_target BEGIN SELECT 1; END; "
                'CREATE VIEW "PrOdUcT_PrInCiPaLs_CaSeViEw" AS '
                "SELECT value FROM audit_case_target; "
                'CREATE TEMP TABLE "PrOdUcT_PrInCiPaLs"(value TEXT);'
            )
            attestation = (
                attest_google_oidc_authorization_transaction_schema(
                    database.connection
                )
            )
            self.assertEqual(attestation["state"], "invalid_prerequisite")
            findings = attestation["prerequisite_schema_attestation"][
                "ownership_closure_findings"
            ]
            observed = {
                (
                    finding["migration"],
                    finding["schema"],
                    finding["object_type"],
                    finding["object"],
                )
                for finding in findings
                if finding.get("reason")
                == "unexpected_prerequisite_owned_object"
            }
            self.assertTrue(
                {
                    (
                        "001_pipeline_state",
                        "main",
                        "table",
                        "UsEr_PiPeLiNe_StAtE_CaSeTaBlE",
                    ),
                    (
                        "001_pipeline_state",
                        "main",
                        "index",
                        "IdX_UsEr_PiPeLiNe_TrAnSiTiOnS_CaSeInDeX",
                    ),
                    (
                        "003_product_principals",
                        "main",
                        "trigger",
                        "TrG_PrOdUcT_PrInCiPaLs_CaSeTrIgGeR",
                    ),
                    (
                        "003_product_principals",
                        "main",
                        "view",
                        "PrOdUcT_PrInCiPaLs_CaSeViEw",
                    ),
                    (
                        "003_product_principals",
                        "temp",
                        "table",
                        "PrOdUcT_PrInCiPaLs",
                    ),
                }
                <= observed,
                observed,
            )

    def test_authoritative_reserved_family_derivation_and_generated_matrix(self):
        expected_tables = {
            "001_pipeline_state": frozenset(
                {
                    "user_pipeline_state",
                    "user_pipeline_transitions",
                    "wahojobs_schema_migrations",
                }
            ),
            "003_product_principals": frozenset(
                {
                    "product_principals",
                    "legacy_owner_aliases",
                    "principal_account_bindings",
                    "ownership_binding_events",
                }
            ),
        }
        contract = (
            transaction_schema._expected_m001_m003_ownership_contract()
        )
        objects = {"main": [], "temp": []}
        expected_findings = set()
        ordinal = 0

        for version, tables in expected_tables.items():
            details = contract["migrations"][version]
            self.assertEqual(details["owned_tables"], tables)
            expected_index_families = tuple(
                sorted(f"idx_{table_name}_" for table_name in tables)
            )
            expected_trigger_families = tuple(
                sorted(f"trg_{table_name}_" for table_name in tables)
            )
            expected_automatic_index_families = tuple(
                sorted(
                    f"sqlite_autoindex_{table_name}_"
                    for table_name in tables
                )
            )
            self.assertEqual(
                details["reserved_index_families"],
                expected_index_families,
            )
            self.assertEqual(
                details["reserved_trigger_families"],
                expected_trigger_families,
            )
            self.assertEqual(
                details["reserved_automatic_index_families"],
                expected_automatic_index_families,
            )
            self.assertTrue(
                set(expected_index_families)
                | set(expected_trigger_families)
                | set(expected_automatic_index_families)
                <= set(details["reserved_prefixes"])
            )

            for table_name in sorted(tables):
                families = (
                    ("index", f"idx_{table_name}_"),
                    ("trigger", f"trg_{table_name}_"),
                )
                for object_type, family in families:
                    for schema_name in ("main", "temp"):
                        for spelling in (
                            family + f"matrix_lower_{ordinal}",
                            family.swapcase()
                            + f"QuOtEd_MiXeD_{ordinal}",
                        ):
                            objects[schema_name].append(
                                (
                                    object_type,
                                    spelling,
                                    f"audit_unrelated_target_{ordinal}",
                                    None,
                                )
                            )
                            expected_findings.add(
                                (
                                    version,
                                    schema_name,
                                    object_type,
                                    spelling,
                                )
                            )
                            ordinal += 1

                for schema_name in ("main", "temp"):
                    for object_type in ("index", "trigger"):
                        object_name = (
                            f"audit_attached_{object_type}_{ordinal}"
                        )
                        objects[schema_name].append(
                            (
                                object_type,
                                object_name,
                                table_name.swapcase(),
                                None,
                            )
                        )
                        expected_findings.add(
                            (
                                version,
                                schema_name,
                                object_type,
                                object_name,
                            )
                        )
                        ordinal += 1

        for schema_name in ("main", "temp"):
            objects[schema_name].extend(
                (
                    (
                        "index",
                        f"audit_unrelated_index_{schema_name}",
                        f"audit_unrelated_table_{schema_name}",
                        None,
                    ),
                    (
                        "trigger",
                        f"audit_unrelated_trigger_{schema_name}",
                        f"audit_unrelated_table_{schema_name}",
                        None,
                    ),
                )
            )

        findings = (
            transaction_schema._reserved_prerequisite_namespace_findings(
                {
                    schema_name: {
                        "objects": tuple(schema_objects),
                        "columns": {},
                    }
                    for schema_name, schema_objects in objects.items()
                },
                contract,
            )
        )
        observed_findings = {
            (
                finding["migration"],
                finding["schema"],
                finding["object_type"],
                finding["object"],
            )
            for finding in findings
        }
        self.assertEqual(observed_findings, expected_findings)
        self.assertTrue(
            all(
                finding["reason"]
                == "unexpected_prerequisite_owned_object"
                and "sql" not in finding
                and "definition" not in finding
                for finding in findings
            )
        )
        self.assertIn(
            "idx_user_pipeline_state_",
            contract["migrations"]["001_pipeline_state"][
                "reserved_index_families"
            ],
        )
        self.assertIn(
            "trg_user_pipeline_state_",
            contract["migrations"]["001_pipeline_state"][
                "reserved_trigger_families"
            ],
        )

    def test_semantic_row_set_dependency_matrix(self):
        cases = (
            (
                "constant",
                {"001_pipeline_state"},
                "main",
                "audit_constant_rows",
                "CREATE VIEW audit_constant_rows AS "
                "SELECT 1 AS value FROM user_pipeline_state;",
            ),
            (
                "count_star",
                {"001_pipeline_state"},
                "main",
                "audit_count_rows",
                "CREATE VIEW audit_count_rows AS "
                "SELECT count(*) AS value FROM user_pipeline_state;",
            ),
            (
                "exists",
                {"003_product_principals"},
                "main",
                "audit_exists_rows",
                "CREATE VIEW audit_exists_rows AS "
                "SELECT EXISTS("
                "SELECT 1 FROM product_principals"
                ") AS value;",
            ),
            (
                "in_subquery",
                {"001_pipeline_state"},
                "main",
                "audit_in_rows",
                "CREATE VIEW audit_in_rows AS "
                "SELECT 'missing' IN ("
                "SELECT pipeline_item_id FROM user_pipeline_state"
                ") AS value;",
            ),
            (
                "scalar_subquery",
                {"003_product_principals"},
                "main",
                "audit_scalar_rows",
                "CREATE VIEW audit_scalar_rows AS "
                "SELECT (SELECT principal_id "
                "FROM product_principals LIMIT 1) AS value;",
            ),
            (
                "implicit_rowid",
                {"001_pipeline_state"},
                "main",
                "audit_rowid_rows",
                "CREATE VIEW audit_rowid_rows AS "
                "SELECT rowid FROM user_pipeline_state;",
            ),
            (
                "join",
                {"001_pipeline_state"},
                "main",
                "audit_join_rows",
                "CREATE TABLE audit_join_input(value TEXT); "
                "CREATE VIEW audit_join_rows AS "
                "SELECT input.value FROM audit_join_input AS input "
                "JOIN user_pipeline_state AS owned ON 1=1;",
            ),
            (
                "cte",
                {"003_product_principals"},
                "main",
                "audit_cte_rows",
                "CREATE VIEW audit_cte_rows AS "
                "WITH owned AS ("
                "SELECT 1 AS value FROM product_principals"
                ") SELECT value FROM owned;",
            ),
            (
                "compound",
                {"001_pipeline_state"},
                "main",
                "audit_compound_rows",
                "CREATE TABLE audit_compound_input(value TEXT); "
                "CREATE VIEW audit_compound_rows AS "
                "SELECT 1 AS value FROM user_pipeline_state "
                "UNION ALL "
                "SELECT 2 AS value FROM audit_compound_input;",
            ),
            (
                "quoted_alias",
                {"001_pipeline_state"},
                "main",
                "audit_quoted_rows",
                'CREATE VIEW audit_quoted_rows AS SELECT '
                '"OwnedAlias"."pipeline_item_id" '
                'FROM "UsEr_PiPeLiNe_StAtE" AS "OwnedAlias";',
            ),
            (
                "temp_main_constant",
                {"003_product_principals"},
                "temp",
                "audit_temp_rows",
                "CREATE TEMP VIEW audit_temp_rows AS "
                "SELECT 1 AS value FROM main.product_principals;",
            ),
            (
                "transitive",
                {"001_pipeline_state"},
                "main",
                "audit_transitive_outer",
                "CREATE VIEW audit_transitive_inner AS "
                "SELECT 1 AS value FROM user_pipeline_state; "
                "CREATE VIEW audit_transitive_outer AS "
                "SELECT value FROM audit_transitive_inner;",
            ),
            (
                "mixed",
                {
                    "001_pipeline_state",
                    "003_product_principals",
                },
                "main",
                "audit_mixed_rows",
                "CREATE TABLE audit_mixed_input(value TEXT); "
                "CREATE VIEW audit_mixed_rows AS "
                "SELECT 1 AS value FROM user_pipeline_state "
                "UNION ALL "
                "SELECT 2 AS value FROM product_principals "
                "UNION ALL "
                "SELECT 3 AS value FROM audit_mixed_input;",
            ),
        )
        for category, versions, schema_name, view_name, mutation in cases:
            with self.subTest(category=category), durable_transaction_database(
                suffix=f"repository-row-set-{category}"
            ) as database:
                database.connection.executescript(mutation)
                attestation = (
                    attest_google_oidc_authorization_transaction_schema(
                        database.connection
                    )
                )
                self.assertEqual(
                    attestation["state"],
                    "invalid_prerequisite",
                )
                findings = attestation[
                    "prerequisite_schema_attestation"
                ]["ownership_closure_findings"]
                observed = {
                    finding["migration"]
                    for finding in findings
                    if finding.get("reason")
                    == "unexpected_prerequisite_view_dependency"
                    and finding.get("schema") == schema_name
                    and finding.get("object") == view_name
                }
                self.assertEqual(observed, versions, findings)

    def test_prerequisite_closure_uses_one_aggregate_budget(self):
        def make_schema(*, combined):
            connection = sqlite3.connect(":memory:")
            connection.executescript(
                "CREATE TABLE budget_input_a("
                "value_a TEXT, value_b TEXT, value_c TEXT"
                "); "
                "CREATE VIEW budget_view_a AS "
                "SELECT value_a FROM budget_input_a;"
            )
            if combined:
                connection.executescript(
                    "CREATE TABLE budget_input_b("
                    "value_d TEXT, value_e TEXT, value_f TEXT"
                    "); "
                    "CREATE VIEW budget_view_b AS "
                    "SELECT value_d FROM budget_input_b;"
                )
            return connection

        def measure(connection):
            budget = (
                transaction_schema._new_prerequisite_closure_budget()
            )
            contract = (
                transaction_schema._expected_m001_m003_ownership_contract()
            )
            snapshot = (
                transaction_schema._bounded_prerequisite_schema_snapshot(
                    connection,
                    contract,
                    budget,
                )
            )
            self.assertEqual(
                transaction_schema._reserved_prerequisite_namespace_findings(
                    snapshot,
                    contract,
                ),
                [],
            )
            self.assertEqual(
                transaction_schema._semantic_prerequisite_view_dependencies(
                    snapshot,
                    contract,
                    budget,
                ),
                [],
            )
            return dict(budget["used"])

        def assert_sanitized_failure(findings):
            self.assertEqual(
                {
                    (
                        finding["reason"],
                        finding["migration"],
                        finding["object"],
                    )
                    for finding in findings
                },
                {
                    (
                        "prerequisite_ownership_closure_inspection_failed",
                        "001_pipeline_state",
                        "prerequisite_ownership_closure",
                    ),
                    (
                        "prerequisite_ownership_closure_inspection_failed",
                        "003_product_principals",
                        "prerequisite_ownership_closure",
                    ),
                },
            )
            rendered = repr(findings)
            self.assertNotIn("budget_input", rendered)
            self.assertNotIn("budget_view", rendered)
            self.assertNotIn("CREATE ", rendered)

        limit_names = {
            "schema_objects": "_MAX_PREREQUISITE_SCHEMA_OBJECTS",
            "views": "_MAX_PREREQUISITE_VIEWS",
            "authorizer_calls": "_MAX_PREREQUISITE_AUTHORIZER_CALLS",
            "explain_rows": "_MAX_PREREQUISITE_EXPLAIN_ROWS",
            "columns": "_MAX_PREREQUISITE_COLUMNS",
            "schema_sql_bytes": (
                "_MAX_PREREQUISITE_SCHEMA_SQL_BYTES"
            ),
        }
        single = make_schema(combined=False)
        combined = make_schema(combined=True)
        try:
            single_used = measure(single)
            combined_used = measure(combined)
            for resource in (
                "authorizer_calls",
                "explain_rows",
                "columns",
                "schema_sql_bytes",
            ):
                combined_limit = combined_used[resource] - 1
                self.assertLessEqual(
                    single_used[resource],
                    combined_limit,
                )
                with mock.patch.object(
                    transaction_schema,
                    limit_names[resource],
                    combined_limit,
                ):
                    self.assertEqual(
                        transaction_schema._attest_m001_m003_ownership_closure(
                            single
                        ),
                        [],
                    )
                    assert_sanitized_failure(
                        transaction_schema._attest_m001_m003_ownership_closure(
                            combined
                        )
                    )

            for resource, constant_name in limit_names.items():
                exact_limit = combined_used[resource]
                self.assertGreater(exact_limit, 0)
                with mock.patch.object(
                    transaction_schema,
                    constant_name,
                    exact_limit,
                ):
                    self.assertEqual(
                        transaction_schema._attest_m001_m003_ownership_closure(
                            combined
                        ),
                        [],
                    )
                with mock.patch.object(
                    transaction_schema,
                    constant_name,
                    exact_limit - 1,
                ):
                    assert_sanitized_failure(
                        transaction_schema._attest_m001_m003_ownership_closure(
                            combined
                        )
                    )
        finally:
            combined.close()
            single.close()

    def test_prerequisite_closure_preserves_caller_authorizer(self):
        with durable_transaction_database(
            suffix="repository-closure-caller-authorizer"
        ) as database:
            calls = []

            def caller_authorizer(action, *_details):
                calls.append(action)
                return sqlite3.SQLITE_OK

            database.connection.set_authorizer(caller_authorizer)
            try:
                attestation = (
                    attest_google_oidc_authorization_transaction_schema(
                        database.connection
                    )
                )
                self.assertEqual(
                    attestation["state"],
                    "correctly_installed",
                )
                calls_before_probe = len(calls)
                database.connection.execute(
                    "SELECT 8675309"
                ).fetchone()
                self.assertGreater(len(calls), calls_before_probe)
            finally:
                database.connection.set_authorizer(None)

    def test_prerequisite_snapshot_avoids_caller_conversion_callbacks(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE audit_callback_free_snapshot(value TEXT)"
        )
        callback_calls = []

        def forbidden_callback(*_values):
            callback_calls.append("called")
            raise AssertionError("caller_conversion_callback_invoked")

        connection.create_function("length", 1, forbidden_callback)
        connection.row_factory = forbidden_callback
        connection.text_factory = forbidden_callback
        prior_length_limit = connection.getlimit(
            sqlite3.SQLITE_LIMIT_LENGTH
        )
        try:
            self.assertEqual(
                transaction_schema._attest_m001_m003_ownership_closure(
                    connection
                ),
                [],
            )
            self.assertEqual(callback_calls, [])
            self.assertEqual(
                connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH),
                prior_length_limit,
            )
        finally:
            connection.close()

    def test_prerequisite_closure_accepts_canonical_and_unrelated_objects(self):
        pending = install_canonical_v2_profiles(":memory:")
        try:
            pending_attestation = (
                attest_google_oidc_authorization_transaction_schema(pending)
            )
            self.assertEqual(pending_attestation["state"], "pending")
            self.assertEqual(
                pending_attestation["prerequisite_schema_attestation"][
                    "ownership_closure_findings"
                ],
                [],
            )
        finally:
            pending.close()

        with durable_transaction_database(
            suffix="repository-closure-canonical"
        ) as database:
            canonical = attest_google_oidc_authorization_transaction_schema(
                database.connection
            )
            self.assertEqual(canonical["state"], "correctly_installed")
            self.assertEqual(
                canonical["prerequisite_schema_attestation"][
                    "ownership_closure_findings"
                ],
                [],
            )
            database.connection.executescript(
                "CREATE TABLE audit_unrelated_values(value TEXT); "
                "CREATE VIEW audit_unrelated_projection AS "
                "SELECT value FROM audit_unrelated_values "
                "/* user_pipeline_state product_principals */; "
                "CREATE VIEW audit_unrelated_projection_two AS "
                "SELECT count(*) AS value FROM audit_unrelated_values; "
                "CREATE VIEW audit_unrelated_reserved_cte AS "
                "WITH user_pipeline_state(value) AS ("
                "SELECT value FROM audit_unrelated_values"
                ") SELECT value FROM user_pipeline_state; "
                'CREATE TABLE "u\u017fer_pipeline_state_unicode_unrelated"'
                "(value TEXT);"
            )
            unrelated = attest_google_oidc_authorization_transaction_schema(
                database.connection
            )
            self.assertEqual(unrelated["state"], "correctly_installed")
            self.assertEqual(
                unrelated["prerequisite_schema_attestation"][
                    "ownership_closure_findings"
                ],
                [],
            )

    def test_same_dependency_is_rejected_pending_installed_and_post_install(self):
        def contaminate(connection, name):
            connection.execute(
                f'CREATE VIEW "{name}" AS '
                "SELECT count(*) AS value "
                'FROM "UsEr_PiPeLiNe_StAtE"'
            )

        pending = install_canonical_v2_profiles(":memory:")
        try:
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(pending)[
                    "state"
                ],
                "pending",
            )
            contaminate(pending, "audit_pending_dependency")
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(pending)[
                    "state"
                ],
                "invalid_prerequisite",
            )
        finally:
            pending.close()

        with tempfile.TemporaryDirectory() as directory:
            post_install_path = (
                Path(directory)
                / "repository-same-dependency-post-install.sqlite"
            )
            post_install = install_canonical_v2_profiles(post_install_path)
            try:
                migration_006.apply_google_oidc_authorization_transactions_migration(
                    post_install,
                    requested_path=post_install_path,
                    expected_identity=(
                        migration_006.database_file_identity(post_install_path)
                    ),
                )
                self.assertEqual(
                    attest_google_oidc_authorization_transaction_schema(
                        post_install
                    )["state"],
                    "correctly_installed",
                )
                contaminate(post_install, "audit_post_install_dependency")
                self.assertEqual(
                    attest_google_oidc_authorization_transaction_schema(
                        post_install
                    )["state"],
                    "invalid_prerequisite",
                )
            finally:
                post_install.close()

        with durable_transaction_database(
            suffix="repository-closure-installed"
        ) as database:
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(
                    database.connection
                )["state"],
                "correctly_installed",
            )
            contaminate(
                database.connection,
                "audit_correctly_installed_dependency",
            )
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(
                database.connection
            )["state"],
                "invalid_prerequisite",
            )

    def test_same_reserved_family_is_rejected_pending_installed_and_post_install(
        self,
    ):
        def contaminate(connection, suffix):
            target_name = f"audit_reserved_family_target_{suffix}"
            object_name = (
                f"IdX_UsEr_PiPeLiNe_StAtE_reserved_family_{suffix}"
            )
            connection.execute(
                f'CREATE TABLE "{target_name}"(value TEXT)'
            )
            connection.execute(
                f'CREATE INDEX "{object_name}" '
                f'ON "{target_name}"(value)'
            )
            return object_name

        def assert_rejected(connection, object_name):
            attestation = (
                attest_google_oidc_authorization_transaction_schema(
                    connection
                )
            )
            self.assertEqual(attestation["state"], "invalid_prerequisite")
            findings = attestation["prerequisite_schema_attestation"][
                "ownership_closure_findings"
            ]
            self.assertTrue(
                any(
                    finding.get("reason")
                    == "unexpected_prerequisite_owned_object"
                    and finding.get("migration")
                    == "001_pipeline_state"
                    and finding.get("object_type") == "index"
                    and finding.get("object") == object_name
                    for finding in findings
                ),
                findings,
            )

        pending = install_canonical_v2_profiles(":memory:")
        try:
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(pending)[
                    "state"
                ],
                "pending",
            )
            assert_rejected(pending, contaminate(pending, "pending"))
        finally:
            pending.close()

        with tempfile.TemporaryDirectory() as directory:
            post_install_path = (
                Path(directory)
                / "repository-same-reserved-family-post-install.sqlite"
            )
            post_install = install_canonical_v2_profiles(post_install_path)
            try:
                migration_006.apply_google_oidc_authorization_transactions_migration(
                    post_install,
                    requested_path=post_install_path,
                    expected_identity=(
                        migration_006.database_file_identity(post_install_path)
                    ),
                )
                self.assertEqual(
                    attest_google_oidc_authorization_transaction_schema(
                        post_install
                    )["state"],
                    "correctly_installed",
                )
                assert_rejected(
                    post_install,
                    contaminate(post_install, "post_install"),
                )
            finally:
                post_install.close()

        with durable_transaction_database(
            suffix="repository-reserved-family-installed"
        ) as database:
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(
                    database.connection
                )["state"],
                "correctly_installed",
            )
            assert_rejected(
                database.connection,
                contaminate(database.connection, "installed"),
            )

    def test_uninspectable_view_dependency_fails_closed_and_sanitized(self):
        with durable_transaction_database(
            suffix="repository-closure-uninspectable"
        ) as database:
            secret = "closure_unknown_function_319e56b4"
            database.connection.executescript(
                "CREATE TABLE audit_unrelated_input(value TEXT); "
                "CREATE VIEW audit_uninspectable_projection AS "
                f"SELECT {secret}(value) FROM audit_unrelated_input;"
            )
            attestation = (
                attest_google_oidc_authorization_transaction_schema(
                    database.connection
                )
            )
            self.assertEqual(attestation["state"], "invalid_prerequisite")
            findings = attestation["prerequisite_schema_attestation"][
                "ownership_closure_findings"
            ]
            self.assertEqual(
                {
                    (
                        finding["reason"],
                        finding["migration"],
                        finding["object"],
                    )
                    for finding in findings
                },
                {
                    (
                        "prerequisite_ownership_closure_inspection_failed",
                        "001_pipeline_state",
                        "prerequisite_ownership_closure",
                    ),
                    (
                        "prerequisite_ownership_closure_inspection_failed",
                        "003_product_principals",
                        "prerequisite_ownership_closure",
                    ),
                },
            )
            rendered = repr(findings)
            self.assertNotIn(secret, rendered)
            self.assertNotIn("audit_uninspectable_projection", rendered)
            self.assertNotIn("CREATE VIEW", rendered)

    def test_prerequisite_closure_gates_prepare_claim_and_cleanup(self):
        with durable_transaction_database(
            suffix="repository-closure-call-sites"
        ) as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-repository-closure-call-sites"
            )
            prepared = None
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                before = transaction_rows(database.connection)
                database.connection.executescript(
                    "CREATE TABLE audit_repository_namespace_target("
                    "value TEXT"
                    "); "
                    'CREATE INDEX "IdX_UsEr_PiPeLiNe_StAtE_'
                    'repository_boundary" '
                    "ON audit_repository_namespace_target(value);"
                )
                with sockets_blocked():
                    for operation in (
                        lambda: prepare_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                        ),
                        lambda: claim_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                            state,
                        ),
                        lambda: cleanup_google_oidc_authorization_transactions(
                            database.connection,
                            harness.gateway,
                            authority,
                            limit=1,
                            terminal_retention_seconds=1,
                        ),
                    ):
                        with self.subTest(
                            operation=operation.__code__.co_firstlineno
                        ), self.assertRaises(
                            GoogleOidcAuthorizationTransactionRepositoryError
                        ):
                            operation()
                self.assertFalse(database.connection.in_transaction)
                self.assertEqual(
                    transaction_rows(database.connection),
                    before,
                )
            finally:
                if prepared is not None:
                    prepared.close()
                harness.close()
                authority.close()

    def test_repository_public_failure_graphs_are_recursively_sanitized(self):
        with durable_transaction_database(
            suffix="repository-failure-graph"
        ) as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-repository-failure-graph"
            )
            record = object.__getattribute__(
                authority,
                "_GoogleOidcTransactionKeyAuthority__record",
            )
            authority_objects = (
                authority,
                record,
                record.lookup_keys,
                record.protection_keys,
                *record.lookup_keys.values(),
                *record.protection_keys.values(),
                harness.gateway,
            )
            authority_material = tuple(
                bytes(value)
                for value in (
                    *record.lookup_keys.values(),
                    *record.protection_keys.values(),
                )
            )
            prepared = None
            try:
                retained, retained_cause = _chained_failure()

                def fail_prepare(boundary):
                    if boundary == "prepare.after_insert":
                        raise retained

                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_failure_boundary",
                    side_effect=fail_prepare,
                ):
                    public = _capture_prepare_failure(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                self.assertIsNone(retained.__traceback__)
                self.assertIsNone(retained.__cause__)
                self.assertIsNone(retained.__context__)
                self.assertIsNone(retained_cause.__traceback__)
                self.assertSanitizedFailureGraph(
                    public,
                    forbidden_objects=authority_objects,
                    forbidden_binary=authority_material,
                )
                self.assertEqual(transaction_rows(database.connection), ())

                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                protected_row = transaction_rows(database.connection)[0]
                row_objects = (protected_row,)
                row_binary = tuple(
                    value
                    for value in protected_row.values()
                    if type(value) is bytes
                )
                row_text = (
                    state,
                    protected_row["transaction_id"],
                )
                retained, retained_cause = _chained_failure()
                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_unprotect_material",
                    side_effect=retained,
                ):
                    public = _capture_claim_failure(
                        database.connection,
                        harness.gateway,
                        authority,
                        state,
                    )
                self.assertIsNone(retained.__traceback__)
                self.assertIsNone(retained.__cause__)
                self.assertIsNone(retained.__context__)
                self.assertIsNone(retained_cause.__traceback__)
                self.assertSanitizedFailureGraph(
                    public,
                    forbidden_objects=(*authority_objects, *row_objects),
                    forbidden_binary=(*authority_material, *row_binary),
                    forbidden_text=row_text,
                )

                retained, retained_cause = _chained_failure()

                def fail_cleanup(boundary):
                    if boundary == "cleanup.after_delete":
                        raise retained

                with mock.patch.object(
                    repository,
                    "_failure_boundary",
                    side_effect=fail_cleanup,
                ):
                    public = _capture_cleanup_failure(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                self.assertIsNone(retained.__traceback__)
                self.assertIsNone(retained.__cause__)
                self.assertIsNone(retained.__context__)
                self.assertIsNone(retained_cause.__traceback__)
                self.assertSanitizedFailureGraph(
                    public,
                    forbidden_objects=(*authority_objects, *row_objects),
                    forbidden_binary=(*authority_material, *row_binary),
                    forbidden_text=row_text,
                )
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
            finally:
                if prepared is not None:
                    prepared.close()
                harness.close()
                authority.close()

    def test_repository_control_flow_is_preserved_after_fail_closed_cleanup(self):
        with durable_transaction_database(
            suffix="repository-control-flow"
        ) as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-repository-control-flow"
            )
            control = KeyboardInterrupt("repository control flow")

            def interrupt(boundary):
                if boundary == "prepare.after_insert":
                    raise control

            try:
                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_failure_boundary",
                    side_effect=interrupt,
                ):
                    try:
                        prepare_google_oidc_authorization_transaction(
                            database.connection,
                            harness.gateway,
                            authority,
                        )
                    except KeyboardInterrupt as observed:
                        self.assertIs(observed, control)
                    else:
                        self.fail("KeyboardInterrupt was not preserved")
                self.assertEqual(transaction_rows(database.connection), ())
                self.assertFalse(database.connection.in_transaction)
            finally:
                harness.close()
                authority.close()

    def test_outer_transactions_and_nonexact_connections_are_rejected(self):
        with durable_transaction_database(suffix="repository-outer") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-repository-outer"
            )
            try:
                database.connection.execute("BEGIN")
                with sockets_blocked(), self.assertRaises(
                    GoogleOidcAuthorizationTransactionRepositoryError
                ):
                    prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                self.assertTrue(database.connection.in_transaction)
                database.connection.rollback()
                self.assertEqual(transaction_rows(database.connection), ())
            finally:
                harness.close()
                authority.close()


if __name__ == "__main__":
    unittest.main()
