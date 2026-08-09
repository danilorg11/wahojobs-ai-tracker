import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_NAMES = (
    "wahojobs.google_oidc_authorization_transactions",
    "wahojobs.google_oidc_transaction_protection",
    "wahojobs.google_oidc_authorization_transaction_schema",
    "wahojobs.google_oidc_authorization_transaction_repository",
    "wahojobs.google_oidc_authorization_transaction_reconciliation",
    "wahojobs.google_oidc_durable_gateway",
)


class DurableGoogleOidcRuntimeIsolationTests(unittest.TestCase):
    def run_python(self, source, *, cwd=ROOT):
        return subprocess.run(
            [sys.executable, "-B", "-c", source],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_imports_open_no_runtime_authority_or_eager_protocol_dependency(self):
        modules = repr(MODULE_NAMES)
        source = rf"""
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

def blocked(*_args, **_kwargs):
    raise RuntimeError("durable google oidc import side effect")

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
    def _blocked(self, *_args, **_kwargs):
        raise RuntimeError("durable google oidc environment read")
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

for name in {modules}:
    __import__(name)

print(
    all(name in sys.modules for name in {modules}),
    tuple(
        root
        for root in ("authlib", "joserfc", "requests", "cryptography")
        if root in sys.modules
    ),
)
"""
        result = self.run_python(source)
        self.assertEqual(result.stdout.strip(), "True ()")

    def test_imports_create_no_file_in_empty_working_directory(self):
        source = rf"""
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
before = tuple(Path.cwd().iterdir())
for name in {MODULE_NAMES!r}:
    __import__(name)
after = tuple(Path.cwd().iterdir())
print(before == after == ())
"""
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_python(source, cwd=directory)
        self.assertEqual(result.stdout.strip(), "True")

    def test_normal_runtime_does_not_import_or_export_durable_components(self):
        source = rf"""
import sys
import wahojobs
import scripts.local_product_app
names = {MODULE_NAMES!r}
activation_names = names + (
    "wahojobs.durable_google_login_browser",
    "wahojobs.durable_google_login_runtime",
)
print(
    any(name in sys.modules for name in activation_names),
    tuple(
        sorted(
            name for name in vars(wahojobs)
            if "oidc" in name.lower() or "google" in name.lower()
        )
    ),
)
"""
        result = self.run_python(source)
        self.assertEqual(result.stdout.strip(), "False ()")

    def test_base_schema_stays_dormant_and_local_app_only_exposes_injection(self):
        schema = (
            ROOT / "wahojobs" / "db" / "schema.sql"
        ).read_text(encoding="utf-8").lower()
        local_app_source = (
            ROOT / "scripts" / "local_product_app.py"
        ).read_text(encoding="utf-8")
        local_app = local_app_source.lower()
        self.assertNotIn("google_oidc_authorization_transactions", schema)
        self.assertNotIn("google_oidc_durable_gateway", schema)
        self.assertNotIn("google_oidc_durable_gateway", local_app)
        self.assertNotIn(
            "prepare_durable_google_oidc_authorization",
            local_app,
        )
        self.assertNotIn(
            "complete_durable_google_oidc_authorization",
            local_app,
        )

        tree = ast.parse(local_app_source)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        self.assertFalse(
            imported_modules
            & {
                "wahojobs.durable_google_login_browser",
                "wahojobs.durable_google_login_runtime",
                "wahojobs.google_oidc_durable_gateway",
            }
        )

        make_handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "make_handler"
        )
        arguments = make_handler.args.args
        defaults = {
            argument.arg: default
            for argument, default in zip(
                arguments[-len(make_handler.args.defaults) :],
                make_handler.args.defaults,
                strict=True,
            )
        }
        self.assertIsNone(
            defaults["durable_google_login_browser_integration"].value
        )
        self.assertIs(
            defaults["exclusive_browser_integration"].value,
            False,
        )

    def test_exact_migration_inventory_ends_at_m007(self):
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
                "007_closed_schema_convergence.sql",
            ],
        )

    def test_durable_gateway_exports_only_the_three_composition_functions(self):
        path = ROOT / "wahojobs" / "google_oidc_durable_gateway.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exports = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        self.assertEqual(
            tuple(
                item.value
                for item in exports.value.elts
                if isinstance(item, ast.Constant)
            ),
            (
                "complete_browser_bound_durable_google_oidc_authorization",
                "complete_durable_google_oidc_authorization",
                "prepare_durable_google_oidc_authorization",
            ),
        )


if __name__ == "__main__":
    unittest.main()
