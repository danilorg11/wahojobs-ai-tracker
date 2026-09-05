"""Source-fair recovery: simulated HTTP accounting and disposable ingestion only."""
from contextlib import closing, redirect_stdout
import gc
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

from scripts import crawl
from tests.test_local_inventory_refresh import CASES, run_saved, response
from wahojobs.crawler import local_inventory as limits
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import initialize_database


def detail_request(source, index):
    base = {'alignerr': 'https://www.alignerr.com/jobs/',
            'micro1': 'https://jobs.micro1.ai/post/'}[source]
    return Request(base + str(index))


class DetailBudgetAllocationTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.create_connection', side_effect=AssertionError('No network'))
        guard.start(); self.addCleanup(guard.stop)

    def simulate_cli(self, order, workloads, failed_source=None):
        sent = []
        def run(source, **options):
            limits.reserve_http_request(Request('https://www.alignerr.com/api/jobs' if source == 'alignerr'
                                               else 'https://prod-api.micro1.ai/api/v1/job/portal', method='GET' if source == 'alignerr' else 'POST'))
            if source == failed_source:
                raise OSError('synthetic catalog failure')
            pending = 0
            for index in range(workloads[source]):
                request = detail_request(source, index)
                try: limits.reserve_http_request(request, detail=True)
                except limits.RequestBudgetExceeded: pending += 1
                else: sent.append((source, index))
            limits.record_detail_recovery(source, dict(pending=pending))
            return object(), object()
        output = io.StringIO()
        with patch.object(crawl, 'run_crawl', side_effect=run) as catalogs, \
                patch.object(crawl, 'print_crawl_summary'), redirect_stdout(output):
            crawl.main([*order, '--db', str(Path(tempfile.gettempdir())/'unused.sqlite3'), '--details', 'needed'])
        self.assertEqual([call.args[0] for call in catalogs.call_args_list], list(dict.fromkeys(order)))
        self.assertEqual(len(sent), len(set(sent)))
        return json.loads(output.getvalue().splitlines()[-1])['request_usage']

    def test_reported_workloads_receive_250_each_in_both_orders(self):
        for order in [('alignerr', 'micro1'), ('micro1', 'alignerr')]:
            with self.subTest(order=order):
                result = self.simulate_cli(order, {'alignerr': 5619, 'micro1': 312})
                self.assertEqual(result['detail_requests'], 500)
                self.assertEqual(result['http_transactions'], 502)
                for source, pending in [('alignerr', 5369), ('micro1', 62)]:
                    state = result['detail_allocations'][source]
                    self.assertEqual(state['requests'], 250)
                    self.assertEqual(state['recovery']['pending'], pending)

    def test_unused_allowance_transfers_forward_but_never_repeats_a_source(self):
        result = self.simulate_cli(['micro1', 'alignerr'], {'micro1': 12, 'alignerr': 5619})
        self.assertEqual(result['detail_allocations']['micro1']['released'], 238)
        self.assertEqual(result['detail_allocations']['alignerr']['requests'], 488)
        reverse = self.simulate_cli(['alignerr', 'micro1'], {'micro1': 12, 'alignerr': 5619})
        self.assertEqual(reverse['detail_requests'], 262)
        self.assertEqual(reverse['detail_allocations']['micro1']['unused'], 238)

    def test_single_source_duplicates_and_failed_catalog_release(self):
        single = self.simulate_cli(['micro1', 'micro1'], {'micro1': 600})
        self.assertEqual(single['detail_allocations']['micro1']['requests'], 500)
        failed = self.simulate_cli(['alignerr', 'micro1'], {'alignerr': 0, 'micro1': 600}, 'alignerr')
        self.assertIsNone(failed['detail_allocations']['alignerr']['recovery'])
        self.assertEqual(failed['detail_allocations']['micro1']['requests'], 500)

    def test_total_transaction_ceiling_overrides_allocations(self):
        with limits.refresh_request_budget(sources=['micro1', 'alignerr']) as budget:
            for _ in range(999): limits.reserve_http_request(Request('https://www.alignerr.com/api/jobs'))
            limits.reserve_http_request(detail_request('micro1', 0), detail=True)
            budget.finish_source('micro1')
            with self.assertRaises(limits.RequestBudgetExceeded):
                limits.reserve_http_request(detail_request('alignerr', 0), detail=True)
            self.assertEqual(len(budget.transactions), 1000)
            self.assertEqual(budget.detail_requests, 1)

    def test_odd_allocation_is_name_deterministic(self):
        with patch.object(limits, 'MAX_DETAIL_REQUESTS', 5):
            self.assertEqual(limits.detail_allocations(['micro1', 'mercor', 'alignerr']), {'alignerr': 3, 'micro1': 2})

    def test_inspection_reports_reservations_without_requests_or_writes(self):
        with tempfile.TemporaryDirectory(prefix='detail-fairness-') as temp:
            target = Path(temp)/'inventory.sqlite3'; initialize_database(target); gc.collect()
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            with patch.object(limits, 'reserve_http_request', side_effect=AssertionError('inspection request')):
                result = limits.inspect_refresh(target, ['micro1', 'mercor', 'alignerr'], details='needed')
            self.assertEqual(result['detail_allocations'], {'alignerr': 250, 'micro1': 250})
            self.assertEqual(result['network_requests'], 0)
            self.assertEqual(result['limits']['http_transactions'], 1000)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)
            self.assertEqual(list(Path(temp).iterdir()), [target])

    def test_missing_details_precede_rechecks_and_preserve_accepted_content(self):
        with tempfile.TemporaryDirectory(prefix='detail-fairness-') as temp:
            target = Path(temp)/'inventory.sqlite3'; initialize_database(target); gc.collect()
            a, missing = CASES[:2]
            run_saved(target, 'alignerr', [a])
            with closing(get_connection(target)) as conn:
                before = tuple(conn.execute('SELECT * FROM job_source_contents').fetchone())
            sent = []
            def fetch(provider, item):
                limits.reserve_http_request(Request(item.url), detail=True)
                sent.append(item.external_id)
                return response(missing)
            with patch.object(limits, 'MAX_DETAIL_REQUESTS', 1), limits.refresh_request_budget(sources=['alignerr']) as budget:
                summary, _ = run_saved(target, 'alignerr', [a, missing], detail_fetch=fetch,
                    envelope_change=lambda d: d['jobs'][0].update(pay='changed'))
                self.assertEqual(sent, [missing['external_id']])
                self.assertIn('accepted=1', summary.warnings[-1])
                self.assertIn('pending=1', summary.warnings[-1])
                self.assertTrue(summary.snapshot_complete)
                self.assertEqual(budget.summary()['detail_allocations']['alignerr']['recovery']['pending'], 1)
            with closing(get_connection(target)) as conn:
                self.assertEqual(tuple(conn.execute('SELECT * FROM job_source_contents ORDER BY job_id LIMIT 1').fetchone()), before)
            gc.collect()

    def test_reuse_is_free_failed_attempt_consumes_share_and_other_source_proceeds(self):
        with tempfile.TemporaryDirectory(prefix='detail-fairness-') as temp:
            target = Path(temp)/'inventory.sqlite3'; initialize_database(target); gc.collect()
            a, changed = CASES[:2]; m = next(c for c in CASES if c['provider'] == 'micro1')
            run_saved(target, 'alignerr', [a, changed])
            with closing(get_connection(target)) as conn:
                before = [tuple(r) for r in conn.execute('SELECT * FROM job_source_contents ORDER BY job_id')]
            def fetch(provider, item):
                limits.reserve_http_request(Request(item.url), detail=True)
                if provider == 'alignerr': raise OSError('synthetic failure')
                return response(m)
            with patch.object(limits, 'MAX_DETAIL_REQUESTS', 2), limits.refresh_request_budget(sources=['alignerr', 'micro1']) as budget:
                summary, calls = run_saved(target, 'alignerr', [a, changed], detail_fetch=fetch,
                    envelope_change=lambda d: d['jobs'][1].update(pay='changed'))
                self.assertEqual(calls, 1)
                self.assertIn('reused=1', summary.warnings[-1]); self.assertIn('failed=1', summary.warnings[-1])
                budget.finish_source('alignerr')
                other, _ = run_saved(target, 'micro1', [m], detail_fetch=fetch)
                self.assertTrue(other.snapshot_complete)
                self.assertIn('accepted=1', other.warnings[-1])
                self.assertEqual(budget.detail_requests, 2)
            with closing(get_connection(target)) as conn:
                self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM job_source_contents ORDER BY job_id LIMIT 2')], before)
            gc.collect()


if __name__ == '__main__': unittest.main()
