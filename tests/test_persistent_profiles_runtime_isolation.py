import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from tests.ownership_test_support import database_snapshot, install_ownership

import scripts.persistent_profiles_migration as migration
import scripts.persistent_profile_canonical_v2_migration as migration_005


ROOT = Path(__file__).resolve().parents[1]


class PersistentProfilesRuntimeIsolationTests(unittest.TestCase):
    def test_b2c4_documentation_records_dormant_lifecycle_boundaries(self):
        documentation = (
            ROOT / "docs" / "persistent_profile_services.md"
        ).read_text(encoding="utf-8")
        for statement in (
            "## B2C4 Dormant Durable Browser Session Lifecycle Services",
            "sealed trusted commands",
            "No browser request can invoke these mutations",
            "request-scoped secret vault",
            "does not retain or reference the vault",
            "pending_commit",
            "a trusted consumption attempt becomes terminal",
            "`terminal_failed`",
            "nonsecret per-issuance binding nonce",
            "the complete request-scoped vault is cleared and closed",
            "No result-to-vault reference",
            "Vault close is mandatory at request completion, idempotent",
            "cannot reproduce either prior raw credential",
            "active/current session row uses version `1`",
            "rotated or revoked historical row uses version `2`",
            "32 edges",
            "Python cannot guarantee physical memory zeroization",
            "no login or logout UI",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, documentation)

    def test_b2c3_documentation_records_exact_browser_authentication_boundaries(self):
        documentation = (
            ROOT / "docs" / "persistent_profile_services.md"
        ).read_text(encoding="utf-8")
        for statement in (
            "`wahojobs_session`",
            "exactly 43 ASCII base64url characters",
            "4,096 bytes",
            "64 headers",
            "Multiple Cookie headers",
            "duplicate `wahojobs_session` occurrences",
            "inside or around the target",
            "never logged, rendered, serialized, persisted",
            "unexpected user-defined",
            "Rotation edges have no interchangeable row-version field",
            "no login UI",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, documentation)

    def test_base_schema_and_browser_runtime_do_not_install_or_import_migration_004(self):
        schema = (ROOT / "wahojobs" / "db" / "schema.sql").read_text(encoding="utf-8")
        local_app = (ROOT / "scripts" / "local_product_app.py").read_text(encoding="utf-8")
        self.assertNotIn("product_profile_revisions", schema)
        self.assertNotIn("product_profile_sources", schema)
        self.assertNotIn("current_product_profiles", schema)
        self.assertNotIn("persistent_profiles_migration", local_app)
        self.assertNotIn("persistent_profile_schema", local_app)
        self.assertNotIn("principal_id", local_app)
        self.assertNotIn("persistent_profile_id", local_app)
        self.assertNotIn("persistent_profile_canonical_v2_migration", local_app)
        self.assertNotIn("persistent_profile_canonical_v2_schema", local_app)
        self.assertNotIn("confirmed_lifecycle_action", local_app)
        self.assertNotIn("005_persistent_profile_canonical_v2", schema)

    def test_migration_changes_only_empty_dormant_objects_in_temporary_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compatibility.sqlite"
            conn = install_ownership(path)
            before = database_snapshot(conn)
            migration.apply_persistent_profiles_migration(conn)
            after = database_snapshot(conn)
            for table, fingerprint in before.items():
                if table == "wahojobs_schema_migrations":
                    continue
                self.assertEqual(after[table], fingerprint)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_pipeline_items").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applicant_status_updates").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM product_principals").fetchone()[0], 0
            )
            conn.close()

    def test_documented_dormant_privacy_lifecycle_and_fault_contracts(self):
        documentation = (
            ROOT / "docs" / "persistent_product_profiles.md"
        ).read_text(encoding="utf-8")
        for statement in (
            "Migration 004 was installed in the workspace database",
            "but it remains dormant infrastructure",
            "all three persistent-profile tables contained zero rows",
            "browser profile persistence is not active",
            "No source can be inserted, updated, or deleted after its revision exists.",
            "lowercase ASCII `snake_case` object keys",
            "Normal Unicode remains valid in profile values.",
            "every other C0 control (U+0000–U+001F)",
            "every C1 control (U+0080–U+009F)",
            "Archived profiles may receive edit or correction revisions",
            "reactivation requires a distinct `reactivate` revision",
            "54 logical hook labels covering 22 distinct transaction-visible database states",
            "B2A has no profile row service or row-level profile reconciliation",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, documentation)

    def test_migration_005_import_and_temporary_apply_remain_runtime_isolated(self):
        self.assertFalse(hasattr(migration_005, "DB_CONNECTION"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m005-isolation.sqlite"
            conn = install_ownership(path)
            migration.apply_persistent_profiles_migration(conn)
            migration_005.apply_persistent_profile_canonical_v2_migration(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_pipeline_items").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applicant_status_updates").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_principals").fetchone()[0], 0)
            conn.close()

    def test_migration_005_import_opens_no_database_network_or_writer(self):
        script = r'''
import builtins
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.persistent_profile_canonical_v2_schema as schema
import scripts.persistent_profile_canonical_v2_migration as migration
print(schema.MIGRATION_VERSION, migration.MIGRATION_VERSION)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            "005_persistent_profile_canonical_v2 005_persistent_profile_canonical_v2",
        )

    def test_b2c1_module_imports_open_no_database_network_or_writer(self):
        script = r'''
import builtins
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.persistent_profiles_application as application
import wahojobs.persistent_profiles_browser as browser
print(application.PROFILE_HISTORY_PAGE_SIZE, browser.PERSISTENT_PROFILE_ROUTE)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "20 /account/profile")

    def test_b2c2_authorization_import_opens_no_database_network_or_writer(self):
        script = r'''
import builtins
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.persistent_profile_read_authorization as authorization
gateway = authorization.DurablePersistentProfileReadAuthorizationGateway()
print(gateway.scope)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "persistent_profile_read")

    def test_b2c3_authentication_import_opens_no_database_network_or_writer(self):
        script = r'''
import builtins
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.browser_session_authentication as authentication
gateway = authentication.DurableBrowserSessionAuthenticationGateway(
    trusted_environment_namespace="private_beta",
    clock=lambda: None,
)
print(authentication.SESSION_COOKIE_NAME, repr(gateway))
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            "wahojobs_session DurableBrowserSessionAuthenticationGateway(<configured>)",
        )

    def test_b2c4_lifecycle_import_opens_no_database_network_or_writer(self):
        script = r'''
import builtins
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.browser_session_lifecycle as lifecycle
print(hasattr(lifecycle, "_TRUSTED_BROWSER_SESSION_COMMAND_ISSUER"))
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False")

    def test_normal_local_runtime_does_not_import_b2c2_authorization(self):
        script = r'''
import sys
import scripts.local_product_app
print("wahojobs.persistent_profile_read_authorization" in sys.modules)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False")

    def test_normal_local_runtime_does_not_import_b2c3_authentication(self):
        script = r'''
import sys
import scripts.local_product_app
print("wahojobs.browser_session_authentication" in sys.modules)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False")

    def test_normal_local_runtime_does_not_import_b2c4_lifecycle(self):
        script = r'''
import sys
import scripts.local_product_app
print("wahojobs.browser_session_lifecycle" in sys.modules)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
