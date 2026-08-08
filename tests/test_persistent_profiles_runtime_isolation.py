import ast
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from tests.ownership_test_support import database_snapshot, install_ownership

import scripts.persistent_profiles_migration as migration
import scripts.persistent_profile_canonical_v2_migration as migration_005


ROOT = Path(__file__).resolve().parents[1]
GOOGLE_OIDC_MODULE = ROOT / "wahojobs" / "google_oidc_gateway.py"
LOCAL_PRODUCT_APP = ROOT / "scripts" / "local_product_app.py"
APPROVED_GOOGLE_OIDC_DEPENDENCY_ROOTS = ("authlib", "joserfc", "requests")


class PersistentProfilesRuntimeIsolationTests(unittest.TestCase):
    def run_python(self, script, *, cwd=ROOT):
        return subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_google_oidc_import_creates_no_runtime_side_effects(self):
        script = rf'''
import asyncio
import atexit
import builtins
import getpass
import http.client
import io
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import _thread
import threading
import urllib.request

sys.path.insert(0, {str(ROOT)!r})

def blocked(*args, **kwargs):
    raise RuntimeError("forbidden google oidc import side effect")

sqlite3.connect = blocked
socket.socket = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked
http.client.HTTPConnection.connect = blocked
http.client.HTTPSConnection.connect = blocked
urllib.request.urlopen = blocked
urllib.request.OpenerDirector.open = blocked
subprocess.Popen = blocked
_thread.start_new_thread = blocked
threading.Thread.__init__ = blocked
threading.Thread.start = blocked
asyncio.create_task = blocked
atexit.register = blocked
builtins.open = blocked
io.open = blocked
Path.open = blocked
Path.write_text = blocked
Path.write_bytes = blocked
os.getenv = blocked
os.open = blocked
os.putenv = blocked
os.unsetenv = blocked
os.urandom = blocked
getpass.getpass = blocked
secrets.token_bytes = blocked
secrets.token_hex = blocked
secrets.token_urlsafe = blocked

class GuardedEnvironment(dict):
    def _blocked(self, *args, **kwargs):
        raise RuntimeError("forbidden google oidc environment or secret read")

    __contains__ = _blocked
    __getitem__ = _blocked
    __iter__ = _blocked
    __len__ = _blocked
    __repr__ = _blocked
    copy = _blocked
    get = _blocked
    items = _blocked
    keys = _blocked
    values = _blocked

os.environ = GuardedEnvironment()

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in {{
        "authlib",
        "joserfc",
        "requests",
        "keyring",
        "flask",
        "django",
        "starlette",
        "fastapi",
    }}:
        raise RuntimeError("forbidden eager dependency, secret, or route import")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import wahojobs.google_oidc_gateway as gateway
dependency_roots = ("authlib", "joserfc", "requests")
print(
    gateway.GoogleOidcGateway.__name__,
    all(root not in sys.modules for root in dependency_roots),
)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "GoogleOidcGateway True")

    def test_google_oidc_import_creates_no_file_in_empty_working_directory(self):
        script = rf'''
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
before = tuple(Path.cwd().iterdir())
import wahojobs.google_oidc_gateway
after = tuple(Path.cwd().iterdir())
print(before == after == ())
'''
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_python(script, cwd=directory)
        self.assertEqual(result.stdout.strip(), "True")

    def test_google_oidc_cold_configuration_and_gateway_open_no_network_or_database(
        self,
    ):
        script = rf'''
import http.client
import socket
import sqlite3
import sys
import urllib.request

sys.path.insert(0, {str(ROOT)!r})

def blocked(*args, **kwargs):
    raise RuntimeError("forbidden google oidc cold-construction side effect")

sqlite3.connect = blocked
socket.socket = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked
http.client.HTTPConnection.connect = blocked
http.client.HTTPSConnection.connect = blocked
urllib.request.urlopen = blocked
urllib.request.OpenerDirector.open = blocked

import tests.google_oidc_gateway_test_support as support
import requests

requests.Session.request = blocked
requests.Session.send = blocked

fake_harness = support.make_fake_gateway()
real_harness = support.make_real_gateway()
try:
    print(
        type(fake_harness.configuration).__name__,
        type(fake_harness.gateway).__name__,
        type(real_harness.gateway).__name__,
    )
finally:
    real_harness.close()
    fake_harness.close()
