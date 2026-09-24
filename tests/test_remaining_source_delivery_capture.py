"""No-network accounting checks for the single beta-host request ledger."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts import remaining_source_delivery_capture as capture


class DeliveryLedgerTests(unittest.TestCase):
    def test_one_reviewed_beta_host_and_ledger_are_not_caller_options(self):
        self.assertEqual(capture.EXPECTED_HOST, 'wahojobs-private-beta-rehearsal-20260917')
        self.assertEqual(capture.LEDGER_ROOT.as_posix(),
                         '/var/lib/wahojobs-beta/remaining-source-coverage-v1/task-ledger')

    def test_prior_phases_share_source_and_aggregate_attempt_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs").mkdir()
            for name, source, attempts, elapsed in (
                ("validation-da", "dataannotation", 3, 12.5),
                ("validation-df", "dataforce", 43, 70.0),
                ("commissioning-da", "dataannotation", 2, 2.0),
            ):
                run = root / "runs" / name
                run.mkdir()
                (run / "receipt.json").write_text(json.dumps({
                    "version": capture.VERSION, "source": source,
                    "http_attempts": attempts, "elapsed_seconds": elapsed,
                }), encoding="utf-8")
            self.assertEqual(capture.usage(root),
                             ({"dataannotation": 5, "dataforce": 43}, 84.5))

    def test_unfinished_or_over_budget_run_blocks_further_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs" / "interrupted").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "unfinished_capture"):
                capture.usage(root)
            (root / "runs" / "interrupted" / "receipt.json").write_text(json.dumps({
                "version": capture.VERSION, "source": "dataannotation",
                "http_attempts": 33, "elapsed_seconds": 1.0,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "capture_ledger_receipt_invalid"):
                capture.usage(root)

    def test_nonfinite_elapsed_time_cannot_reset_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'runs' / 'invalid'
            run.mkdir(parents=True)
            (run / 'receipt.json').write_text(json.dumps({
                'version': capture.VERSION, 'source': 'dataforce',
                'http_attempts': 1, 'elapsed_seconds': float('nan'),
            }), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'capture_ledger_receipt_invalid'):
                capture.usage(root)


if __name__ == "__main__":
    unittest.main()
