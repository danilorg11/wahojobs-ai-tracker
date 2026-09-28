"""Deferred planning reports never replace atomic, complete acceptance replay."""
from contextlib import closing
from datetime import timedelta
import sqlite3
import unittest
from unittest.mock import Mock, patch

from wahojobs import daily_inventory as daily, evidence_maintenance as maintenance
from wahojobs.crawler import staged_observation as staged
from wahojobs.crawler.types import TrackingSummary
from wahojobs.database_lifetime_ownership import (
    acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)
from tests import test_daily_source_coverage as coverage
from tests.test_daily_source_coverage import Transport, offline
from tests.evidence_maintenance_support import T0


class StagedBaselineTests(unittest.TestCase):
    setUp = coverage.CoverageIntegrationTests.setUp

    def plan(self, **options):
        return maintenance.build_plan(self.db, ['mercor'], **(dict(now=T0+timedelta(days=1),
            http_limit=1, details=None, phase='source', daily_discovery=True,
            staged_baseline=True) | options))

    def collect(self):
        self.at = T0+timedelta(days=1)
        with maintenance.read_connection(self.db) as connection:
            url = connection.execute("SELECT careers_url FROM companies WHERE slug='mercor'").fetchone()[0]
        with offline(self.at, Transport()):
            staged.collect('mercor', url, self.root/'collection', run_id='baseline', code_commit='a'*40,
                http_max=1, journal_root=self.journal)
            self.observation, self.collection_report = staged.load(self.root/'collection', 'mercor', run_id='baseline',
                code_commit='a'*40, journal_root=self.journal)
        return self.observation

    def execute(self, plan, **options):
        lease = acquire_database_lifetime_ownership(self.db, role=ROLE_OFFLINE_OPERATOR)
        try:
            with offline(self.at+timedelta(minutes=20), Transport()):
                return maintenance.execute_plan(plan, self.journal, authorized=True,
                    authorize_sources=True, ownership=lease, observation=self.observation,
                    now=self.at+timedelta(minutes=20), **options)
        finally:
            release_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=self.db)

    def absent_id(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT id FROM jobs WHERE external_id='synthetic-maintenance-absent'").fetchone()[0]

    def inactive_absent(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('UPDATE jobs SET is_active=0 WHERE id=?', (self.absent_id(),))

    def corrupt_absent_history(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE job_source_content_captures SET body=body||' corrupt retained history' "
                'WHERE id=(SELECT accepted_capture_id FROM job_source_content_acceptances WHERE job_id=?)',
                (self.absent_id(),))

    def catalog(self):
        tables = ('jobs', 'job_source_contents', 'job_source_content_captures',
            'job_source_content_acceptances', 'job_events', 'canonical_opportunities', 'opportunity_enrichments')
        with closing(sqlite3.connect(self.db)) as connection:
            return {table: connection.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall() for table in tables}

    @staticmethod
    def operations(result):
        return [event['data'] for event in result['events'] if event['event']=='operation_result']

    def test_baseline_preserves_full_fingerprint_operations_and_comparison_fields(self):
        self.inactive_absent()
        normal = self.plan(staged_baseline=False)
        with patch.object(maintenance, 'get_job_source_capture_evidence', side_effect=AssertionError('preplan replay')):
            baseline = self.plan()
        self.assertEqual(normal['schema_fingerprint'], baseline['schema_fingerprint'])
        self.assertEqual(normal['operations'], baseline['operations'])
        before, lightweight = normal['sources'][0], baseline['sources'][0]
        self.assertEqual(before['fingerprint'], lightweight['fingerprint'])
        self.assertEqual(before['coverage'], {key: value for key, value in lightweight['coverage'].items()
            if key not in ('inspection_mode', 'history_validation')})
        self.assertEqual(lightweight['coverage']['history_validation'], 'deferred_until_atomic_publication')
        self.assertEqual(lightweight['inspection_mode'], maintenance.STAGED_BASELINE)
        for full, light in zip(before['jobs'], lightweight['jobs']):
            self.assertEqual({key: value for key, value in full.items() if key!='evidence'},
                {key: value for key, value in light.items() if key!='evidence'})
            for key in ('accepted_semantic_material_sha256', 'accepted_capture_id', 'latest_capture_id', 'last_confirmed_at'):
                self.assertEqual(full['evidence'].get(key), light['evidence'][key])

    def test_scope_and_missing_observation_refuse_before_any_execution_effect(self):
        for change in (dict(daily_discovery=False), dict(phase='all'), dict(details='needed'),
                dict(detail_limit=1), dict(staged_baseline='true')):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'baseline_scope_required'):
                self.plan(**change)
        plan = self.plan()
        before = self.db.read_bytes()
        with patch.object(maintenance, 'Journal') as journal, patch.object(maintenance, 'pin_journal') as pin,\
                patch.object(maintenance, 'run_crawl') as crawl,\
                patch('wahojobs.maintenance_gate.operation_gate') as gate:
            with self.assertRaisesRegex(ValueError, 'baseline_requires_atomic_observation'):
                maintenance.execute_plan(plan, self.journal, authorized=True, authorize_sources=True)
            for effect in (journal, pin, crawl, gate): effect.assert_not_called()
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse((self.journal/plan['plan_id']).exists())

    def test_legacy_accepted_semantic_comparison_matches_full_reader(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            job_id = connection.execute("SELECT id FROM jobs WHERE external_id='synthetic-maintenance-support'").fetchone()[0]
            connection.execute('DELETE FROM job_source_content_acceptances WHERE job_id=?', (job_id,))
            connection.execute('DELETE FROM job_source_content_captures WHERE job_id=?', (job_id,))
            connection.execute("UPDATE jobs SET semantic_authority_state='legacy_accepted' WHERE id=?", (job_id,))
        self.collect()
        full_plan, light_plan = self.plan(staged_baseline=False), self.plan()
        full = next(job for job in full_plan['sources'][0]['jobs'] if job['job_id']==job_id)
        light = next(job for job in light_plan['sources'][0]['jobs'] if job['job_id']==job_id)
        self.assertEqual(full['evidence']['accepted_semantic_material_sha256'],
            light['evidence']['accepted_semantic_material_sha256'])
        self.assertIsNone(light['evidence']['accepted_capture_id'])
        self.assertIsNone(light['evidence']['latest_capture_id'])
        self.assertEqual(full['evidence']['state'], 'legacy_accepted')
        self.assertEqual(full_plan['sources'][0]['coverage'],
            {key: value for key, value in light_plan['sources'][0]['coverage'].items()
             if key not in ('inspection_mode', 'history_validation')})
        report = staged.publication_report(self.collection_report, self.execute(light_plan))
        full_counts, light_counts = [daily.summarize_source(plan, report, self.at,
            self.at+timedelta(minutes=20)) for plan in (full_plan, light_plan)]
        self.assertTrue(light_counts['qualifying_observation'])
        for field in ('changed', 'reconfirmed', 'new', 'new_canonical_opportunities'):
            self.assertEqual(full_counts[field], light_counts[field])
        self.assertEqual((light_counts['changed'], light_counts['reconfirmed']), (0, 1))

    def test_raw_history_change_between_plan_and_rebuild_refuses_before_dispatch(self):
        self.collect(); plan = self.plan(); self.corrupt_absent_history()
        before = self.catalog()
        with patch.object(maintenance, 'run_crawl') as crawl, patch.object(maintenance, 'Journal') as journal:
            with self.assertRaisesRegex(ValueError, 'maintenance_plan_source_changed'):
                self.execute(plan)
            crawl.assert_not_called(); journal.assert_not_called()
        self.assertEqual(self.catalog(), before)
        self.assertFalse((self.journal/plan['plan_id']).exists())

    def test_only_final_semantic_inspection_replays_all_jobs_atomically_with_original_clocks(self):
        self.inactive_absent(); self.collect()
        plan = self.plan()
        original = maintenance.get_job_source_capture_evidence
        inspections = []
        def observe(connection, job_id):
            inspections.append((job_id, connection.in_transaction))
            return original(connection, job_id)
        with patch.object(maintenance, 'get_job_source_capture_evidence', side_effect=observe),\
                patch.object(maintenance, 'source_fingerprint', wraps=maintenance.source_fingerprint) as fingerprints:
            result = self.execute(plan)
        self.assertEqual(len(fingerprints.call_args_list), 2)  # full rebuilt and final fingerprints
        expected = {job['job_id'] for job in plan['sources'][0]['jobs']}
        self.assertEqual({job_id for job_id, _ in inspections}, expected)
        self.assertEqual(len(inspections), len(expected))
        self.assertTrue(all(atomic for _, atomic in inspections))
        operation = self.operations(result)[0]
        self.assertEqual(operation['status'], 'partially_completed')
        after = operation['result']['after']
        self.assertNotIn('inspection_mode', after)
        self.assertNotIn('history_validation', after)
        with maintenance.read_connection(self.db) as connection:
            support = connection.execute("SELECT id,last_seen_at FROM jobs WHERE external_id='synthetic-maintenance-support'").fetchone()
            absent = connection.execute('SELECT is_active,last_seen_at FROM jobs WHERE id=?', (self.absent_id(),)).fetchone()
            self.assertEqual(support['last_seen_at'], self.at.isoformat())
            self.assertEqual(tuple(absent), (0, T0.isoformat()))
            self.assertEqual(connection.execute('SELECT last_confirmed_at FROM job_source_content_acceptances WHERE job_id=?',
                (support['id'],)).fetchone()[0], self.at.isoformat())
        prepared = [event for event in result['events'] if event['event']=='catalog_commit_prepared']
        self.assertEqual(len(prepared), 1)

    def test_inactive_omitted_history_corruption_rolls_back_all_tentative_catalog_changes(self):
        self.inactive_absent(); self.collect(); self.corrupt_absent_history()
        before = self.catalog()
        plan = self.plan()  # raw fingerprint binds corrupt bytes; it is not a semantic proof
        with self.assertRaises(RuntimeError): self.plan(staged_baseline=False)
        original = maintenance.inspect_source
        final = []
        def inspect(connection, slug, at, **options):
            if not options.get('staged_baseline', False):
                final.append(connection.in_transaction)
                self.assertEqual(connection.execute("SELECT last_seen_at FROM jobs WHERE external_id='synthetic-maintenance-support'").fetchone()[0], self.at.isoformat())
            return original(connection, slug, at, **options)
        with patch.object(maintenance, 'inspect_source', side_effect=inspect): result = self.execute(plan)
        self.assertEqual(final, [True])
        self.assertEqual(self.operations(result)[0]['status'], 'failed')
        self.assertFalse(any(event['event']=='catalog_commit_prepared' for event in result['events']))
        self.assertEqual(self.catalog(), before)

    def test_history_changed_after_rebuild_is_rejected_by_atomic_full_inspection(self):
        self.collect(); plan = self.plan(); before = self.catalog()
        original = maintenance.run_crawl
        def after_rebuild(*args, **options):
            callback = options['before_lifecycle_commit']
            def mutate_then_inspect(connection, company, run_id, summary):
                connection.execute("UPDATE job_source_content_captures SET body=body||' transaction tamper' "
                    'WHERE id=(SELECT accepted_capture_id FROM job_source_content_acceptances WHERE job_id=?)',
                    (self.absent_id(),))
                return callback(connection, company, run_id, summary)
            return original(*args, **dict(options, before_lifecycle_commit=mutate_then_inspect))
        with patch.object(maintenance, 'run_crawl', side_effect=after_rebuild): result = self.execute(plan)
        self.assertEqual(self.operations(result)[0]['status'], 'failed')
        self.assertEqual(self.catalog(), before)
        self.assertFalse(any(event['event']=='catalog_commit_prepared' for event in result['events']))

    def test_incomplete_or_invalid_final_inspector_result_cannot_commit(self):
        self.inactive_absent(); self.collect(); plan = self.plan(); before = self.catalog()
        original = maintenance.inspect_source
        for fault in ('omit_inactive', 'invalid_semantics', 'invalid_evidence', 'deferred'):
            def inspect(connection, slug, at, **options):
                state = original(connection, slug, at, **options)
                if not options.get('staged_baseline', False):
                    missing = next(job for job in state['jobs'] if job['job_id']==self.absent_id())
                    if fault=='omit_inactive': state['jobs'].remove(missing)
                    elif fault=='invalid_semantics': missing['verification']['status']='invalid_provenance'
                    elif fault=='invalid_evidence': missing['evidence']['state']='invalid'
                    else: missing['evidence']['history_validation']='deferred_until_atomic_publication'
                return state
            # Each attempt gets its own plan id; audit-only failed crawl
            # runs are intentionally retained while every catalog row rolls back.
            with self.subTest(fault=fault), patch.object(maintenance, 'inspect_source', side_effect=inspect):
                plan = self.plan()
                result = self.execute(plan)
                self.assertEqual(self.operations(result)[0]['status'], 'failed')
                self.assertEqual(self.catalog(), before)
                self.assertFalse(any(event['event']=='catalog_commit_prepared' for event in result['events']))

    def test_missing_atomic_callback_cannot_be_reported_as_qualified(self):
        self.collect(); plan = self.plan(); before = self.catalog()
        with patch.object(maintenance, 'run_crawl', return_value=(None, Mock(spec=TrackingSummary))):
            result = self.execute(plan)
        self.assertEqual(self.operations(result)[0]['status'], 'failed')
        self.assertEqual(self.catalog(), before)
        self.assertFalse(any(event['event']=='catalog_commit_prepared' for event in result['events']))


if __name__ == '__main__':
    unittest.main()
