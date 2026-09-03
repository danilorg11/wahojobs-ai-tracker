"""Freeze verification, runtime isolation and portable no-exports checks."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_semantic_pipeline_freeze as verifier


class CompleteSemanticFreezeTests(unittest.TestCase):
    def test_manifest_and_transitive_dependency_closure(self):
        result = verifier.verify_manifest()
        self.assertGreater(result["runtime_dependencies_verified"], 30)
        manifest = json.loads((ROOT / verifier.MANIFEST_PATH).read_text(encoding="utf-8"))
        for name in ("wahojobs/source_capture.py", "wahojobs/db/repository.py",
                     "wahojobs/opportunity_enrichment.py", "wahojobs/opportunity_semantic_staging.py",
                     "wahojobs/matching/semantic_shadow_grounding.py"):
            self.assertIn(name, manifest["runtime_dependency_closure"])

    def test_manifest_detects_upstream_dependency_tampering(self):
        manifest = json.loads((ROOT / verifier.MANIFEST_PATH).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wahojobs-freeze-copy-") as tmp:
            root = Path(tmp)
            for name in [verifier.MANIFEST_PATH, *manifest["files"]]:
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, dest)
            target = root / "wahojobs/source_capture.py"
            target.write_bytes(target.read_bytes() + b"\n# synthetic tamper\n")
            with self.assertRaisesRegex(ValueError, "frozen_file_changed"):
                verifier.verify_manifest(root=root)

    def test_synthetic_pipeline_in_clean_checkout_without_exports(self):
        manifest = json.loads((ROOT / verifier.MANIFEST_PATH).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wahojobs-no-benchmark-exports-") as tmp:
            root = Path(tmp)
            for name in [verifier.MANIFEST_PATH, *manifest["files"]]:
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, dest)
            self.assertFalse((root / "exports").exists())
            self.assertFalse((root / "data").exists())
            env = {k: v for k, v in os.environ.items()
                   if k not in {"OPENAI_API_KEY", "PYTHONPATH", "PYTHONHOME"}}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            # This suite installs its socket guard and creates only temporary synthetic DBs.
            result = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_semantic_pipeline_v2"],
                                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([sys.executable, "-B", "scripts/verify_semantic_pipeline_freeze.py"],
                                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_production_integration_or_benchmark_runtime_imports(self):
        runtime = ["scripts/semantic_pipeline_v2.py", "scripts/semantic_pipeline_evidence.py"]
        for name in runtime:
            source = (ROOT / name).read_text(encoding="utf-8")
            for forbidden in ("exports/", "blind_human_labels", "evaluate_semantic_development",
                              "prepare_prospective_semantic", "semantic_ranker_development"):
                self.assertNotIn(forbidden, source)
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("benchmark", node.module or "")
        for path in (ROOT / "wahojobs").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("semantic_pipeline_v2", source)
            self.assertNotIn("semantic_pipeline_evidence", source)


if __name__ == "__main__":
    unittest.main()
