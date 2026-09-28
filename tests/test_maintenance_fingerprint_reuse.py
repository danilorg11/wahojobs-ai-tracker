"""Fresh complete reconstructed plan proofs replace only redundant hashing."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch

from wahojobs import evidence_maintenance as maintenance
from tests import test_evidence_maintenance as existing
from tests.evidence_maintenance_support import source_plan, offline_transport, T0, TRANSPORT, COHORT


class RebuiltFingerprintTests(unittest.TestCase):
    setUp = existing.MaintenanceTests.setUp

    def execute(self, plan):
        return maintenance.execute_plan(plan, self.journal, authorized=True, authorize_sources=True,
            now=T0, transport_binding=TRANSPORT)

    @staticmethod
    def reseal(plan):
        plan['plan_id'] = maintenance.digest({key: value for key, value in plan.items() if key != 'plan_id'})
        return plan

    def test_one_fresh_preflight_fingerprint_per_source_and_full_postcommit_proof(self):
        plan = source_plan(self.path, T0, details=None)
        with offline_transport(T0), patch.object(maintenance, 'source_fingerprint', wraps=maintenance.source_fingerprint) as fingerprints:
            result = self.execute(plan)
        # One independent rebuilt proof plus the post-publication proof; no
        # extra full-history scan before that same independent reconstruction.
        self.assertEqual(Counter(call.args[1] for call in fingerprints.call_args_list),
                         Counter({source: 2 for source in COHORT}))
        completed = [event['data'] for event in result['events'] if event['event'] == 'operation_result']
        self.assertEqual({item['result']['after']['provider']: item['status'] for item in completed},
                         {'alignerr': 'completed', 'mercor': 'partially_completed'})
        with maintenance.read_connection(self.path) as connection:
            for item in completed:
                state = item['result']['after']
                self.assertEqual(state['fingerprint'], maintenance.source_fingerprint(connection, state['provider']))
                self.assertTrue(state['jobs'])

    def test_changed_rows_still_refuse_before_dispatch_and_journal_creation(self):
        plan = source_plan(self.path, T0)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE companies SET careers_url='https://changed.example/catalog' WHERE slug='mercor'")
        before = self.path.read_bytes()
        with patch.object(maintenance, 'run_crawl', side_effect=AssertionError('no changed source dispatch')):
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                self.execute(plan)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.journal.exists())

    def test_resealed_missing_extra_duplicate_and_mismatched_source_scopes_refuse(self):
        plan = source_plan(self.path, T0)
        cases = [[], plan['sources'][:1], [*plan['sources'], plan['sources'][0]],
                 [*plan['sources'], dict(provider='appen', fingerprint='a' * 64)],
                 [dict(plan['sources'][0], fingerprint='a' * 64), plan['sources'][1]]]
        for sources in cases:
            with self.subTest(sources=[source['provider'] for source in sources]):
                edited = deepcopy(plan)
                edited['sources'] = deepcopy(sources)
                self.reseal(edited)
                with patch.object(maintenance, 'run_crawl', side_effect=AssertionError('no forged scope dispatch')):
                    with self.assertRaisesRegex(ValueError, 'source_changed'):
                        self.execute(edited)
                self.assertFalse(self.journal.exists())

    def test_schema_change_between_initial_check_and_rebuild_still_refuses(self):
        plan = source_plan(self.path, T0)
        original = maintenance.build_plan
        def changed(*args, **kwargs):
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute('CREATE TABLE synthetic_added_schema (value TEXT)')
            return original(*args, **kwargs)
        with patch.object(maintenance, 'build_plan', side_effect=changed):
            with patch.object(maintenance, 'run_crawl', side_effect=AssertionError('no changed schema dispatch')):
                with self.assertRaisesRegex(ValueError, 'schema_changed'):
                    self.execute(plan)
        self.assertFalse(self.journal.exists())

    def test_historical_capture_tampering_is_not_cached_or_skipped(self):
        from datetime import timedelta
        from tests.evidence_maintenance_support import source_execute
        source_execute(self.path, self.journal, T0)
        later = T0 + timedelta(hours=73)
        plan = source_plan(self.path, later)
        original = maintenance.build_plan
        def changed(*args, **kwargs):
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute("UPDATE job_source_content_captures SET body=body || 'tampered' WHERE id=(SELECT min(id) FROM job_source_content_captures)")
            return original(*args, **kwargs)
        with patch.object(maintenance, 'build_plan', side_effect=changed):
            with patch.object(maintenance, 'run_crawl', side_effect=AssertionError('no tampered history dispatch')):
                with self.assertRaises((RuntimeError, ValueError)):
                    maintenance.execute_plan(plan, self.directory / 'changed-journal', authorized=True,
                        authorize_sources=True, now=later, transport_binding=TRANSPORT)
        self.assertFalse((self.directory / 'changed-journal').exists())


if __name__ == '__main__':
    unittest.main()
