import hashlib
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.browser_session_authentication_test_support import (
    AUTHENTICATED_AT,
    REQUEST_AT,
    browser_request,
    guarded_update,
    install_browser_authentication_database,
    read_only_connection,
    seed_browser_session,
)
from tests.persistent_profile_read_authorization_test_support import file_fingerprint
from wahojobs import accounts
import wahojobs.browser_session_authentication as browser_authentication
from wahojobs.browser_session_authentication import (
    BrowserSessionAuthenticationUnavailable,
    DurableBrowserSessionAuthenticationGateway,
    MAX_COOKIE_HEADER_BYTES,
    SESSION_COOKIE_NAME,
)
from wahojobs.persistent_profiles_application import TrustedAuthenticatedBrowserActor


class BrowserSessionAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "browser-auth.sqlite"
        self.writer = install_browser_authentication_database(self.path)
        self.state = seed_browser_session(self.writer)
        self.gateway = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=self.state["environment"],
            clock=lambda: REQUEST_AT,
        )

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def authenticate(self, request=None, *, now=None):
        with read_only_connection(self.path) as connection:
            connection.execute("BEGIN")
            try:
                return self.gateway.authenticate_browser_request(
                    connection,
                    request or browser_request(self.state["session_token"]),
                    now=now,
                )
            finally:
                connection.rollback()

    def assert_unavailable(self, request=None):
        with self.assertRaises(BrowserSessionAuthenticationUnavailable) as caught:
            self.authenticate(request)
        self.assertEqual(str(caught.exception), "Browser authentication is temporarily unavailable.")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def corrupt_session(self, field, value):
        triggers = (
            ("trg_account_sessions_core_immutable",)
            if field in {"session_id", "user_id", "created_at"}
            else ()
        )
        guarded_update(
            self.writer,
            triggers,
            lambda: self.writer.execute(
                f'UPDATE account_sessions SET "{field}" = ? WHERE session_id = ?',
                (value, self.state["session_id"]),
            ),
        )

    def assert_schema_object_unavailable(self, definition, *, suffix):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"unexpected-schema-{suffix}.sqlite"
            writer = install_browser_authentication_database(path)
            state = seed_browser_session(writer, suffix=suffix)
            writer.execute(definition)
            writer.commit()
            writer.close()
            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace=state["environment"],
                clock=lambda: REQUEST_AT,
            )
            with read_only_connection(path) as connection, mock.patch(
                "wahojobs.browser_session_authentication._rows",
                wraps=browser_authentication._rows,
            ) as rows, mock.patch(
                "wahojobs.browser_session_authentication._issue_actor"
            ) as issue:
                with self.assertRaises(
                    BrowserSessionAuthenticationUnavailable
                ) as caught:
                    gateway.authenticate_browser_request(
                        connection,
                        browser_request(state["session_token"]),
                    )
            self.assertEqual(str(caught.exception), "Browser authentication is temporarily unavailable.")
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(rows.call_count, 0)
            self.assertEqual(issue.call_count, 0)

    def test_valid_session_issues_one_sealed_actor_without_session_material(self):
        with mock.patch(
            "wahojobs.browser_session_authentication._issue_actor",
            wraps=__import__(
                "wahojobs.browser_session_authentication",
                fromlist=["_issue_actor"],
            )._issue_actor,
        ) as issue:
            actor = self.authenticate()
        self.assertIs(type(actor), TrustedAuthenticatedBrowserActor)
        self.assertEqual(issue.call_count, 1)
        self.assertEqual(
            actor.account_reference_for_authorization(),
            (self.state["account_id"], self.state["environment"]),
        )
        exposed = repr(actor) + str(actor) + repr(actor.__slots__)
        self.assertNotIn(self.state["session_token"], exposed)
        self.assertNotIn(self.state["session_id"], exposed)

    def test_missing_unknown_and_alternate_credentials_are_unauthenticated(self):
        unknown = "A" * 43
        cases = (
            browser_request(),
            browser_request(extra_headers=(("Cookie", "other=value"),)),
            browser_request(unknown),
            browser_request(extra_headers=(("Authorization", f"Bearer {self.state['session_token']}"),)),
            browser_request(extra_headers=(("X-Account", self.state["account_id"]),)),
        )
        for request in cases:
            with self.subTest(request=repr(request)):
                self.assertIsNone(self.authenticate(request))

    def test_cookie_attack_matrix_is_rejected_without_issuer_call(self):
        token = self.state["session_token"]
        headers = (
            (("Cookie", f"{SESSION_COOKIE_NAME}="),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}; {SESSION_COOKIE_NAME}={token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}"), ("Cookie", "other=value")),
            (("Cookie", f'{SESSION_COOKIE_NAME}="{token}"'),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}%20"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}=é{token[1:]}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token},other"),),
            (("Cookie", "x=" + ("a" * MAX_COOKIE_HEADER_BYTES)),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}\x01"),),
        )
        with mock.patch("wahojobs.browser_session_authentication._issue_actor") as issue:
            for value in headers:
                with self.subTest(value=repr(value)[:80]):
                    self.assertIsNone(self.authenticate(browser_request(extra_headers=value)))
        self.assertEqual(issue.call_count, 0)

    def test_target_cookie_requires_an_exact_assignment_with_narrow_separator_space(self):
        token = self.state["session_token"]
        valid = (
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}"),),
            (("Cookie", f"other=value; {SESSION_COOKIE_NAME}={token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}; other=value"),),
        )
        with mock.patch(
            "wahojobs.browser_session_authentication._issue_actor",
            wraps=browser_authentication._issue_actor,
        ) as issue:
            for headers in valid:
                with self.subTest(valid=headers):
                    self.assertIsNotNone(
                        self.authenticate(browser_request(extra_headers=headers))
                    )
        self.assertEqual(issue.call_count, len(valid))

        invalid = (
            (("Cookie", f"{SESSION_COOKIE_NAME}= {token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME} ={token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME} = {token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token} "),),
            (("Cookie", f" {SESSION_COOKIE_NAME}={token}"),),
            (("Cookie", f"other=value;  {SESSION_COOKIE_NAME}={token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}\t={token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}=\t{token}"),),
            (("Cookie", f'{SESSION_COOKIE_NAME}="{token}"'),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}; {SESSION_COOKIE_NAME}={token}"),),
            (("Cookie", f"{SESSION_COOKIE_NAME}={token}"), ("Cookie", "other=value")),
        )
        with mock.patch(
            "wahojobs.browser_session_authentication._issue_actor"
        ) as issue:
            for headers in invalid:
                with self.subTest(invalid=headers):
                    self.assertIsNone(
                        self.authenticate(browser_request(extra_headers=headers))
                    )
        self.assertEqual(issue.call_count, 0)

    def test_expiry_equality_future_creation_revocation_and_rotation_do_not_authenticate(self):
        idle = AUTHENTICATED_AT + timedelta(hours=2)
        self.assertIsNone(self.authenticate(now=idle))
        self.assertIsNone(self.authenticate(now=idle + timedelta(seconds=1)))
        self.assertIsNone(self.authenticate(now=AUTHENTICATED_AT - timedelta(seconds=1)))

        accounts.revoke_current_session(
            self.writer,
            session_token=self.state["session_token"],
            expected_session_version=1,
            reason="user_logout",
            now=REQUEST_AT,
        )
        self.writer.commit()
        self.assertIsNone(self.authenticate())

    def test_rotated_predecessor_is_rejected_and_valid_replacement_authenticates(self):
        replacement = accounts.rotate_session(
            self.writer,
            session_token=self.state["session_token"],
            expected_session_version=1,
            idle_ttl=timedelta(hours=1),
            idempotency_key="browser-session-rotation",
            now=REQUEST_AT,
        )
        self.writer.commit()
        self.assertIsNone(self.authenticate())
        actor = self.authenticate(
            browser_request(replacement.session_token),
            now=REQUEST_AT + timedelta(minutes=1),
        )
        self.assertIs(type(actor), TrustedAuthenticatedBrowserActor)

    def test_malformed_session_rows_fail_unavailable_without_actor_issuance(self):
        cases = (
            ("token_hash_version", "sha1_v0"),
            ("csrf_secret_hash", "A" * 64),
            ("created_at", "not-a-time"),
            ("last_seen_at", "2099-01-01T00:00:00+00:00"),
            ("session_version", 0),
            ("request_fingerprint", "0" * 64),
        )
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"malformed-{index}.sqlite"
                writer = install_browser_authentication_database(path)
                state = seed_browser_session(writer, suffix=str(100 + index))
                triggers = (
                    ("trg_account_sessions_core_immutable",)
                    if field == "created_at"
                    else ()
                )
                guarded_update(
                    writer,
                    triggers,
                    lambda: writer.execute(
                        f'UPDATE account_sessions SET "{field}" = ? WHERE session_id = ?',
                        (value, state["session_id"]),
                    ),
                )
                writer.close()
                gateway = DurableBrowserSessionAuthenticationGateway(
                    trusted_environment_namespace=state["environment"],
                    clock=lambda: REQUEST_AT,
                )
                with read_only_connection(path) as connection, mock.patch(
                    "wahojobs.browser_session_authentication._issue_actor"
                ) as issue:
                    with self.assertRaises(BrowserSessionAuthenticationUnavailable):
                        gateway.authenticate_browser_request(
                            connection,
                            browser_request(state["session_token"]),
                        )
                self.assertEqual(issue.call_count, 0)

    def test_active_root_rejects_every_unsupported_session_version(self):
        versions = (0, -1, 2, 7, 1.5, "unsupported", sqlite3.Binary(b"1"))
        for index, version in enumerate(versions):
            with self.subTest(version=repr(version)), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"root-version-{index}.sqlite"
                writer = install_browser_authentication_database(path)
                state = seed_browser_session(writer, suffix=str(300 + index))
                guarded_update(
                    writer,
                    (),
                    lambda: writer.execute(
                        "UPDATE account_sessions SET session_version = ? WHERE session_id = ?",
                        (version, state["session_id"]),
                    ),
                )
                writer.close()
                gateway = DurableBrowserSessionAuthenticationGateway(
                    trusted_environment_namespace=state["environment"],
                    clock=lambda: REQUEST_AT,
                )
                with read_only_connection(path) as connection, mock.patch(
                    "wahojobs.browser_session_authentication._issue_actor"
                ) as issue:
                    with self.assertRaises(BrowserSessionAuthenticationUnavailable):
                        gateway.authenticate_browser_request(
                            connection,
                            browser_request(state["session_token"]),
                        )
                self.assertEqual(issue.call_count, 0)

    def test_replacement_and_historical_predecessor_versions_are_exact(self):
        for target in ("replacement", "predecessor"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"rotation-version-{target}.sqlite"
                writer = install_browser_authentication_database(path)
                state = seed_browser_session(writer, suffix=f"40{len(target)}")
                replacement = accounts.rotate_session(
                    writer,
                    session_token=state["session_token"],
                    expected_session_version=1,
                    idle_ttl=timedelta(hours=1),
                    idempotency_key=f"browser-version-{target}",
                    now=REQUEST_AT,
                )
                session_id = (
                    replacement.session.session_id
                    if target == "replacement"
                    else state["session_id"]
                )
                version = 2 if target == "replacement" else 3
                guarded_update(
                    writer,
                    (),
                    lambda: writer.execute(
                        "UPDATE account_sessions SET session_version = ? WHERE session_id = ?",
                        (version, session_id),
                    ),
                )
                writer.close()
                gateway = DurableBrowserSessionAuthenticationGateway(
                    trusted_environment_namespace=state["environment"],
                    clock=lambda: REQUEST_AT + timedelta(minutes=1),
                )
                with read_only_connection(path) as connection, mock.patch(
                    "wahojobs.browser_session_authentication._issue_actor"
                ) as issue:
                    with self.assertRaises(BrowserSessionAuthenticationUnavailable):
                        gateway.authenticate_browser_request(
                            connection,
                            browser_request(replacement.session_token),
                        )
                self.assertEqual(issue.call_count, 0)

    def test_rotation_lineage_accepts_32_edges_and_rejects_33(self):
        for edges, expected in ((32, "actor"), (33, "unavailable")):
            with self.subTest(edges=edges), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"rotation-depth-{edges}.sqlite"
                writer = install_browser_authentication_database(path)
                state = seed_browser_session(writer, suffix=str(500 + edges))
                token = state["session_token"]
                for index in range(1, edges + 1):
                    replacement = accounts.rotate_session(
                        writer,
                        session_token=token,
                        expected_session_version=1,
                        idle_ttl=timedelta(hours=1),
                        idempotency_key=f"browser-depth-{edges}-{index}",
                        now=AUTHENTICATED_AT + timedelta(minutes=index),
                    )
                    token = replacement.session_token
                writer.commit()
                writer.close()
                gateway = DurableBrowserSessionAuthenticationGateway(
                    trusted_environment_namespace=state["environment"],
                    clock=lambda: AUTHENTICATED_AT + timedelta(minutes=edges + 1),
                )
                with read_only_connection(path) as connection, mock.patch(
                    "wahojobs.browser_session_authentication._issue_actor",
                    wraps=browser_authentication._issue_actor,
                ) as issue:
                    if expected == "actor":
                        self.assertIsNotNone(
                            gateway.authenticate_browser_request(
                                connection,
                                browser_request(token),
                            )
                        )
                        self.assertEqual(issue.call_count, 1)
                    else:
                        with self.assertRaises(BrowserSessionAuthenticationUnavailable):
                            gateway.authenticate_browser_request(
                                connection,
                                browser_request(token),
                            )
                        self.assertEqual(issue.call_count, 0)

    def test_missing_identity_fails_unavailable(self):
        self.writer.execute("DELETE FROM auth_identities WHERE user_id = ?", (self.state["account_id"],))
        self.writer.commit()
        self.assert_unavailable()

    def test_malformed_account_fails_unavailable(self):
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        self.writer.execute("UPDATE users SET row_version = 0 WHERE user_id = ?", (self.state["account_id"],))
        self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.commit()
        self.assert_unavailable()

    def test_suspended_account_and_disabled_only_identity_are_unauthenticated(self):
        accounts.suspend_user(
            self.writer,
            user_id=self.state["account_id"],
            expected_version=1,
            source="test_admin",
            idempotency_key="browser-auth-suspend",
            now=REQUEST_AT,
        )
        self.writer.commit()
        self.assertIsNone(self.authenticate())

    def test_disabled_only_identity_is_unauthenticated(self):
        self.writer.execute(
            "UPDATE auth_identities SET disabled_at = ? WHERE user_id = ?",
            (REQUEST_AT.isoformat(timespec="seconds"), self.state["account_id"]),
        )
        self.writer.commit()
        self.assertIsNone(self.authenticate())

    def test_malformed_rotation_counterpart_fails_unavailable(self):
        replacement = accounts.rotate_session(
            self.writer,
            session_token=self.state["session_token"],
            expected_session_version=1,
            idle_ttl=timedelta(hours=1),
            idempotency_key="browser-session-counterpart-rotation",
            now=REQUEST_AT,
        )
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        self.writer.execute(
            "UPDATE account_sessions SET request_fingerprint = ? WHERE session_id = ?",
            ("0" * 64, self.state["session_id"]),
        )
        self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.commit()
        self.assert_unavailable(
            browser_request(replacement.session_token),
        )

    def test_direct_gateway_preserves_caller_owned_transaction(self):
        with read_only_connection(self.path) as connection:
            connection.execute("BEGIN")
            actor = self.gateway.authenticate_browser_request(
                connection,
                browser_request(self.state["session_token"]),
            )
            self.assertIs(type(actor), TrustedAuthenticatedBrowserActor)
            self.assertTrue(connection.in_transaction)
            connection.rollback()

    def test_schema_foreign_key_and_query_only_capabilities_are_required(self):
        with read_only_connection(self.path) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            with self.assertRaises(BrowserSessionAuthenticationUnavailable):
                self.gateway.authenticate_browser_request(connection, browser_request(self.state["session_token"]))
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(BrowserSessionAuthenticationUnavailable):
                self.gateway.authenticate_browser_request(connection, browser_request(self.state["session_token"]))
        finally:
            connection.close()

    def test_schema_definition_drift_is_rejected(self):
        sql = self.writer.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_account_sessions_user_active'"
        ).fetchone()[0]
        version = self.writer.execute("PRAGMA schema_version").fetchone()[0]
        self.writer.execute("PRAGMA writable_schema = ON")
        self.writer.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type='index' AND name='idx_account_sessions_user_active'",
            (sql.replace("absolute_expires_at", "idle_expires_at"),),
        )
        self.writer.execute("PRAGMA writable_schema = OFF")
        self.writer.execute(f"PRAGMA schema_version = {version + 1}")
        self.writer.commit()
        self.assert_unavailable()

    def test_unexpected_session_schema_objects_fail_before_token_lookup(self):
        definitions = (
            "CREATE INDEX idx_unexpected_probe_one ON account_sessions(created_at)",
            "CREATE TRIGGER trg_unexpected_probe_two AFTER INSERT ON account_sessions BEGIN SELECT 1; END",
            "CREATE INDEX idx_unexpected_probe_three ON account_session_rotations(created_at)",
            "CREATE TRIGGER trg_unexpected_probe_four AFTER INSERT ON account_session_rotations BEGIN SELECT 1; END",
            "CREATE VIEW unexpected_probe_five AS SELECT session_id FROM account_sessions",
            "CREATE TABLE unexpected_probe_six (session_id TEXT REFERENCES account_sessions(session_id))",
        )
        for index, definition in enumerate(definitions):
            with self.subTest(definition=definition):
                self.assert_schema_object_unavailable(
                    definition,
                    suffix=str(600 + index),
                )

    def test_unrelated_product_index_does_not_become_account_schema_drift(self):
        self.writer.execute(
            "CREATE INDEX idx_unrelated_profile_probe ON product_profiles(profile_id)"
        )
        self.writer.commit()
        self.assertIsNotNone(self.authenticate())

    def test_missing_expected_session_trigger_is_unavailable(self):
        self.writer.execute("DROP TRIGGER trg_account_sessions_rotation_state_guard")
        self.writer.commit()
        with mock.patch(
            "wahojobs.browser_session_authentication._rows",
            wraps=browser_authentication._rows,
        ) as rows, mock.patch(
            "wahojobs.browser_session_authentication._issue_actor"
        ) as issue:
            self.assert_unavailable()
        self.assertEqual(rows.call_count, 0)
        self.assertEqual(issue.call_count, 0)

    def test_authentication_is_read_only_and_uses_indexed_digest_lookup(self):
        before = file_fingerprint(self.path)
        with read_only_connection(self.path) as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT session_id FROM account_sessions WHERE token_hash = ? ORDER BY session_id LIMIT 2",
                (hashlib.sha256(self.state["session_token"].encode("ascii")).hexdigest(),),
            ).fetchall()
            self.gateway.authenticate_browser_request(connection, browser_request(self.state["session_token"]))
        self.assertTrue(any("token_hash" in row[-1] or "sqlite_autoindex_account_sessions_2" in row[-1] for row in plan))
        self.assertEqual(file_fingerprint(self.path), before)
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-journal").exists())


if __name__ == "__main__":
    unittest.main()