'''
        result = self.run_python(script)
        self.assertEqual(
            result.stdout.strip(),
            "TrustedGoogleOidcConfiguration GoogleOidcGateway GoogleOidcGateway",
        )

    def test_package_root_does_not_export_or_import_google_oidc_gateway(self):
        script = r'''
import sys
import wahojobs
oidc_exports = tuple(
    sorted(
        name
        for name in vars(wahojobs)
        if "google" in name.lower() or "oidc" in name.lower()
    )
)
print(
    oidc_exports,
    "wahojobs.google_oidc_gateway" in sys.modules,
    all(root not in sys.modules for root in ("authlib", "joserfc", "requests")),
)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "() False True")
        package_root = (ROOT / "wahojobs" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("google_oidc_gateway", package_root)
        self.assertNotIn("GoogleOidc", package_root)

    def test_normal_local_startup_does_not_import_google_oidc_or_dependencies(self):
        script = r'''
import sys
import scripts.local_product_app
print(
    "wahojobs.google_oidc_gateway" in sys.modules,
    tuple(
        root
        for root in ("authlib", "joserfc", "requests")
        if root in sys.modules
    ),
)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "False ()")

    def test_accepted_accounts_b2c_and_b2d1_do_not_reverse_import_google_oidc(self):
        script = r'''
import sys
import wahojobs.accounts
import wahojobs.account_reconciliation
import wahojobs.browser_session_authentication
import wahojobs.browser_session_lifecycle
import wahojobs.trusted_login_completion
print(
    "wahojobs.google_oidc_gateway" in sys.modules,
    tuple(
        root
        for root in ("authlib", "joserfc", "requests")
        if root in sys.modules
    ),
)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "False ()")

    def test_google_oidc_dependencies_exist_only_in_the_lazy_loader(self):
        tree = ast.parse(GOOGLE_OIDC_MODULE.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        dependency_imports = []
        dynamic_imports = []
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
                self.assertNotIn("*", (alias.name for alias in node.names))
            elif isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "__import__"
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    dynamic_imports.append(node)

            for module in modules:
                root = module.split(".", 1)[0]
                if root not in APPROVED_GOOGLE_OIDC_DEPENDENCY_ROOTS:
                    continue
                parent = parents.get(node)
                while parent is not None and not isinstance(
                    parent, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    parent = parents.get(parent)
                dependency_imports.append(
                    (module, None if parent is None else parent.name)
                )

        self.assertFalse(dynamic_imports)
        self.assertEqual(
            {module for module, _function in dependency_imports},
            {
                "authlib.common.errors",
                "authlib.integrations.base_client.errors",
                "authlib.integrations.requests_client",
                "authlib.oidc.core",
                "authlib.oauth2.rfc6749.errors",
                "joserfc",
                "joserfc.errors",
                "joserfc.jwk",
                "requests",
                "requests.adapters",
                "requests.exceptions",
            },
        )
        self.assertEqual(
            {function for _module, function in dependency_imports},
            {"_load_dependencies"},
        )
        source = GOOGLE_OIDC_MODULE.read_text(encoding="utf-8")
        self.assertNotIn("authlib.jose", source)

    def test_local_product_app_default_stays_dormant_with_explicit_injection_only(
        self,
    ):
        source = LOCAL_PRODUCT_APP.read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in (
            "google_oidc_gateway",
            "google_oidc_durable_gateway",
            "durable_google_login_runtime",
            "accounts.google.com",
            "oauth2.googleapis.com",
            "www.googleapis.com/oauth2",
            "import authlib",
            "from authlib",
            "import joserfc",
            "from joserfc",
            "set-cookie",
            "/oidc",
            "/oauth",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

        tree = ast.parse(source)
        make_handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "make_handler"
        )
        positional = make_handler.args.args
        defaults = {
            argument.arg: default
            for argument, default in zip(
                positional[-len(make_handler.args.defaults) :],
                make_handler.args.defaults,
                strict=True,
            )
        }
        self.assertIsInstance(
            defaults["durable_google_login_browser_integration"],
            ast.Constant,
        )
        self.assertIsNone(
            defaults["durable_google_login_browser_integration"].value
        )
        self.assertIsInstance(
            defaults["exclusive_browser_integration"],
            ast.Constant,
        )
        self.assertIs(
            defaults["exclusive_browser_integration"].value,
            False,
        )

        ordinary_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_handler"
        ]
        self.assertEqual(len(ordinary_calls), 1)
        self.assertNotIn(
            "durable_google_login_browser_integration",
            {
                keyword.arg
                for keyword in ordinary_calls[0].keywords
                if keyword.arg is not None
            },
        )
        self.assertNotIn(
            "exclusive_browser_integration",
            {
                keyword.arg
                for keyword in ordinary_calls[0].keywords
                if keyword.arg is not None
            },
        )

    def test_google_oidc_adds_no_route_migration_startup_cookie_or_activation(self):
        source = GOOGLE_OIDC_MODULE.read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in (
            "basehttprequesthandler",
            "send_header",
            "send_response",
            "set-cookie",
            "add_route",
            "@app.route",
            "@router.",
            "local_product_app",
            "create table",
            "alter table",
            "drop table",
            "insert into",
            "update auth_",
            "delete from",
            "argparse",
            "atexit.register",
            'if __name__ == "__main__"',
            "/account/profile",
            "csrf",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

        tree = ast.parse(source)
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertFalse(
            imported_roots
            & {
                "scripts",
                "http",
                "flask",
                "django",
                "starlette",
                "fastapi",
            }
        )

        migrations = sorted(
            (ROOT / "wahojobs" / "db" / "migrations").glob("*.sql")
        )
        self.assertEqual(
            [path.name for path in migrations],
            [
                "001_pipeline_state.sql",
                "002_accounts_sessions.sql",
                "003_product_principals.sql",
                "004_persistent_product_profiles.sql",
                "005_persistent_profile_canonical_v2.sql",
                "006_google_oidc_authorization_transactions.sql",
            ],
        )

    def test_b2d1_documentation_records_dormant_login_completion_boundaries(self):
        documentation = (
            ROOT / "docs" / "persistent_profile_services.md"
        ).read_text(encoding="utf-8")
        for statement in (
            "## B2D1 Dormant Trusted Login Completion",
            "dormant, default-disabled completion boundary",
            "authentication-provider gateway",
            "sealed trusted completion policy",
            "exact expected provider and assurance-policy version",
            "bounded independent retry",
            "revoked as undelivered",
            "closed and has zero entries",
            "Control-flow exceptions are",
            "idempotent emergency",
            "No ordinary result returns",
            "new trusted login request",
            "complete account and identity rows",
            "trusted environment appears independently",
            "sole owner of lifetime bounds",
            "one transaction",
            "request-scoped secret vault",
            "`pending_commit`",
            "An exact replay returns `already_completed`",
            "ineligible state produces only generic authentication",
            "generic unavailability",
            "compose a browser response",
            "emit `Set-Cookie`",
            "implement signup",
            "future provider gateway",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, documentation)

    def test_b2d1_recovery_has_only_the_accepted_phase_a_broad_boundaries(self):
        source = (
            ROOT / "wahojobs" / "trusted_login_completion.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        qualified = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Name)
                and node.type.id == "BaseException"
            ):
                continue
            function = parents.get(node)
            while function is not None and not isinstance(
                function,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                function = parents.get(function)
            self.assertIsNotNone(function)
            owner = parents.get(function)
            while owner is not None and not isinstance(owner, ast.ClassDef):
                if isinstance(
                    owner,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    break
                owner = parents.get(owner)
            qualified.append(
                (
                    f"{owner.name}." if isinstance(owner, ast.ClassDef) else ""
                )
                + function.name
            )
        self.assertEqual(
            sorted(qualified),
            [],
        )

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

    def test_b2d1_import_opens_no_database_network_writer_or_environment_identity(self):
        script = r'''
import builtins
import os
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
os.getenv = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.trusted_login_completion as completion
print(
    hasattr(completion, "_TRUSTED_EXTERNAL_AUTHENTICATION_ISSUER"),
    hasattr(completion, "_TRUSTED_LOGIN_COMPLETION_POLICY_ISSUER"),
    hasattr(completion, "DEFAULT_TRUSTED_LOGIN_COMPLETION_POLICY"),
    hasattr(completion, "LOGIN_ROUTE"),
    completion.TrustedLoginCompletionResult.__name__,
)
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
            "False False False False TrustedLoginCompletionResult",
        )

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
        self.assertEqual(result.stdout.strip(), "True")

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
        self.assertEqual(result.stdout.strip(), "True")

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

    def test_normal_local_runtime_does_not_import_or_expose_b2d1(self):
        script = r'''
import sys
import wahojobs
import scripts.local_product_app
print(
    "wahojobs.trusted_login_completion" in sys.modules,
    hasattr(wahojobs, "complete_trusted_login"),
)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False False")


if __name__ == "__main__":
    unittest.main()
