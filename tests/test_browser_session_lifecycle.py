import copy
import base64
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import pickle
import re
import sqlite3
import unittest
from unittest import mock

from tests.accounts_test_support import create_user
from tests.browser_session_authentication_test_support import (
    browser_request,
    read_only_connection,
)
from tests.browser_session_lifecycle_test_support import (
    NOW,
    close_secret_vault,
    corrupt_effective_expiry,
    corrupt_issuance_handle,
    consume_issued,
    create_command,
    create_browser_session,
    finalize_issued,
    lifecycle_database,
    request_secret_vault,
    recursively_reachable_objects,
    revoke_command,
    revoke_browser_session,
    rotate_command,
    rotate_browser_session,
    session_row,
    token_from_cookie_header,
    vault_entry_count,
    vault_for_result,
    vault_is_closed_and_empty,
)
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.browser_session_lifecycle import (
    BrowserSessionLifecycleError,
    CreateBrowserSessionCommand,
    IssuedBrowserSession,
    RequestScopedSessionSecretVault,
    RevokeBrowserSessionCommand,
    RotateBrowserSessionCommand,
)


def marker_hits(values, token, csrf):
    raw_token = base64.urlsafe_b64decode(token + "=")
    raw_csrf = base64.urlsafe_b64decode(csrf + "=")
    markers = (token, csrf, raw_token, raw_csrf, bytearray(raw_token), bytearray(raw_csrf))
    return [type(value).__name__ for value in values if value in markers]


def traceback_values(error):
    values = []
    cursor = error.__traceback__
    while cursor is not None:
        values.extend(cursor.tb_frame.f_locals.values())
        cursor = cursor.tb_next
    return values


