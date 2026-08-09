import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "wahojobs" / "agent_operations.py"


class AgentOperationsRuntimeIsolationTests(unittest.TestCase):
    def run_python(self, script, *, cwd=ROOT, hash_seed=None):
        environment = os.environ.copy()
        if hash_seed is not None:
            environment["PYTHONHASHSEED"] = str(hash_seed)
        return subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_import_opens_no_database_file_network_model_tool_or_secret(self):
        script = rf'''
import builtins
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.request

sys.path.insert(0, {str(ROOT)!r})

def blocked(*args, **kwargs):
    raise RuntimeError("forbidden side effect")

sqlite3.connect = blocked
socket.socket = blocked
urllib.request.urlopen = blocked
subprocess.Popen = blocked
threading.Thread.start = blocked
builtins.open = blocked
Path.open = blocked
Path.write_text = blocked
Path.write_bytes = blocked
os.getenv = blocked

class GuardedEnvironment(dict):
    def __getitem__(self, key):
        raise RuntimeError("secret access")
    def get(self, key, default=None):
        raise RuntimeError("secret access")
    def items(self):
        raise RuntimeError("secret access")

os.environ = GuardedEnvironment()
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in {{"openai", "anthropic", "requests", "httpx"}}:
        raise RuntimeError("provider or tool import")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import

import wahojobs.agent_operations as operations
print(operations.A1_SAFETY_POLICY.runtime_active, len(operations.CAPABILITY_TAXONOMY))
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "False 36")

    def test_import_creates_no_files_in_empty_working_directory(self):
        script = rf'''
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
before = tuple(Path.cwd().iterdir())
import wahojobs.agent_operations
after = tuple(Path.cwd().iterdir())
print(before == after == ())
'''
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_python(script, cwd=directory)
        self.assertEqual(result.stdout.strip(), "True")

    def test_package_root_does_not_export_or_eagerly_import_agent_operations(self):
        script = '''
import sys
import wahojobs
print(hasattr(wahojobs, "agent_operations"), "wahojobs.agent_operations" in sys.modules)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "False False")
        package_root = (ROOT / "wahojobs" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("agent_operations", package_root)

    def test_normal_product_runtime_does_not_import_agent_operations(self):
        script = '''
import sys
import scripts.local_product_app
print("wahojobs.agent_operations" in sys.modules)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "False")

    def test_matcher_pipeline_crawler_and_greenhouse_do_not_import_agent_operations(self):
        script = '''
import sys
import wahojobs.matching.evergreen
import wahojobs.pipeline_actions
import wahojobs.crawler.pipeline
import wahojobs.crawler.greenhouse_pilot
import wahojobs.crawler.greenhouse_observations
print("wahojobs.agent_operations" in sys.modules)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "False")

    def test_accepted_account_ownership_profile_and_session_foundations_do_not_import_it(self):
        script = '''
import sys
import wahojobs.accounts
import wahojobs.ownership
import wahojobs.persistent_profiles
import wahojobs.persistent_profile_read_authorization
import wahojobs.browser_session_authentication
import wahojobs.browser_session_lifecycle
print("wahojobs.agent_operations" in sys.modules)
'''
        result = self.run_python(script)
        self.assertEqual(result.stdout.strip(), "False")

    def test_module_has_no_database_network_provider_executor_or_scheduler_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertFalse(
            imported_roots
            & {
                "sqlite3",
                "wahojobs",
                "openai",
                "anthropic",
                "requests",
                "httpx",
                "socket",
                "urllib",
                "subprocess",
                "threading",
                "asyncio",
            }
        )

    def test_module_defines_no_runtime_issuer_executor_scheduler_or_loop(self):
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in (
            "def execute_tool",
            "class ToolExecutor",
            "class AgentScheduler",
            "def autonomous_loop",
            "def issue_human_approval",
            "DB_CONNECTION",
            "MODEL_PROVIDER",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        import wahojobs.agent_operations as operations

        self.assertFalse(hasattr(operations, "issue_human_approval"))
        self.assertFalse(hasattr(operations, "execute_tool"))
        self.assertFalse(hasattr(operations, "schedule_agent"))

    def test_canonical_serialization_is_hash_seed_independent(self):
        script = '''
import hashlib
from wahojobs.agent_operations import canonical_json_bytes
value = {"set": frozenset({"zeta", "alpha", "café"}), "mapping": {"z": 2, "a": 1}}
print(hashlib.sha256(canonical_json_bytes(value)).hexdigest())
'''
        digests = {
            self.run_python(script, hash_seed=seed).stdout.strip()
            for seed in (0, 1, 17, 99991)
        }
        self.assertEqual(len(digests), 1)

    def test_test_approval_validity_is_import_order_independent(self):
        scripts = (
            '''
import wahojobs.agent_operations as operations
import tests.test_agent_operations_domain as fixtures
task = fixtures.make_task(operations.Capability.MODIFY_JOB_METADATA)
intent = fixtures.make_intent(
    operations.Capability.MODIFY_JOB_METADATA,
    summary="Bounded normalized metadata fields for approved records.",
)
approval = fixtures.issue_test_approval(task, (intent,))
print(operations._is_trusted_approval(approval))
''',
            '''
import tests.test_agent_operations_domain as fixtures
import wahojobs.agent_operations as operations
task = fixtures.make_task(operations.Capability.MODIFY_JOB_METADATA)
intent = fixtures.make_intent(
    operations.Capability.MODIFY_JOB_METADATA,
    summary="Bounded normalized metadata fields for approved records.",
)
approval = fixtures.issue_test_approval(task, (intent,))
print(operations._is_trusted_approval(approval))
''',
        )
        for script in scripts:
            with self.subTest(order=script.splitlines()[1]):
                result = self.run_python(script)
                self.assertEqual(result.stdout.strip(), "True")

    def test_no_migration_schema_cli_route_or_background_entrypoint_was_added(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("CREATE TABLE", source.upper())
        self.assertNotIn("argparse", source)
        self.assertNotIn("__main__", source)
        self.assertNotIn("add_route", source)
        migrations = sorted((ROOT / "wahojobs" / "db" / "migrations").glob("*.sql"))
        accepted_migrations = [
            "001_pipeline_state.sql",
            "002_accounts_sessions.sql",
            "003_product_principals.sql",
            "004_persistent_product_profiles.sql",
            "005_persistent_profile_canonical_v2.sql",
            "006_google_oidc_authorization_transactions.sql",
            "007_closed_schema_convergence.sql",
        ]
        self.assertEqual([path.name for path in migrations], accepted_migrations)
        self.assertNotEqual(
            [
                *accepted_migrations,
                "008_unexpected_dormant_migration.sql",
            ],
            accepted_migrations,
        )

    def test_documentation_records_dormant_safety_and_future_boundaries(self):
        documentation = (ROOT / "docs" / "agent_operations.md").read_text(encoding="utf-8")
        for statement in (
            "A1 is dormant. It executes nothing.",
            "An A1 `allow_*` result is a pure policy classification.",
            "A1 agent definitions cannot receive\n`restricted` access.",
            "A draft is never a\nsent message",
            "A1 provides no normal-runtime approval\nissuer",
            "Generated IDs are excluded from replay identity.",
            "no `chain_of_thought`, `reasoning_trace`,\n`scratchpad`, `hidden_prompt`, or `raw_model_context`",
            "A2 may add a durable task",
            "A3 may add a read-only Daily\nCompany Briefing Agent",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, documentation)


if __name__ == "__main__":
    unittest.main()
