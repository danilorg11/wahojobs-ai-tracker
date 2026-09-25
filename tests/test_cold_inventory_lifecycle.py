"""Offline lifecycle regressions: source preparation, expiry and isolation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import sqlite3
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from tests.test_public_jobs_catalog import PublicJobsCatalogTests, NOW, ReadOnlyProvider
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs import public_jobs_catalog as catalog, public_job_page, opportunity_enrichment
from wahojobs.matching import evaluation_memo as memo
from scripts import private_beta_app


class CatalogPreparationTests(unittest.TestCase):
    setUp = PublicJobsCatalogTests.setUp
    integration = PublicJobsCatalogTests.integration

    def load(self, preparation, now=NOW):
        with ReadOnlyProvider(self.path)() as connection:
            return catalog.load_public_jobs(connection, now=now, preparation=preparation)

    def write(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(sql, args)

    def test_expiry_refresh_reuses_preparation_with_exact_cold_output_and_facets(self):
        cache = catalog.CatalogPreparation()
        self.load(cache)
        later = NOW + timedelta(seconds=301)
        with patch.object(opportunity_enrichment, 'validate_enrichment_document', side_effect=AssertionError('revalidated')):
            with patch.object(public_job_page, 'prepare_public_job_variants', side_effect=AssertionError('reprojected')):
                reused = self.load(cache, later)
        cold = self.load(None, later)
        self.assertEqual(reused, cold)
        self.assertEqual(catalog.build_catalog(reused, {}), catalog.build_catalog(cold, {}))
        expired = NOW + timedelta(days=10)
        self.assertEqual(self.load(cache, expired), self.load(None, expired))
        # Backwards clocks may re-admit evidence only under the original policy.
        self.assertEqual(self.load(cache), self.load(None))

    def test_actual_content_changes_invalidate_even_without_updating_recorded_hash(self):
        cache = catalog.CatalogPreparation()
        before = self.load(cache)
        self.write("UPDATE job_source_contents SET body=body || ' Specific new search text.'")
        after = self.load(cache)
        self.assertNotEqual(before, after)
        self.assertEqual(after, self.load(None))
        self.write("UPDATE jobs SET is_active=0")
        self.assertEqual(self.load(cache), [])

    def test_override_add_delete_and_enrichment_delete_have_cold_parity(self):
        cache = catalog.CatalogPreparation()
        original = self.load(cache)
        self.write("INSERT INTO opportunity_enrichment_overrides "
                   "(canonical_opportunity_id,field_path,operation,actor,reason) "
                   "VALUES (9002,'attributes.role.work_activities','set_unknown','synthetic','test')")
        self.assertNotEqual(self.load(cache), original)
        self.assertEqual(self.load(cache), self.load(None))
        self.write("DELETE FROM opportunity_enrichment_overrides WHERE field_path='attributes.role.work_activities'")
        self.assertEqual(self.load(cache), original)
        self.write('DELETE FROM opportunity_enrichments')
        self.assertEqual(self.load(cache), self.load(None))

    def test_queued_and_mid_preparation_expiry_do_not_publish_stale_jobs(self):
        current = [NOW]
        integration = self.integration(now=lambda: current[0])
        self.assertTrue(integration._load_public_jobs_inventory())
        class ExpiringLock:
            def __enter__(self):
                current[0] += timedelta(days=10)
            def __exit__(self, *args):
                pass
        integration._public_jobs_cache_lock = ExpiringLock()
        self.assertEqual(integration._load_public_jobs_inventory(), ())
        current[0] = NOW
        integration = self.integration(now=lambda: current[0])
        original = catalog.load_public_jobs
        def expires(*args, **kwargs):
            result = original(*args, **kwargs)
            current[0] += timedelta(days=10)
            return result
        with patch.object(catalog, 'load_public_jobs', side_effect=expires):
            self.assertEqual(integration._load_public_jobs_inventory(), ())

    def test_invalid_enrichment_never_serves_previous_prepared_result_and_recovers(self):
        integration = self.integration()
        integration._load_public_jobs_inventory()
        with ReadOnlyProvider(self.path)() as connection:
            original = connection.execute('SELECT automatic_document_json FROM opportunity_enrichments').fetchone()[0]
        self.write("UPDATE opportunity_enrichments SET automatic_document_json='{}'")
        with self.assertRaises(ValueError):
            integration._load_public_jobs_inventory()
        self.assertIsNone(integration._public_jobs_cache)
        self.write('UPDATE opportunity_enrichments SET automatic_document_json=?', (original,))
        self.assertEqual(integration._load_public_jobs_inventory(), tuple(self.load(None)))

    def test_threads_share_one_preparation_and_restart_discards_it(self):
        integration = self.integration()
        with patch.object(catalog, 'load_public_jobs', wraps=catalog.load_public_jobs) as load:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: integration._load_public_jobs_inventory(), range(4)))
            self.assertEqual(load.call_count, 1)
        self.assertTrue(all(result == results[0] for result in results))
        integration.close()
        self.assertEqual(integration._catalog_preparation.variants, {})
        with patch.object(catalog, 'load_public_jobs', wraps=catalog.load_public_jobs) as load:
            self.assertEqual(self.integration()._load_public_jobs_inventory(), results[0])
            self.assertEqual(load.call_count, 1)


class RetainedMatchesTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)

    def test_plain_navigation_reuses_and_different_owner_recomputes(self):
        self.assertEqual(self.f.get().status, 200)
        original = self.f.integration._load_inventory
        with patch.object(type(self.f.integration), '_load_inventory', side_effect=AssertionError('recomputed')):
            self.assertEqual(self.f.get().status, 200)
            self.f.owner = 'b'
            self.assertEqual(self.f.get().status, 503)
        self.assertEqual(self.f.get().status, 200)
        self.assertEqual(self.f.last_run().owner_profile_id, 'synthetic-owner-b')

    def test_missing_clock_stays_unverified_but_new_evidence_invalidates(self):
        self.f.update_inventory('UPDATE crawl_runs SET status="failed"')
        self.assertEqual(self.f.get().status, 200)
        run = self.f.last_run()
        proof = run.recommendation_context['_authenticated_reuse']
        self.assertGreater(proof['valid_until'], proof['evaluated_at'])
        self.f.advance(1)
        with patch.object(type(self.f.integration), '_load_inventory', side_effect=AssertionError('recomputed')):
            self.assertEqual(self.f.get().status, 200)
            self.f.update_inventory('UPDATE crawl_runs SET status="success"')
            self.assertEqual(self.f.get().status, 503)


class StartupAndMemoTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'Actual Linux hosted alarm lifecycle')
    def test_actual_alarm_interrupts_before_bind_and_restores_handler_in_subprocess(self):
        code = '''
import signal, time
from unittest.mock import Mock, patch
from scripts import private_beta_app as app
from wahojobs.workos_authkit_staging import WorkOSAuthKitStagingError
previous = signal.getsignal(signal.SIGALRM)
for fail in (True, False):
    events = []
    config = Mock(proxy_secret='synthetic')
    runtime = Mock(bind_address=('127.0.0.1', 0))
    runtime.prepare_serving_inventory.side_effect = lambda: time.sleep(.2 if fail else 0)
    runtime.close.side_effect = lambda **kw: events.append('closed')
    server = Mock()
    def bind(*args):
        events.append('bind')
        return server
    def alarm(seconds):
        signal.setitimer(signal.ITIMER_REAL, .05 if seconds else 0)
    with patch.object(app, 'load_workos_authkit_staging_configuration', return_value=config), patch.object(app, 'make_remote_handler', return_value=object()), patch.object(signal, 'alarm', side_effect=alarm):
        try:
            app.run('synthetic', runtime_builder=lambda _: runtime, server_factory=bind, ready=lambda _: events.append('ready'))
            assert not fail
        except WorkOSAuthKitStagingError:
            assert fail
    assert events == (['closed'] if fail else ['bind', 'ready', 'closed']), events
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0., 0.)
    config.clear_secrets.assert_called_once()
print('actual_alarm_lifecycle_passed')
'''
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('actual_alarm_lifecycle_passed', result.stdout)

    def test_native_restore_reserves_preparation_without_extending_total_budget(self):
        from scripts import daily_inventory as daily
        operations = daily.NativeOperations({'database': 'synthetic.sqlite'}, 'synthetic-policy')
        with patch.object(daily, 'bounded_process') as process, \
             patch.object(daily.time, 'monotonic', side_effect=[0, 12, 70]):
            operations.restore(120)
        self.assertEqual([call.kwargs['timeout'] for call in process.call_args_list], [40, 75, 50])
        with patch.object(daily, 'bounded_process', side_effect=TimeoutError) as process:
            with self.assertRaises(TimeoutError):
                operations.restore(20)
            self.assertEqual(process.call_count, 1)

    def test_no_listener_or_readiness_before_preparation_and_failure_closes_runtime(self):
        for fails in (False, True):
            events = []
            config = Mock(proxy_secret='synthetic')
            runtime = Mock(bind_address=('127.0.0.1', 0))
            def prepare():
                events.append('prepare')
                if fails:
                    raise ValueError('synthetic preparation failure')
            runtime.prepare_serving_inventory.side_effect = prepare
            runtime.close.side_effect = lambda **kw: events.append('closed')
            server = Mock()
            server.serve_forever.side_effect = lambda **kw: events.append('serve')
            def factory(*args):
                events.append('bind')
                return server
            with patch.object(private_beta_app, 'load_workos_authkit_staging_configuration', return_value=config), \
                 patch.object(private_beta_app, 'make_remote_handler', return_value=object()), \
                 patch.object(private_beta_app.signal, 'signal'):
                def call():
                    private_beta_app.run('synthetic', runtime_builder=lambda _: runtime, server_factory=factory,
                                         ready=lambda _: events.append('ready'))
                if fails:
                    with self.assertRaises(ValueError):
                        call()
                    self.assertEqual(events, ['prepare', 'closed'])
                else:
                    call()
                    self.assertEqual(events, ['prepare', 'bind', 'ready', 'serve', 'closed'])
            config.clear_secrets.assert_called_once()

    def test_memo_copies_values_bounds_storage_and_clears_after_failure(self):
        calls = []
        @memo.memoized_text
        def parse(value):
            calls.append(value)
            return [{'value': value}]
        @memo.evaluation_scope
        def evaluate():
            parse('same')[0]['value'] = 'mutated'
            self.assertEqual(parse('same'), [{'value': 'same'}])
            for number in range(8200):
                parse(str(number))
            self.assertLessEqual(max(map(len, memo._current.get().values())), 8192)
            raise ValueError('synthetic')
        with self.assertRaises(ValueError):
            evaluate()
        self.assertIsNone(memo._current.get())
        self.assertEqual(calls.count('same'), 1)
        parse('same')
        self.assertEqual(calls.count('same'), 2)
