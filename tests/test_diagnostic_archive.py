import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wahojobs.diagnostic_archive import archive_preflight


class DiagnosticArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name) / "logs"
        self.parent.mkdir(mode=0o700)
        self.parent.chmod(0o700)
        probe = self.parent / "probe"
        try:
            probe.symlink_to(self.parent, target_is_directory=True)
        except OSError:
            self.can_link = False
        else:
            self.can_link = True
            probe.unlink()

    def require_links(self):
        if not self.can_link:
            self.skipTest("directory links unavailable in this test environment")

    def make_runs(self, count):
        for number in range(count):
            run = self.parent / ("run-" + f"{number:032x}")
            run.mkdir()
            (run / "requests.jsonl").write_text(f"{number}\n")

    def test_at_below_above_guard_and_repeated_runs_preserve_references(self):
        self.require_links()
        for count in (31, 32, 36):
            with self.subTest(count=count):
                parent = self.parent / f"case-{count}"
                parent.mkdir(mode=0o700)
                parent.chmod(0o700)
                for number in range(count):
                    run = parent / ("run-" + f"{number:032x}")
                    run.mkdir()
                    (run / "requests.jsonl").write_text(f"{number}\n")
                first = archive_preflight(parent)
                self.assertEqual(first["live"], 8)
                self.assertEqual(first["archived_total"], count - 8)
                self.assertEqual(archive_preflight(parent)["archived"], ())
                for number in range(count):
                    run = parent / ("run-" + f"{number:032x}")
                    self.assertEqual((run / "requests.jsonl").read_text(), f"{number}\n")
                for number in range(count, count + 3):
                    run = parent / ("run-" + f"{number:032x}")
                    run.mkdir()
                    (run / "requests.jsonl").write_text(f"{number}\n")
                    archive_preflight(parent)
                self.assertEqual(archive_preflight(parent)["live"], 8)
                self.assertEqual(len(list((parent / "archive").iterdir())), count - 5)

    def test_active_and_unfinished_artifacts_are_preserved(self):
        self.require_links()
        self.make_runs(12)
        active = "run-" + f"{0:032x}"
        unfinished = self.parent / ("run-" + f"{1:032x}")
        (unfinished / "unfinished.txt").write_text("retain")
        result = archive_preflight(self.parent, active_runs=(active,))
        self.assertTrue((self.parent / active).is_dir())
        self.assertFalse((self.parent / active).is_symlink())
        self.assertEqual((unfinished / "unfinished.txt").read_text(), "retain")
        self.assertEqual(result["live"], 9)
        for archived in (self.parent / "archive").iterdir():
            manifest = json.loads((archived / ".archive-manifest.json").read_text())
            self.assertIn("requests.jsonl", manifest)

    def test_interrupted_move_and_unwritable_archive_do_not_lose_run(self):
        self.require_links()
        self.make_runs(10)
        original = self.parent / ("run-" + f"{0:032x}")
        real_rename = Path.rename
        def fail_rename(path, target):
            if path == original:
                raise OSError("full archive")
            return real_rename(path, target)
        with patch.object(Path, "rename", fail_rename):
            with self.assertRaises(OSError):
                archive_preflight(self.parent)
        self.assertEqual((original / "requests.jsonl").read_text(), "0\n")
        result = archive_preflight(self.parent)
        self.assertIn(original.name, result["archived"])
        link = self.parent / ("run-" + f"{1:032x}")
        target = self.parent / "archive" / link.name
        link.unlink()
        self.assertTrue(target.is_dir())
        archive_preflight(self.parent)
        self.assertEqual((link / "requests.jsonl").read_text(), "1\n")

    def test_rejects_symlinked_run_entry_without_moving_it(self):
        self.require_links()
        self.make_runs(10)
        run = self.parent / ("run-" + f"{0:032x}")
        (run / "requests.jsonl").unlink()
        (run / "requests.jsonl").symlink_to(self.parent / "unrelated")
        with self.assertRaisesRegex(ValueError, "unsafe_diagnostic_run_entry"):
            archive_preflight(self.parent)
        self.assertFalse((self.parent / "archive" / run.name).exists())

    def test_missing_archive_manifest_fails_before_restoring_reference(self):
        archive = self.parent / "archive"
        archive.mkdir(mode=0o700)
        archive.chmod(0o700)
        target = archive / ("run-" + f"{0:032x}")
        target.mkdir()
        (target / "requests.jsonl").write_text("retained\n")
        with patch('wahojobs.diagnostic_archive._validate_parent', lambda path: Path(path)), \
                patch('wahojobs.diagnostic_archive.stat.S_IMODE', return_value=0o700):
            with self.assertRaisesRegex(ValueError, "diagnostic_archive_manifest_missing"):
                archive_preflight(self.parent)
        self.assertFalse((self.parent / target.name).exists())


if __name__ == "__main__":
    unittest.main()