class BrowserSessionLifecycleTests(unittest.TestCase):
    def test_trusted_commands_reject_direct_dictionary_copy_pickle_and_subclass(self):
        for command_type in (
            CreateBrowserSessionCommand,
            RotateBrowserSessionCommand,
            RevokeBrowserSessionCommand,
        ):
            with self.subTest(command=command_type.__name__):
                with self.assertRaises(TypeError):
                    command_type({"account_id": "untrusted"})
                with self.assertRaises(TypeError):
                    type(f"Forged{command_type.__name__}", (command_type,), {})

        with lifecycle_database(suffix="command-seal") as (_path, _connection, created):
            command = create_command(created)
            self.assertNotIn(created.user.user_id, repr(command))
            self.assertNotIn(created.identity.auth_identity_id, str(command))
            self.assertFalse(hasattr(command, "__dict__"))
            with self.assertRaises(TypeError):
                copy.copy(command)
            with self.assertRaises(TypeError):
                copy.deepcopy(command)
            with self.assertRaises(TypeError):
                pickle.dumps(command)
            with self.assertRaises(TypeError):
                replace(command)
            with self.assertRaises(AttributeError):
                command.account_id = created.user.user_id
            with self.assertRaises(TypeError):
                command._payload["account_id"] = created.user.user_id

        import wahojobs.browser_session_lifecycle as lifecycle

        self.assertFalse(hasattr(lifecycle, "_TRUSTED_BROWSER_SESSION_COMMAND_ISSUER"))

    def test_command_policy_bounds_and_future_time_are_enforced(self):
        with lifecycle_database(suffix="command-policy") as (_path, connection, created):
            for invalid_ttl in (
                timedelta(seconds=59),
                timedelta(days=30, seconds=1),
            ):
                with self.subTest(invalid_ttl=invalid_ttl):
                    with self.assertRaises(TypeError):
                        create_command(created, idle_ttl=invalid_ttl)
            with self.assertRaises(TypeError):
                create_command(created, absolute_ttl=timedelta(days=90, seconds=1))
            with self.assertRaises(TypeError):
                create_command(
                    created,
                    accepted_at=NOW.replace(microsecond=1),
                )

            future = create_command(created, accepted_at=NOW + timedelta(seconds=1))
            with self.assertRaises(BrowserSessionLifecycleError) as error:
                create_browser_session(connection, future, _clock=lambda: NOW)
            self.assertEqual(error.exception.code, "ineligible_account_or_identity")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

    def test_valid_creation_stores_only_digests_and_issues_one_shot_credentials(self):
        with lifecycle_database(suffix="create-valid") as (_path, connection, created):
            result = create_browser_session(connection, create_command(created))
            self.assertEqual(result.status, "issued")
            self.assertIsInstance(result, IssuedBrowserSession)
            self.assertFalse(hasattr(result, "__dict__"))
            self.assertNotIn(created.user.user_id, repr(result))
            consumed = consume_issued(result)
            self.assertEqual(result.status, "consumed")
            header = consumed.set_cookie_header
            csrf = consumed.csrf_credential
            token = token_from_cookie_header(header)
            self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
            self.assertRegex(csrf, r"^[A-Za-z0-9_-]{43}$")
            self.assertNotEqual(token, csrf)
            self.assertIn("; Path=/;", header)
            self.assertIn("; Secure; HttpOnly; SameSite=Lax", header)
            self.assertIn("Max-Age=3600", header)
            self.assertIn("Expires=", header)
            self.assertNotIn("Domain=", header)
            self.assertNotIn("\r", header)
            self.assertNotIn("\n", header)
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(result)

            row = session_row(connection, key="browser-session-create-001")
            self.assertEqual(row["session_version"], 1)
            self.assertIsNone(row["revoked_at"])
            self.assertIsNone(row["rotated_at"])
            self.assertEqual(row["token_hash"], hashlib.sha256(token.encode("ascii")).hexdigest())
            self.assertEqual(row["csrf_secret_hash"], hashlib.sha256(csrf.encode("ascii")).hexdigest())
            self.assertNotIn(token, tuple(str(value) for value in row))
            self.assertNotIn(csrf, tuple(str(value) for value in row))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_session_rotations").fetchone()[0],
                0,
            )

    def test_issued_result_is_sealed_redacted_and_not_serializable(self):
        with lifecycle_database(suffix="result-seal") as (_path, connection, created):
            result = create_browser_session(connection, create_command(created))
            with self.assertRaises(TypeError):
                IssuedBrowserSession()
            with self.assertRaises(TypeError):
                type("ForgedIssuedBrowserSession", (IssuedBrowserSession,), {})
            with self.assertRaises(TypeError):
                copy.copy(result)
            with self.assertRaises(TypeError):
                copy.deepcopy(result)
            with self.assertRaises(TypeError):
                pickle.dumps(result)
            with self.assertRaises(TypeError):
                replace(result)
            with self.assertRaises(AttributeError):
                result.status = "changed"
            text = repr(result)
            self.assertIn("credentials=<redacted>", text)
            self.assertNotRegex(text, r"[A-Za-z0-9_-]{43}")

    def test_issued_result_has_no_direct_secret_fields_and_consumes_once(self):
        token = base64.urlsafe_b64encode(b"T" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"C" * 32).rstrip(b"=").decode("ascii")
        with lifecycle_database(suffix="result-secret-state") as (
            _path,
            connection,
            created,
        ):
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                result = create_browser_session(connection, create_command(created))
            self.assertNotIn("token", " ".join(IssuedBrowserSession.__slots__).lower())
            self.assertNotIn("csrf", " ".join(IssuedBrowserSession.__slots__).lower())
            for slot in IssuedBrowserSession.__slots__:
                if slot == "__weakref__":
                    continue
                attribute = (
                    f"_IssuedBrowserSession{slot}"
                    if slot.startswith("__")
                    else slot
                )
                value = getattr(result, attribute)
                self.assertNotEqual(value, token)
                self.assertNotEqual(value, csrf)
                self.assertNotIn(token, repr(value))
                self.assertNotIn(csrf, repr(value))
            self.assertFalse(hasattr(result, "_cookie_directive"))
            response = consume_issued(result)
            self.assertEqual(result.status, "consumed")
            self.assertEqual(token_from_cookie_header(response.set_cookie_header), token)
            self.assertEqual(response.csrf_credential, csrf)
            for slot in IssuedBrowserSession.__slots__:
                if slot != "__weakref__":
                    attribute = (
                        f"_IssuedBrowserSession{slot}"
                        if slot.startswith("__")
                        else slot
                    )
                    value = getattr(result, attribute)
                    self.assertNotIn(token, repr(value))
                    self.assertNotIn(csrf, repr(value))
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(result)

    def test_issued_result_recursive_state_cannot_reach_vault_or_credentials(self):
        token = base64.urlsafe_b64encode(b"V" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"W" * 32).rstrip(b"=").decode("ascii")
        with lifecycle_database(suffix="recursive-result-state") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                result = create_browser_session(
                    connection,
                    create_command(created),
                    secret_vault=vault,
                )
            reached = recursively_reachable_objects(result)
            self.assertEqual(marker_hits(reached, token, csrf), [])
            self.assertNotIn(id(vault), {id(value) for value in reached})
            self.assertFalse(hasattr(result, "__dict__"))
            self.assertNotIn(token, repr(result))
            self.assertNotIn(csrf, str(result))
            with self.assertRaises(TypeError):
                json.dumps(result)

            functions = [value for value in reached if callable(value)]
            for function in functions:
                closure = getattr(function, "__closure__", None)
                if closure:
                    closure_values = []
                    for cell in closure:
                        try:
                            closure_values.append(cell.cell_contents)
                        except ValueError:
                            pass
                    self.assertEqual(marker_hits(closure_values, token, csrf), [])

            vault_reached = recursively_reachable_objects(vault)
            self.assertCountEqual(marker_hits(vault_reached, token, csrf), ["bytearray", "bytearray"])
            self.assertEqual(vault_entry_count(vault), 1)
            response = consume_issued(result, vault=vault)
            self.assertIn(token, response.set_cookie_header)
            self.assertEqual(response.csrf_credential, csrf)
            self.assertEqual(marker_hits(recursively_reachable_objects(result), token, csrf), [])
            self.assertEqual(marker_hits(recursively_reachable_objects(vault), token, csrf), [])
            self.assertEqual(vault_entry_count(vault), 0)
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(result, vault=vault)
            close_secret_vault(vault)

    def test_request_vault_is_sealed_bounded_and_clears_unconsumed_entries(self):
        import wahojobs.browser_session_lifecycle as lifecycle

        token = base64.urlsafe_b64encode(b"X" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"Y" * 32).rstrip(b"=").decode("ascii")
        vault = request_secret_vault(max_entries=1, max_secret_bytes=128)
        self.assertFalse(hasattr(lifecycle, "_ISSUED_VAULTS"))
        self.assertFalse(hasattr(vault, "__dict__"))
        self.assertEqual(repr(vault), "RequestScopedSessionSecretVault(<redacted>)")
        with self.assertRaises(TypeError):
            RequestScopedSessionSecretVault()
        with self.assertRaises(TypeError):
            type("VaultSubclass", (RequestScopedSessionSecretVault,), {})
        with self.assertRaises(TypeError):
            copy.copy(vault)
        with self.assertRaises(TypeError):
            copy.deepcopy(vault)
        with self.assertRaises(TypeError):
            pickle.dumps(vault)
        with self.assertRaises(TypeError):
            json.dumps(vault)

        with lifecycle_database(suffix="vault-bounds") as (_path, connection, created):
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                result = create_browser_session(
                    connection,
                    create_command(created),
                    secret_vault=vault,
                )
            reached_before = recursively_reachable_objects(vault)
            secret_buffers = [
                value
                for value in reached_before
                if type(value) is bytearray and value in (
                    bytearray(base64.urlsafe_b64decode(token + "=")),
                    bytearray(base64.urlsafe_b64decode(csrf + "=")),
                )
            ]
            self.assertEqual(len(secret_buffers), 2)
            with self.assertRaises(BrowserSessionLifecycleError):
                create_browser_session(
                    connection,
                    create_command(created, key="browser-session-create-vault-full"),
                    secret_vault=vault,
                )
            self.assertEqual(vault_entry_count(vault), 1)
            self.assertEqual(result.status, "issued")
            close_secret_vault(vault)
            self.assertEqual(vault_entry_count(vault), 0)
            self.assertTrue(all(set(buffer) <= {0} for buffer in secret_buffers))
            self.assertEqual(marker_hits(recursively_reachable_objects(vault), token, csrf), [])

            byte_limited_vault = request_secret_vault(
                max_entries=2,
                max_secret_bytes=64,
            )
            create_browser_session(
                connection,
                create_command(
                    created,
                    key="browser-session-create-vault-byte-limit-first",
                ),
                secret_vault=byte_limited_vault,
            )
            with self.assertRaises(BrowserSessionLifecycleError):
                create_browser_session(
                    connection,
                    create_command(
                        created,
                        key="browser-session-create-vault-byte-limit-second",
                    ),
                    secret_vault=byte_limited_vault,
                )
            self.assertEqual(vault_entry_count(byte_limited_vault), 1)
            close_secret_vault(byte_limited_vault)

    def test_vault_consumption_rejects_wrong_inputs_and_clears_terminal_failures(self):
        with lifecycle_database(suffix="vault-consumption-errors") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            wrong_vault = request_secret_vault()
            result = create_browser_session(
                connection,
                create_command(created),
                secret_vault=vault,
            )
            with self.assertRaises(BrowserSessionLifecycleError):
                result.consume_for_response(wrong_vault, object(), now=NOW)
            self.assertEqual(result.status, "issued")
            self.assertEqual(vault_entry_count(vault), 1)
            with self.assertRaises(BrowserSessionLifecycleError) as wrong:
                consume_issued(result, vault=wrong_vault)
            self.assertEqual(wrong.exception.code, "internal_consistency_failure")
            self.assertEqual(result.status, "terminal_failed")
            self.assertTrue(vault_is_closed_and_empty(wrong_vault))
            self.assertEqual(vault_entry_count(vault), 1)
            with self.assertRaises(BrowserSessionLifecycleError) as retry:
                consume_issued(result, vault=vault)
            self.assertEqual(retry.exception.code, "internal_consistency_failure")
            self.assertEqual(vault_entry_count(vault), 1)
            close_secret_vault(vault)
            self.assertTrue(vault_is_closed_and_empty(vault))

            expired_vault = request_secret_vault()
            expired = create_browser_session(
                connection,
                create_command(
                    created,
                    key="browser-session-create-vault-expired",
                    idle_ttl=timedelta(minutes=1),
                    absolute_ttl=timedelta(minutes=1),
                ),
                secret_vault=expired_vault,
            )
            with self.assertRaises(BrowserSessionLifecycleError) as rejected:
                consume_issued(
                    expired,
                    vault=expired_vault,
                    now=NOW + timedelta(minutes=1),
                )
            self.assertEqual(rejected.exception.code, "session_state_conflict")
            self.assertEqual(expired.status, "terminal_failed")
            self.assertTrue(vault_is_closed_and_empty(expired_vault))
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(expired, vault=expired_vault, now=NOW)
            close_secret_vault(expired_vault)
            close_secret_vault(wrong_vault)

    def test_unmatched_handle_is_terminal_and_clears_the_complete_correct_vault(self):
        token = base64.urlsafe_b64encode(b"H" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"J" * 32).rstrip(b"=").decode("ascii")
        with lifecycle_database(suffix="vault-unmatched-handle") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                result = create_browser_session(
                    connection,
                    create_command(created),
                    secret_vault=vault,
                )
            unmatched_handle = "ish_" + "f" * 32
            if result._issuance_handle == unmatched_handle:
                unmatched_handle = "ish_" + "e" * 32
            original_handle = corrupt_issuance_handle(result, unmatched_handle)
            secret_buffers = [
                value
                for value in recursively_reachable_objects(vault)
                if type(value) is bytearray
            ]
            self.assertEqual(len(secret_buffers), 2)
            self.assertEqual(vault_entry_count(vault), 1)

            with self.assertRaises(BrowserSessionLifecycleError) as caught:
                consume_issued(result, vault=vault)
            self.assertEqual(caught.exception.code, "internal_consistency_failure")
            self.assertNotEqual(caught.exception.code, "already_completed")
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(result.status, "terminal_failed")
            self.assertIsNone(result._issuance_handle)
            self.assertIsNone(result._issuance_binding)
            self.assertTrue(vault_is_closed_and_empty(vault))
            self.assertEqual(vault_entry_count(vault), 0)
            self.assertTrue(all(set(buffer) <= {0} for buffer in secret_buffers))
            self.assertEqual(
                marker_hits(traceback_values(caught.exception), token, csrf),
                [],
            )
            self.assertEqual(
                marker_hits(recursively_reachable_objects(result), token, csrf),
                [],
            )
            self.assertEqual(
                marker_hits(recursively_reachable_objects(vault), token, csrf),
                [],
            )

            corrupt_issuance_handle(result, original_handle)
            with self.assertRaises(BrowserSessionLifecycleError) as original_retry:
                consume_issued(result, vault=vault)
            self.assertEqual(
                original_retry.exception.code,
                "internal_consistency_failure",
            )
            self.assertEqual(result.status, "terminal_failed")
            corrupt_issuance_handle(result, None)

            other_vault = request_secret_vault()
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(result, vault=other_vault)
            close_secret_vault(other_vault)
            close_secret_vault(vault)
            close_secret_vault(vault)
            self.assertTrue(vault_is_closed_and_empty(vault))

    def test_handle_binding_mismatch_cannot_consume_another_vault_entry(self):
        with lifecycle_database(suffix="vault-handle-binding-mismatch") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            first = create_browser_session(
                connection,
                create_command(created),
                secret_vault=vault,
            )
            second = create_browser_session(
                connection,
                create_command(
                    created,
                    key="browser-session-create-binding-second",
                ),
                secret_vault=vault,
            )
            self.assertEqual(vault_entry_count(vault), 2)
            corrupt_issuance_handle(first, second._issuance_handle)

            with self.assertRaises(BrowserSessionLifecycleError) as caught:
                consume_issued(first, vault=vault)
            self.assertEqual(caught.exception.code, "internal_consistency_failure")
            self.assertEqual(first.status, "terminal_failed")
            self.assertIsNone(first._issuance_handle)
            self.assertIsNone(first._issuance_binding)
            self.assertTrue(vault_is_closed_and_empty(vault))

            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(second, vault=vault)
            self.assertEqual(second.status, "terminal_failed")
            self.assertTrue(vault_is_closed_and_empty(vault))
            close_secret_vault(vault)

    def test_missing_entry_and_expiry_metadata_mismatch_are_terminal(self):
        with lifecycle_database(suffix="vault-terminal-metadata") as (
            _path,
            connection,
            created,
        ):
            missing_vault = request_secret_vault()
            missing = create_browser_session(
                connection,
                create_command(created),
                secret_vault=missing_vault,
            )
            close_secret_vault(missing_vault)
            with self.assertRaises(BrowserSessionLifecycleError) as missing_error:
                consume_issued(missing, vault=missing_vault)
            self.assertEqual(missing_error.exception.code, "internal_consistency_failure")
            self.assertEqual(missing.status, "terminal_failed")
            self.assertTrue(vault_is_closed_and_empty(missing_vault))

            mismatch_vault = request_secret_vault()
            mismatch = create_browser_session(
                connection,
                create_command(
                    created,
                    key="browser-session-create-expiry-mismatch",
                ),
                secret_vault=mismatch_vault,
            )
            corrupt_effective_expiry(
                mismatch,
                (NOW + timedelta(minutes=30)).isoformat(),
            )
            with self.assertRaises(BrowserSessionLifecycleError) as mismatch_error:
                consume_issued(mismatch, vault=mismatch_vault)
            self.assertEqual(
                mismatch_error.exception.code,
                "internal_consistency_failure",
            )
            self.assertEqual(mismatch.status, "terminal_failed")
            self.assertTrue(vault_is_closed_and_empty(mismatch_vault))

    def test_terminal_abort_retries_a_one_time_close_failure(self):
        with lifecycle_database(suffix="vault-terminal-close-retry") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            result = create_browser_session(
                connection,
                create_command(created),
                secret_vault=vault,
            )
            corrupt_issuance_handle(result, "ish_" + "e" * 32)
            injected = []

            def fail_once(point):
                injected.append(point)
                if point == "during_vault_close":
                    raise RuntimeError("private one-time close failure")

            with self.assertRaises(BrowserSessionLifecycleError) as caught:
                consume_issued(
                    result,
                    vault=vault,
                    _failure_injector=fail_once,
                )
            self.assertEqual(caught.exception.code, "internal_consistency_failure")
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(result.status, "terminal_failed")
            self.assertEqual(injected, ["during_vault_close"])
            self.assertTrue(vault_is_closed_and_empty(vault))
            close_secret_vault(vault)

    def test_vault_failure_boundaries_clear_entries_and_detach_tracebacks(self):
        token = base64.urlsafe_b64encode(b"D" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"E" * 32).rstrip(b"=").decode("ascii")
        cases = (
            "before_vault_deposit",
            "during_vault_deposit",
            "after_vault_deposit",
        )
        for point in cases:
            with self.subTest(point=point):
                with lifecycle_database(suffix=f"vault-deposit-{point}") as (
                    _path,
                    connection,
                    created,
                ):
                    vault = request_secret_vault()

                    def fail(candidate):
                        if candidate == point:
                            raise RuntimeError("private vault failure")

                    with mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential",
                        side_effect=(token, csrf),
                    ):
                        with self.assertRaises(BrowserSessionLifecycleError) as caught:
                            create_browser_session(
                                connection,
                                create_command(created),
                                secret_vault=vault,
                                _failure_injector=fail,
                            )
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertEqual(
                        marker_hits(traceback_values(caught.exception), token, csrf),
                        [],
                    )
                    self.assertEqual(vault_entry_count(vault), 0)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        1,
                    )
                    close_secret_vault(vault)

        for point in ("during_cookie_formatting", "before_response_return"):
            with self.subTest(point=point):
                with lifecycle_database(suffix=f"vault-consume-{point}") as (
                    _path,
                    connection,
                    created,
                ):
                    vault = request_secret_vault()
                    with mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential",
                        side_effect=(token, csrf),
                    ):
                        result = create_browser_session(
                            connection,
                            create_command(created),
                            secret_vault=vault,
                        )

                    def fail(candidate):
                        if candidate == point:
                            raise RuntimeError("private consume failure")

                    with self.assertRaises(BrowserSessionLifecycleError) as caught:
                        consume_issued(
                            result,
                            vault=vault,
                            _failure_injector=fail,
                        )
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertEqual(
                        marker_hits(traceback_values(caught.exception), token, csrf),
                        [],
                    )
                    self.assertEqual(vault_entry_count(vault), 0)
                    self.assertEqual(result.status, "terminal_failed")
                    self.assertTrue(vault_is_closed_and_empty(vault))
                    with self.assertRaises(BrowserSessionLifecycleError):
                        consume_issued(result, vault=vault)
                    close_secret_vault(vault)

    def test_nested_create_and_rotation_require_post_commit_finalization(self):
        with lifecycle_database(suffix="nested-post-commit") as (
            _path,
            connection,
            created,
        ):
            create_vault = request_secret_vault()
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "nested-create", "https://example.test/careers"),
            )
            created_result = create_browser_session(
                connection,
                create_command(created),
                secret_vault=create_vault,
            )
            self.assertEqual(created_result.status, "pending_commit")
            with self.assertRaises(BrowserSessionLifecycleError):
                finalize_issued(connection, created_result, vault=create_vault)
            connection.commit()
            finalize_issued(connection, created_result, vault=create_vault)
            self.assertEqual(created_result.status, "issued")
            consume_issued(created_result, vault=create_vault)

            predecessor = session_row(connection, key="browser-session-create-001")
            rotate_vault = request_secret_vault()
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "nested-rotate", "https://example.test/careers"),
            )
            rotated = rotate_browser_session(
                connection,
                rotate_command(created.user.user_id, predecessor["session_id"]),
                secret_vault=rotate_vault,
            )
            self.assertEqual(rotated.status, "pending_commit")
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(rotated, vault=rotate_vault, now=NOW + timedelta(minutes=5))
            connection.commit()
            finalize_issued(connection, rotated, vault=rotate_vault)
            response = consume_issued(
                rotated,
                vault=rotate_vault,
                now=NOW + timedelta(minutes=5),
            )
            self.assertIn("Max-Age=3600", response.set_cookie_header)
            close_secret_vault(create_vault)
            close_secret_vault(rotate_vault)

    def test_replay_has_no_handle_and_never_recovers_a_vault_entry(self):
        with lifecycle_database(suffix="vault-replay") as (_path, connection, created):
            command = create_command(created)
            first_vault = request_secret_vault()
            first = create_browser_session(
                connection,
                command,
                secret_vault=first_vault,
            )
            self.assertEqual(vault_entry_count(first_vault), 1)
            consume_issued(first, vault=first_vault)

            replay_vault = request_secret_vault()
            with mock.patch(
                "wahojobs.browser_session_lifecycle.secrets.token_bytes"
            ) as generator:
                replay = create_browser_session(
                    connection,
                    command,
                    secret_vault=replay_vault,
                )
            generator.assert_not_called()
            self.assertEqual(replay.status, "already_completed")
            self.assertIsNone(replay._issuance_handle)
            self.assertEqual(vault_entry_count(replay_vault), 0)
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(replay, vault=replay_vault)
            close_secret_vault(first_vault)
            close_secret_vault(replay_vault)

    def test_vault_handle_collision_is_bounded_and_nonsecret(self):
        with lifecycle_database(suffix="vault-handle-collision") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            result = create_browser_session(
                connection,
                create_command(created),
                secret_vault=vault,
            )
            handle = result._issuance_handle
            self.assertRegex(handle, r"^ish_[0-9a-f]{32}$")
            self.assertNotEqual(handle, session_row(connection, key="browser-session-create-001")["session_id"])
            with vault._lock, mock.patch(
                "wahojobs.browser_session_lifecycle.secrets.token_hex",
                return_value=handle.removeprefix("ish_"),
            ):
                with self.assertRaises(BrowserSessionLifecycleError):
                    vault._new_handle_locked()
            self.assertEqual(vault_entry_count(vault), 1)
            close_secret_vault(vault)

    def test_close_and_response_construction_failures_leave_no_secret_state(self):
        token = base64.urlsafe_b64encode(b"Z" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"Q" * 32).rstrip(b"=").decode("ascii")
        with lifecycle_database(suffix="vault-close-failure") as (
            _path,
            connection,
            created,
        ):
            close_vault = request_secret_vault()
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                create_browser_session(
                    connection,
                    create_command(created),
                    secret_vault=close_vault,
                )
            secret_buffers = [
                value
                for value in recursively_reachable_objects(close_vault)
                if type(value) is bytearray
            ]

            def fail_close(point):
                if point == "during_vault_close":
                    raise RuntimeError("private close failure")

            with self.assertRaises(BrowserSessionLifecycleError) as closed:
                close_secret_vault(close_vault, _failure_injector=fail_close)
            self.assertIsNone(closed.exception.__cause__)
            self.assertIsNone(closed.exception.__context__)
            self.assertEqual(vault_entry_count(close_vault), 0)
            self.assertTrue(all(set(buffer) <= {0} for buffer in secret_buffers))

            response_vault = request_secret_vault()
            response_result = create_browser_session(
                connection,
                create_command(created, key="browser-session-create-response-failure"),
                secret_vault=response_vault,
            )
            with mock.patch(
                "wahojobs.browser_session_lifecycle.ConsumedSessionResponse",
                side_effect=RuntimeError("private response failure"),
            ):
                with self.assertRaises(BrowserSessionLifecycleError) as response_error:
                    consume_issued(response_result, vault=response_vault)
            self.assertIsNone(response_error.exception.__cause__)
            self.assertIsNone(response_error.exception.__context__)
            self.assertEqual(vault_entry_count(response_vault), 0)
            self.assertIsNone(response_result._issuance_handle)
            self.assertEqual(response_result.status, "terminal_failed")
            self.assertTrue(vault_is_closed_and_empty(response_vault))
            close_secret_vault(response_vault)

    def test_nested_vault_deposit_failures_rollback_lifecycle_writes_only(self):
        for operation in ("create", "rotate"):
            for point in (
                "before_vault_deposit",
                "during_vault_deposit",
                "after_vault_deposit",
            ):
                with self.subTest(operation=operation, point=point):
                    with lifecycle_database(suffix=f"nested-vault-{operation}-{point}") as (
                        _path,
                        connection,
                        created,
                    ):
                        predecessor = None
                        if operation == "rotate":
                            issued = create_browser_session(
                                connection,
                                create_command(created),
                            )
                            consume_issued(issued)
                            predecessor = session_row(
                                connection,
                                key="browser-session-create-001",
                            )
                        connection.execute(
                            "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                            ("Caller", "nested-vault", "https://example.test/careers"),
                        )
                        vault = request_secret_vault()

                        def fail(candidate):
                            if candidate == point:
                                raise RuntimeError("private nested vault failure")

                        with self.assertRaises(BrowserSessionLifecycleError):
                            if operation == "create":
                                create_browser_session(
                                    connection,
                                    create_command(created),
                                    secret_vault=vault,
                                    _failure_injector=fail,
                                )
                            else:
                                rotate_browser_session(
                                    connection,
                                    rotate_command(
                                        created.user.user_id,
                                        predecessor["session_id"],
                                    ),
                                    secret_vault=vault,
                                    _failure_injector=fail,
                                )
                        self.assertTrue(connection.in_transaction)
                        self.assertEqual(vault_entry_count(vault), 0)
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM account_sessions"
                            ).fetchone()[0],
                            0 if operation == "create" else 1,
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM account_session_rotations"
                            ).fetchone()[0],
                            0,
                        )
                        connection.commit()
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM companies WHERE slug = 'nested-vault'"
                            ).fetchone()[0],
                            1,
                        )
                        close_secret_vault(vault)

    def test_secret_failure_is_absent_from_public_traceback_locals(self):
        token = base64.urlsafe_b64encode(b"S" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"R" * 32).rstrip(b"=").decode("ascii")
        raw_markers = (base64.urlsafe_b64decode(token + "="), base64.urlsafe_b64decode(csrf + "="))
        cases = {
            "create": (
                "after_credential_generation",
                "after_session_insert",
                "after_root_lineage_insert",
                "after_verification",
                "before_commit_or_release",
            ),
            "rotate": (
                "after_credential_generation",
                "after_replacement_insert",
                "after_predecessor_update",
                "after_rotation_edge_insert",
                "after_verification",
                "before_commit_or_release",
            ),
        }
        for operation, points in cases.items():
            for point in points:
                with self.subTest(operation=operation, point=point):
                    with lifecycle_database(suffix=f"traceback-{operation}-{point}") as (
                        _path,
                        connection,
                        created,
                    ):
                        predecessor = None
                        if operation == "rotate":
                            issued = create_browser_session(
                                connection,
                                create_command(created),
                            )
                            consume_issued(issued)
                            predecessor = session_row(
                                connection,
                                key="browser-session-create-001",
                            )

                        def fail(candidate):
                            if candidate == point:
                                raise RuntimeError("injected")

                        with mock.patch(
                            "wahojobs.browser_session_lifecycle._generate_credential",
                            side_effect=(token, csrf),
                        ):
                            with self.assertRaises(BrowserSessionLifecycleError) as caught:
                                if operation == "create":
                                    create_browser_session(
                                        connection,
                                        create_command(created),
                                        _failure_injector=fail,
                                    )
                                else:
                                    rotate_browser_session(
                                        connection,
                                        rotate_command(
                                            created.user.user_id,
                                            predecessor["session_id"],
                                        ),
                                        _failure_injector=fail,
                                    )
                        error = caught.exception
                        self.assertIsNone(error.__cause__)
                        self.assertIsNone(error.__context__)
                        traceback_cursor = error.__traceback__
                        while traceback_cursor is not None:
                            for value in traceback_cursor.tb_frame.f_locals.values():
                                rendered = repr(value)
                                self.assertNotIn(token, rendered)
                                self.assertNotIn(csrf, rendered)
                                for raw_marker in raw_markers:
                                    self.assertNotEqual(value, raw_marker)
                                    self.assertNotEqual(value, bytearray(raw_marker))
                            traceback_cursor = traceback_cursor.tb_next
                        expected_sessions = 0 if operation == "create" else 1
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM account_sessions"
                            ).fetchone()[0],
                            expected_sessions,
                        )

    def test_cookie_uses_effective_expiry_and_response_time(self):
        with lifecycle_database(suffix="cookie-effective-expiry") as (
            _path,
            connection,
            created,
        ):
            immediate = create_browser_session(connection, create_command(created))
            immediate_response = consume_issued(immediate, now=NOW)
            self.assertIn("Max-Age=3600", immediate_response.set_cookie_header)
            self.assertIn(
                "Expires=Tue, 21 Jul 2026 13:00:00 GMT",
                immediate_response.set_cookie_header,
            )

            delayed = create_browser_session(
                connection,
                create_command(created, key="browser-session-create-delayed"),
            )
            delayed_response = consume_issued(
                delayed,
                now=NOW + timedelta(minutes=30),
            )
            self.assertIn("Max-Age=1800", delayed_response.set_cookie_header)
            self.assertNotIn("Max-Age=604800", delayed_response.set_cookie_header)

            multiday = create_browser_session(
                connection,
                create_command(
                    created,
                    key="browser-session-create-multiday",
                    idle_ttl=timedelta(days=30),
                    absolute_ttl=timedelta(days=90),
                ),
            )
            multiday_response = consume_issued(multiday, now=NOW)
            self.assertIn("Max-Age=2592000", multiday_response.set_cookie_header)

            boundary = create_browser_session(
                connection,
                create_command(
                    created,
                    key="browser-session-create-boundary",
                    idle_ttl=timedelta(minutes=1),
                    absolute_ttl=timedelta(minutes=1),
                ),
            )
            with self.assertRaises(BrowserSessionLifecycleError) as expired:
                consume_issued(boundary, now=NOW + timedelta(minutes=1))
            self.assertEqual(expired.exception.code, "session_state_conflict")
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(boundary, now=NOW + timedelta(seconds=30))

    def test_stale_create_is_rejected_before_credential_generation(self):
        with lifecycle_database(suffix="stale-create") as (_path, connection, created):
            stale = create_command(
                created,
                absolute_ttl=timedelta(days=7),
            )
            with mock.patch(
                "wahojobs.browser_session_lifecycle.secrets.token_bytes"
            ) as generator:
                with self.assertRaises(BrowserSessionLifecycleError) as rejected:
                    create_browser_session(
                        connection,
                        stale,
                        _clock=lambda: NOW + timedelta(days=8),
                    )
            self.assertEqual(rejected.exception.code, "ineligible_account_or_identity")
            generator.assert_not_called()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

            equality = create_command(
                created,
                key="browser-session-create-expiry-equality",
                idle_ttl=timedelta(minutes=1),
                absolute_ttl=timedelta(minutes=1),
            )
            with mock.patch(
                "wahojobs.browser_session_lifecycle.secrets.token_bytes"
            ) as equality_generator:
                with self.assertRaises(BrowserSessionLifecycleError):
                    create_browser_session(
                        connection,
                        equality,
                        _clock=lambda: NOW + timedelta(minutes=1),
                    )
            equality_generator.assert_not_called()

            invalid_now = create_command(
                created,
                key="browser-session-create-invalid-now",
            )
            with self.assertRaises(BrowserSessionLifecycleError) as invalid:
                create_browser_session(
                    connection,
                    invalid_now,
                    _clock=lambda: NOW.replace(microsecond=1),
                )
            self.assertEqual(invalid.exception.code, "internal_consistency_failure")

            one_second = create_command(
                created,
                key="browser-session-create-one-second",
                idle_ttl=timedelta(minutes=1),
                absolute_ttl=timedelta(minutes=1),
            )
            result = create_browser_session(
                connection,
                one_second,
                _clock=lambda: NOW + timedelta(seconds=59),
            )
            response = consume_issued(result, now=NOW + timedelta(seconds=59))
            self.assertIn("Max-Age=1", response.set_cookie_header)

    def test_expired_rotation_is_rejected_before_credential_generation(self):
        with lifecycle_database(suffix="stale-rotation") as (_path, connection, created):
            create_browser_session(
                connection,
                create_command(
                    created,
                    idle_ttl=timedelta(hours=1),
                    absolute_ttl=timedelta(hours=1),
                ),
            )
            predecessor = session_row(connection, key="browser-session-create-001")
            command = rotate_command(
                created.user.user_id,
                predecessor["session_id"],
                accepted_at=NOW + timedelta(minutes=30),
            )
            with mock.patch(
                "wahojobs.browser_session_lifecycle.secrets.token_bytes"
            ) as generator:
                with self.assertRaises(BrowserSessionLifecycleError) as rejected:
                    rotate_browser_session(
                        connection,
                        command,
                        _clock=lambda: NOW + timedelta(hours=1),
                    )
            self.assertEqual(rejected.exception.code, "session_state_conflict")
            generator.assert_not_called()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

    def test_exact_creation_replay_returns_no_prior_or_replacement_secret(self):
        with lifecycle_database(suffix="create-replay") as (_path, connection, created):
            first = create_browser_session(connection, create_command(created))
            first_response = consume_issued(first)
            first_header = first_response.set_cookie_header
            first_csrf = first_response.csrf_credential
            replay = create_browser_session(connection, create_command(created))
            self.assertEqual(replay.status, "already_completed")
            with self.assertRaises(BrowserSessionLifecycleError) as token_error:
                consume_issued(replay)
            self.assertEqual(token_error.exception.code, "already_completed")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )
            self.assertNotIn(token_from_cookie_header(first_header), repr(replay))
            self.assertNotIn(first_csrf, repr(replay))
            later_replay = create_browser_session(
                connection,
                create_command(created),
                _clock=lambda: NOW + timedelta(days=8),
            )
            self.assertEqual(later_replay.status, "already_completed")
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(later_replay, now=NOW + timedelta(days=8))

    def test_exact_replay_allows_later_valid_identity_authentication_time(self):
        with lifecycle_database(suffix="create-replay-later-auth") as (
            _path,
            connection,
            created,
        ):
            command = create_command(created)
            create_browser_session(connection, command, _clock=lambda: NOW)
            connection.execute(
                "UPDATE auth_identities SET last_authenticated_at = ? "
                "WHERE auth_identity_id = ?",
                (
                    (NOW + timedelta(minutes=10)).isoformat(),
                    created.identity.auth_identity_id,
                ),
            )
            connection.commit()
            replay = create_browser_session(
                connection,
                command,
                _clock=lambda: NOW + timedelta(minutes=11),
            )
            self.assertEqual(replay.status, "already_completed")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

    def test_changed_creation_request_and_cross_account_key_conflict(self):
        with lifecycle_database(suffix="create-conflict") as (_path, connection, created):
            create_browser_session(connection, create_command(created))
            changed = create_command(created, idle_ttl=timedelta(hours=2))
            with self.assertRaises(BrowserSessionLifecycleError) as conflict:
                create_browser_session(connection, changed)
            self.assertEqual(conflict.exception.code, "idempotency_conflict")

            _invitation, second = create_user(
                connection,
                suffix="create-conflict-second",
                now=NOW,
            )
            cross_account = create_command(second, key="browser-session-create-001")
            with self.assertRaises(BrowserSessionLifecycleError) as cross_conflict:
                create_browser_session(connection, cross_account)
            self.assertEqual(cross_conflict.exception.code, "idempotency_conflict")
            self.assertNotIn(created.user.user_id, str(cross_conflict.exception))

    def test_creation_revalidates_account_and_identity(self):
        scenarios = ("missing_account", "inactive_account", "missing_identity", "disabled_identity")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                with lifecycle_database(suffix=f"eligible-{scenario}") as (
                    _path,
                    connection,
                    created,
                ):
                    if scenario == "missing_account":
                        command = create_command(
                            created,
                            account_id="usr_11111111111111111111111111111111",
                            supporting_identity_id="auth_11111111111111111111111111111111",
                        )
                    elif scenario == "inactive_account":
                        connection.execute(
                            "UPDATE users SET lifecycle_status = 'suspended' WHERE user_id = ?",
                            (created.user.user_id,),
                        )
                    elif scenario == "missing_identity":
                        connection.execute(
                            "DELETE FROM auth_identities WHERE auth_identity_id = ?",
                            (created.identity.auth_identity_id,),
                        )
                    else:
                        connection.execute(
                            "UPDATE auth_identities SET disabled_at = ? WHERE auth_identity_id = ?",
                            (NOW.isoformat(), created.identity.auth_identity_id),
                        )
                    if scenario != "missing_account":
                        command = create_command(created)
                    connection.commit()
                    with self.assertRaises(BrowserSessionLifecycleError) as error:
                        create_browser_session(connection, command)
                    self.assertEqual(error.exception.code, "ineligible_account_or_identity")
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        0,
                    )

    def test_creation_rejects_malformed_identity_as_internal_failure(self):
        with lifecycle_database(suffix="identity-malformed") as (_path, connection, created):
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE auth_identities SET last_authenticated_at = 'not-a-time' "
                "WHERE auth_identity_id = ?",
                (created.identity.auth_identity_id,),
            )
            connection.execute("PRAGMA ignore_check_constraints = OFF")
            connection.commit()
            with self.assertRaises(BrowserSessionLifecycleError) as error:
                create_browser_session(connection, create_command(created))
            self.assertEqual(error.exception.code, "internal_consistency_failure")
            self.assertIsNone(error.exception.__cause__)
            self.assertIsNone(error.exception.__context__)

    def test_creation_failure_injection_rolls_back_every_boundary(self):
        points = (
            "after_idempotency_lookup",
            "after_credential_generation",
            "after_session_insert",
            "after_root_lineage_insert",
            "after_verification",
            "before_commit_or_release",
        )
        for point in points:
            with self.subTest(point=point):
                with lifecycle_database(suffix=f"create-failure-{point}") as (
                    _path,
                    connection,
                    created,
                ):
                    def fail(candidate):
                        if candidate == point:
                            raise RuntimeError("private failure with credential-looking text")

                    with self.assertRaises(BrowserSessionLifecycleError) as error:
                        create_browser_session(
                            connection,
                            create_command(created),
                            _failure_injector=fail,
                        )
                    self.assertEqual(error.exception.code, "internal_consistency_failure")
                    self.assertIsNone(error.exception.__cause__)
                    self.assertIsNone(error.exception.__context__)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        0,
                    )
                    self.assertFalse(connection.in_transaction)

    def test_caller_transaction_uses_savepoint_and_preserves_unrelated_work(self):
        with lifecycle_database(suffix="caller-transaction") as (_path, connection, created):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "caller-unrelated", "https://example.test/careers"),
            )
            result = create_browser_session(connection, create_command(created))
            self.assertTrue(connection.in_transaction)
            self.assertEqual(result.status, "pending_commit")
            pending_vault = vault_for_result(result)
            self.assertEqual(vault_entry_count(pending_vault), 1)
            connection.rollback()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            with self.assertRaises(BrowserSessionLifecycleError):
                finalize_issued(connection, result, vault=pending_vault)
            self.assertEqual(vault_entry_count(pending_vault), 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies WHERE slug = 'caller-unrelated'"
                ).fetchone()[0],
                0,
            )

            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "caller-preserved", "https://example.test/careers"),
            )

            def fail(point):
                if point == "after_session_insert":
                    raise RuntimeError("stop")

            with self.assertRaises(BrowserSessionLifecycleError):
                create_browser_session(
                    connection,
                    create_command(created, key="browser-session-create-failed"),
                    _failure_injector=fail,
                )
            self.assertTrue(connection.in_transaction)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies WHERE slug = 'caller-preserved'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            connection.commit()

    def test_nested_savepoint_commit_and_interrupt_cleanup_preserve_caller_scope(self):
        with lifecycle_database(suffix="nested-savepoints") as (_path, connection, created):
            connection.execute("SAVEPOINT caller_owned")
            result = create_browser_session(connection, create_command(created))
            self.assertEqual(result.status, "pending_commit")
            pending_vault = vault_for_result(result)
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(result, vault=pending_vault)
            connection.execute("RELEASE SAVEPOINT caller_owned")
            self.assertFalse(connection.in_transaction)
            finalized = finalize_issued(connection, result, vault=pending_vault)
            self.assertIs(finalized, result)
            self.assertEqual(result.status, "issued")
            consume_issued(result, vault=pending_vault)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

        with lifecycle_database(suffix="interrupt-cleanup") as (_path, connection, created):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "caller-interrupt", "https://example.test/careers"),
            )

            def interrupt(point):
                if point == "after_session_insert":
                    raise KeyboardInterrupt()

            with self.assertRaises(BrowserSessionLifecycleError) as interrupted:
                create_browser_session(
                    connection,
                    create_command(created),
                    _failure_injector=interrupt,
                )
            self.assertEqual(interrupted.exception.code, "internal_consistency_failure")
            self.assertTrue(connection.in_transaction)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies WHERE slug = 'caller-interrupt'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            connection.rollback()

    def test_top_level_rollback_retries_and_fixed_sql_fallback_remove_partial_state(self):
        failure_types = (KeyboardInterrupt, sqlite3.OperationalError)
        for failure_type in failure_types:
            with self.subTest(failure=failure_type.__name__):
                with lifecycle_database(suffix=f"rollback-once-{failure_type.__name__}") as (
                    _path,
                    connection,
                    created,
                ):
                    calls = 0

                    def rollback_once(candidate):
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            raise failure_type("injected")
                        candidate.rollback()

                    def fail(point):
                        if point == "after_session_insert":
                            raise RuntimeError("stop")

                    with mock.patch(
                        "wahojobs.browser_session_lifecycle._connection_rollback",
                        side_effect=rollback_once,
                    ):
                        with self.assertRaises(BrowserSessionLifecycleError):
                            create_browser_session(
                                connection,
                                create_command(created),
                                _failure_injector=fail,
                            )
                    self.assertEqual(calls, 2)
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        0,
                    )
                    self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

        with lifecycle_database(suffix="rollback-fixed-sql") as (
            _path,
            connection,
            created,
        ):
            def fail(point):
                if point == "after_session_insert":
                    raise RuntimeError("stop")

            with mock.patch(
                "wahojobs.browser_session_lifecycle._connection_rollback",
                side_effect=(KeyboardInterrupt(), sqlite3.OperationalError("injected")),
            ):
                with self.assertRaises(BrowserSessionLifecycleError):
                    create_browser_session(
                        connection,
                        create_command(created),
                        _failure_injector=fail,
                    )
            self.assertFalse(connection.in_transaction)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

    def test_one_time_sqlite_rollback_denial_cannot_leave_commit_able_session(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                with lifecycle_database(suffix=f"rollback-denial-{nested}") as (
                    _path,
                    connection,
                    created,
                ):
                    if nested:
                        connection.execute(
                            "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                            ("Caller", "caller-rollback-denial", "https://example.test/careers"),
                        )
                    denied = False

                    def authorizer(action, argument, _arg2, _database, _trigger):
                        nonlocal denied
                        is_target = (
                            not nested
                            and action == sqlite3.SQLITE_TRANSACTION
                            and str(argument).upper() == "ROLLBACK"
                        ) or (
                            nested
                            and action == sqlite3.SQLITE_SAVEPOINT
                            and str(argument).upper() == "ROLLBACK"
                        )
                        if is_target and not denied:
                            denied = True
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    def fail(point):
                        if point == "after_session_insert":
                            raise RuntimeError("stop")

                    connection.set_authorizer(authorizer)
                    try:
                        with self.assertRaises(BrowserSessionLifecycleError):
                            create_browser_session(
                                connection,
                                create_command(created),
                                _failure_injector=fail,
                            )
                    finally:
                        connection.set_authorizer(None)
                    self.assertTrue(denied)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        0,
                    )
                    if nested:
                        self.assertTrue(connection.in_transaction)
                        connection.commit()
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM companies "
                                "WHERE slug = 'caller-rollback-denial'"
                            ).fetchone()[0],
                            1,
                        )
                    else:
                        self.assertFalse(connection.in_transaction)
                    connection.commit()
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        0,
                    )

        with lifecycle_database(suffix="release-denial") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "caller-release-denial", "https://example.test/careers"),
            )
            denied = False

            def authorizer(action, argument, _arg2, _database, _trigger):
                nonlocal denied
                if (
                    action == sqlite3.SQLITE_SAVEPOINT
                    and str(argument).upper() == "RELEASE"
                    and not denied
                ):
                    denied = True
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            def fail(point):
                if point == "after_session_insert":
                    raise RuntimeError("stop")

            connection.set_authorizer(authorizer)
            try:
                with self.assertRaises(BrowserSessionLifecycleError):
                    create_browser_session(
                        connection,
                        create_command(created),
                        _failure_injector=fail,
                    )
            finally:
                connection.set_authorizer(None)
            self.assertTrue(denied)
            self.assertTrue(connection.in_transaction)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            connection.commit()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies WHERE slug = 'caller-release-denial'"
                ).fetchone()[0],
                1,
            )

    def test_one_time_top_level_rollback_denial_cleans_rotation_and_revocation(self):
        cases = (
            ("rotate", "after_replacement_insert"),
            ("rotate", "after_predecessor_update"),
            ("rotate", "after_rotation_edge_insert"),
            ("revoke", "after_session_update"),
        )
        for operation, point in cases:
            with self.subTest(operation=operation, point=point):
                with lifecycle_database(suffix=f"rollback-{operation}-{point}") as (
                    _path,
                    connection,
                    created,
                ):
                    issued = create_browser_session(connection, create_command(created))
                    consume_issued(issued)
                    predecessor = session_row(
                        connection,
                        key="browser-session-create-001",
                    )
                    denied = False

                    def authorizer(action, argument, _arg2, _database, _trigger):
                        nonlocal denied
                        if (
                            action == sqlite3.SQLITE_TRANSACTION
                            and str(argument).upper() == "ROLLBACK"
                            and not denied
                        ):
                            denied = True
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    def fail(candidate):
                        if candidate == point:
                            raise RuntimeError("stop")

                    connection.set_authorizer(authorizer)
                    try:
                        with self.assertRaises(BrowserSessionLifecycleError):
                            if operation == "rotate":
                                rotate_browser_session(
                                    connection,
                                    rotate_command(
                                        created.user.user_id,
                                        predecessor["session_id"],
                                    ),
                                    _failure_injector=fail,
                                )
                            else:
                                revoke_browser_session(
                                    connection,
                                    revoke_command(
                                        created.user.user_id,
                                        predecessor["session_id"],
                                    ),
                                    _failure_injector=fail,
                                )
                    finally:
                        connection.set_authorizer(None)
                    self.assertTrue(denied)
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM account_session_rotations"
                        ).fetchone()[0],
                        0,
                    )
                    stored = session_row(
                        connection,
                        session_id=predecessor["session_id"],
                    )
                    self.assertEqual(stored["session_version"], 1)
                    self.assertIsNone(stored["revoked_at"])
                    self.assertIsNone(stored["rotated_at"])

    def test_service_uses_only_the_supplied_connection(self):
        with lifecycle_database(suffix="one-connection") as (_path, connection, created):
            with mock.patch(
                "wahojobs.browser_session_lifecycle.sqlite3.connect",
                side_effect=AssertionError("auxiliary connection"),
            ) as connector:
                result = create_browser_session(connection, create_command(created))
                consume_issued(result)
                predecessor = session_row(
                    connection,
                    key="browser-session-create-001",
                )
                rotated = rotate_browser_session(
                    connection,
                    rotate_command(created.user.user_id, predecessor["session_id"]),
                )
                consume_issued(rotated, now=NOW + timedelta(minutes=5))
                replacement = session_row(
                    connection,
                    key="browser-session-rotate-001",
                )
                revoked = revoke_browser_session(
                    connection,
                    revoke_command(
                        created.user.user_id,
                        replacement["session_id"],
                        accepted_at=NOW + timedelta(minutes=10),
                    ),
                )
            self.assertEqual(result.status, "consumed")
            self.assertEqual(rotated.status, "consumed")
            self.assertEqual(revoked.status, "revoked")
            connector.assert_not_called()

    def test_generation_collision_is_bounded_and_leaves_no_state(self):
        with lifecycle_database(suffix="credential-collision") as (
            _path,
            connection,
            created,
        ):
            with mock.patch(
                "wahojobs.browser_session_lifecycle.secrets.token_bytes",
                return_value=b"x" * 32,
            ) as generator:
                with self.assertRaises(BrowserSessionLifecycleError) as error:
                    create_browser_session(connection, create_command(created))
            self.assertEqual(error.exception.code, "internal_consistency_failure")
            self.assertEqual(generator.call_count, 12)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

    def test_cross_role_credential_digest_collision_is_regenerated(self):
        with lifecycle_database(suffix="cross-role-collision") as (
            _path,
            connection,
            created,
        ):
            first = create_browser_session(connection, create_command(created))
            first_response = consume_issued(first)
            first_csrf = first_response.csrf_credential
            replacement_token = base64.urlsafe_b64encode(b"t" * 32).rstrip(b"=").decode("ascii")
            replacement_csrf = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
            generated = [
                first_csrf,
                base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode("ascii"),
                replacement_token,
                replacement_csrf,
            ]
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=generated,
            ):
                result = create_browser_session(
                    connection,
                    create_command(
                        created,
                        key="browser-session-create-cross-role",
                    ),
            )
            self.assertEqual(result.status, "issued")
            response = consume_issued(result)
            self.assertEqual(
                token_from_cookie_header(response.set_cookie_header),
                replacement_token,
            )
            self.assertEqual(response.csrf_credential, replacement_csrf)

    def test_valid_rotation_is_atomic_and_preserves_absolute_expiry(self):
        with lifecycle_database(suffix="rotate-valid") as (_path, connection, created):
            original = create_browser_session(connection, create_command(created))
            consume_issued(original)
            predecessor = session_row(connection, key="browser-session-create-001")
            result = rotate_browser_session(
                connection,
                rotate_command(created.user.user_id, predecessor["session_id"]),
            )
            self.assertEqual(result.status, "issued")
            replacement = session_row(connection, key="browser-session-rotate-001")
            predecessor = session_row(connection, session_id=predecessor["session_id"])
            self.assertEqual(predecessor["session_version"], 2)
            self.assertEqual(predecessor["revoke_reason"], "session_rotated")
            self.assertEqual(predecessor["rotated_at"], predecessor["revoked_at"])
            self.assertEqual(replacement["session_version"], 1)
            self.assertEqual(replacement["absolute_expires_at"], predecessor["absolute_expires_at"])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_session_rotations").fetchone()[0],
                1,
            )
            edge = connection.execute("SELECT * FROM account_session_rotations").fetchone()
            self.assertEqual(edge["predecessor_session_id"], predecessor["session_id"])
            self.assertEqual(edge["replacement_session_id"], replacement["session_id"])
            response = consume_issued(result, now=NOW + timedelta(minutes=5))
            self.assertRegex(token_from_cookie_header(response.set_cookie_header), r"^[A-Za-z0-9_-]{43}$")
            self.assertRegex(response.csrf_credential, r"^[A-Za-z0-9_-]{43}$")

    def test_rotation_clamps_idle_expiry_and_replay_has_no_credentials(self):
        with lifecycle_database(suffix="rotate-clamp") as (_path, connection, created):
            create_browser_session(
                connection,
                create_command(
                    created,
                    idle_ttl=timedelta(hours=2),
                    absolute_ttl=timedelta(hours=2),
                ),
            )
            predecessor = session_row(connection, key="browser-session-create-001")
            command = rotate_command(
                created.user.user_id,
                predecessor["session_id"],
                accepted_at=NOW + timedelta(hours=1, minutes=30),
                idle_ttl=timedelta(hours=1),
            )
            first = rotate_browser_session(connection, command)
            replacement = session_row(connection, key="browser-session-rotate-001")
            self.assertEqual(replacement["idle_expires_at"], predecessor["absolute_expires_at"])
            replay = rotate_browser_session(connection, command)
            self.assertEqual(replay.status, "already_completed")
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_issued(replay, now=NOW + timedelta(hours=1, minutes=30))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                2,
            )
            clamped_response = consume_issued(
                first,
                now=NOW + timedelta(hours=1, minutes=30),
            )
            self.assertIn("Max-Age=1800", clamped_response.set_cookie_header)

    def test_rotation_allows_32_edges_and_rejects_the_33rd(self):
        with lifecycle_database(suffix="rotation-depth") as (_path, connection, created):
            issued = create_browser_session(connection, create_command(created))
            consume_issued(issued)
            current = session_row(connection, key="browser-session-create-001")
            for index in range(1, 33):
                issued = rotate_browser_session(
                    connection,
                    rotate_command(
                        created.user.user_id,
                        current["session_id"],
                        key=f"browser-session-rotate-{index:03d}",
                        accepted_at=NOW + timedelta(minutes=index),
                    ),
                )
                consume_issued(issued, now=NOW + timedelta(minutes=index))
                current = session_row(
                    connection,
                    key=f"browser-session-rotate-{index:03d}",
                )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_session_rotations"
                ).fetchone()[0],
                32,
            )
            with self.assertRaises(BrowserSessionLifecycleError) as error:
                rotate_browser_session(
                    connection,
                    rotate_command(
                        created.user.user_id,
                        current["session_id"],
                        key="browser-session-rotate-033",
                        accepted_at=NOW + timedelta(minutes=33),
                    ),
                )
            self.assertEqual(error.exception.code, "session_state_conflict")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_session_rotations"
                ).fetchone()[0],
                32,
            )

    def test_rotation_and_revocation_reject_cross_account_session_authority(self):
        with lifecycle_database(suffix="cross-account-session") as (
            _path,
            connection,
            created,
        ):
            create_browser_session(connection, create_command(created))
            target = session_row(connection, key="browser-session-create-001")
            _invitation, second = create_user(
                connection,
                suffix="cross-account-session-second",
                now=NOW,
            )
            for operation in (
                lambda: rotate_browser_session(
                    connection,
                    rotate_command(second.user.user_id, target["session_id"]),
                ),
                lambda: revoke_browser_session(
                    connection,
                    revoke_command(second.user.user_id, target["session_id"]),
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(BrowserSessionLifecycleError) as error:
                        operation()
                    self.assertEqual(error.exception.code, "internal_consistency_failure")
            stored = session_row(connection, session_id=target["session_id"])
            self.assertEqual(stored["session_version"], 1)
            self.assertIsNone(stored["revoked_at"])

    def test_rotation_stale_and_changed_replay_are_sanitized(self):
        with lifecycle_database(suffix="rotate-conflicts") as (_path, connection, created):
            create_browser_session(connection, create_command(created))
            predecessor = session_row(connection, key="browser-session-create-001")
            with self.assertRaises(BrowserSessionLifecycleError) as stale:
                rotate_browser_session(
                    connection,
                    rotate_command(
                        created.user.user_id,
                        predecessor["session_id"],
                        expected_session_version=2,
                    ),
                )
            self.assertEqual(stale.exception.code, "stale_session")

            rotate_browser_session(
                connection,
                rotate_command(created.user.user_id, predecessor["session_id"]),
            )
            changed = rotate_command(
                created.user.user_id,
                predecessor["session_id"],
                idle_ttl=timedelta(hours=2),
            )
            with self.assertRaises(BrowserSessionLifecycleError) as conflict:
                rotate_browser_session(connection, changed)
            self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_rotation_failure_injection_leaves_no_partial_lineage(self):
        points = (
            "after_replacement_insert",
            "after_predecessor_update",
            "after_rotation_edge_insert",
            "after_verification",
            "before_commit_or_release",
        )
        for point in points:
            with self.subTest(point=point):
                with lifecycle_database(suffix=f"rotate-failure-{point}") as (
                    _path,
                    connection,
                    created,
                ):
                    create_browser_session(connection, create_command(created))
                    predecessor = session_row(connection, key="browser-session-create-001")

                    def fail(candidate):
                        if candidate == point:
                            raise RuntimeError("stop")

                    with self.assertRaises(BrowserSessionLifecycleError):
                        rotate_browser_session(
                            connection,
                            rotate_command(created.user.user_id, predecessor["session_id"]),
                            _failure_injector=fail,
                        )
                    stored = session_row(connection, session_id=predecessor["session_id"])
                    self.assertEqual(stored["session_version"], 1)
                    self.assertIsNone(stored["revoked_at"])
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM account_session_rotations"
                        ).fetchone()[0],
                        0,
                    )

    def test_valid_revocation_is_single_session_idempotent_and_blocks_authentication(self):
        with lifecycle_database(suffix="revoke-valid") as (path, connection, created):
            first = create_browser_session(connection, create_command(created))
            token = token_from_cookie_header(consume_issued(first).set_cookie_header)
            target = session_row(connection, key="browser-session-create-001")
            second = create_browser_session(
                connection,
                create_command(created, key="browser-session-create-unrelated"),
            )
            consume_issued(second)
            command = revoke_command(created.user.user_id, target["session_id"])
            result = revoke_browser_session(connection, command)
            self.assertEqual(result.status, "revoked")
            replay = revoke_browser_session(connection, command)
            self.assertEqual(replay.status, "already_completed")
            revoked = session_row(connection, session_id=target["session_id"])
            unrelated = session_row(connection, key="browser-session-create-unrelated")
            self.assertEqual(revoked["session_version"], 2)
            self.assertEqual(revoked["revoke_reason"], "explicit_revoke")
            self.assertIsNone(revoked["rotated_at"])
            self.assertEqual(unrelated["session_version"], 1)
            self.assertIsNone(unrelated["revoked_at"])

            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="test",
                clock=lambda: NOW + timedelta(minutes=6),
            )
            with read_only_connection(path) as read_only:
                self.assertIsNone(
                    gateway.authenticate_browser_request(
                        read_only,
                        browser_request(token),
                    )
                )

    def test_revocation_changed_replay_and_rotation_conflict_fail_closed(self):
        with lifecycle_database(suffix="revoke-conflict") as (_path, connection, created):
            create_browser_session(connection, create_command(created))
            target = session_row(connection, key="browser-session-create-001")
            revoke_browser_session(
                connection,
                revoke_command(created.user.user_id, target["session_id"]),
            )
            changed = revoke_command(
                created.user.user_id,
                target["session_id"],
                reason="security_reset",
            )
            with self.assertRaises(BrowserSessionLifecycleError) as conflict:
                revoke_browser_session(connection, changed)
            self.assertEqual(conflict.exception.code, "session_state_conflict")
            with self.assertRaises(BrowserSessionLifecycleError):
                rotate_browser_session(
                    connection,
                    rotate_command(
                        created.user.user_id,
                        target["session_id"],
                        key="browser-session-rotate-revoked",
                    ),
                )

    def test_revocation_failure_injection_rolls_back(self):
        for point in (
            "after_session_update",
            "after_fingerprint_verification",
            "before_commit_or_release",
        ):
            with self.subTest(point=point):
                with lifecycle_database(suffix=f"revoke-failure-{point}") as (
                    _path,
                    connection,
                    created,
                ):
                    create_browser_session(connection, create_command(created))
                    target = session_row(connection, key="browser-session-create-001")

                    def fail(candidate):
                        if candidate == point:
                            raise RuntimeError("stop")

                    with self.assertRaises(BrowserSessionLifecycleError):
                        revoke_browser_session(
                            connection,
                            revoke_command(created.user.user_id, target["session_id"]),
                            _failure_injector=fail,
                        )
                    stored = session_row(connection, session_id=target["session_id"])
                    self.assertEqual(stored["session_version"], 1)
                    self.assertIsNone(stored["revoked_at"])

    def test_every_failure_hook_preserves_nested_caller_transaction(self):
        cases = {
            "create": (
                "after_idempotency_lookup",
                "after_credential_generation",
                "after_session_insert",
                "after_root_lineage_insert",
                "after_verification",
                "before_commit_or_release",
            ),
            "rotate": (
                "after_replacement_insert",
                "after_predecessor_update",
                "after_rotation_edge_insert",
                "after_verification",
                "before_commit_or_release",
            ),
            "revoke": (
                "after_session_update",
                "after_fingerprint_verification",
                "before_commit_or_release",
            ),
        }
        for operation, points in cases.items():
            for point in points:
                with self.subTest(operation=operation, point=point):
                    with lifecycle_database(suffix=f"nested-{operation}-{point}") as (
                        _path,
                        connection,
                        created,
                    ):
                        predecessor = None
                        if operation != "create":
                            issued = create_browser_session(
                                connection,
                                create_command(created),
                            )
                            consume_issued(issued)
                            predecessor = session_row(
                                connection,
                                key="browser-session-create-001",
                            )
                        connection.execute(
                            "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                            ("Caller", "caller-hook", "https://example.test/careers"),
                        )

                        def fail(candidate):
                            if candidate == point:
                                raise RuntimeError("stop")

                        with self.assertRaises(BrowserSessionLifecycleError):
                            if operation == "create":
                                create_browser_session(
                                    connection,
                                    create_command(created),
                                    _failure_injector=fail,
                                )
                            elif operation == "rotate":
                                rotate_browser_session(
                                    connection,
                                    rotate_command(
                                        created.user.user_id,
                                        predecessor["session_id"],
                                    ),
                                    _failure_injector=fail,
                                )
                            else:
                                revoke_browser_session(
                                    connection,
                                    revoke_command(
                                        created.user.user_id,
                                        predecessor["session_id"],
                                    ),
                                    _failure_injector=fail,
                                )
                        self.assertTrue(connection.in_transaction)
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM companies WHERE slug = 'caller-hook'"
                            ).fetchone()[0],
                            1,
                        )
                        expected_sessions = 0 if operation == "create" else 1
                        self.assertEqual(
                            connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                            expected_sessions,
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM account_session_rotations"
                            ).fetchone()[0],
                            0,
                        )
                        if predecessor is not None:
                            stored = session_row(
                                connection,
                                session_id=predecessor["session_id"],
                            )
                            self.assertEqual(stored["session_version"], 1)
                            self.assertIsNone(stored["revoked_at"])
                            self.assertIsNone(stored["rotated_at"])
                        connection.commit()
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM companies WHERE slug = 'caller-hook'"
                            ).fetchone()[0],
                            1,
                        )

    def test_schema_drift_and_read_only_connections_fail_without_writes(self):
        with lifecycle_database(suffix="schema-drift") as (_path, connection, created):
            connection.execute(
                "CREATE INDEX unexpected_session_index ON account_sessions(last_seen_at)"
            )
            connection.commit()
            with self.assertRaises(BrowserSessionLifecycleError) as unavailable:
                create_browser_session(connection, create_command(created))
            self.assertEqual(unavailable.exception.code, "schema_capability_unavailable")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

        with lifecycle_database(suffix="query-only") as (_path, connection, created):
            connection.execute("PRAGMA query_only = ON")
            with self.assertRaises(BrowserSessionLifecycleError) as unavailable:
                create_browser_session(connection, create_command(created))
            self.assertEqual(unavailable.exception.code, "schema_capability_unavailable")

    def test_private_failure_text_is_detached_from_public_error(self):
        marker = "raw-session-token-secret-marker"
        with lifecycle_database(suffix="privacy-error") as (_path, connection, created):
            def fail(point):
                if point == "after_credential_generation":
                    raise RuntimeError(marker)

            with self.assertRaises(BrowserSessionLifecycleError) as caught:
                create_browser_session(
                    connection,
                    create_command(created),
                    _failure_injector=fail,
                )
            error = caught.exception
            self.assertNotIn(marker, str(error))
            self.assertNotIn(marker, repr(error))
            self.assertNotIn(marker, str(error.as_public_dict()))
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)

    def test_database_integrity_and_foreign_keys_remain_clean(self):
        with lifecycle_database(suffix="integrity") as (_path, connection, created):
            first = create_browser_session(connection, create_command(created))
            consume_issued(first)
            predecessor = session_row(connection, key="browser-session-create-001")
            rotated = rotate_browser_session(
                connection,
                rotate_command(created.user.user_id, predecessor["session_id"]),
            )
            consume_issued(rotated, now=NOW + timedelta(minutes=5))
            replacement = session_row(connection, key="browser-session-rotate-001")
            revoke_browser_session(
                connection,
                revoke_command(
                    created.user.user_id,
                    replacement["session_id"],
                    accepted_at=NOW + timedelta(minutes=10),
                ),
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
