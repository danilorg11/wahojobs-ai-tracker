"""Operational ceilings and no-redirect behavior; mocked public transports only."""
from contextlib import closing
from email.message import Message
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, HTTPSHandler, build_opener
from urllib.response import addinfourl

from tests.test_micro1_provider_contract import job, page
from tests.test_local_inventory_refresh import run_saved, CASES, listing, response
from wahojobs.crawler import local_inventory as limits
from wahojobs.crawler.providers import alignerr, mercor, micro1
from wahojobs.crawler.types import ProviderOutcome, evaluate_removal_authorization
from wahojobs.crawler.provider_details import fetch_detail
from tests.test_provider_detail_recovery import candidate
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import initialize_database


class RefreshRequestLimitTests(unittest.TestCase):
    def test_micro1_completes_at_exact_page_and_record_limits(self):
        pages=[page([job(f'p{p}-r{r}') for r in range(100)],5000) for p in range(50)]
        with patch.object(micro1,'fetch_page',side_effect=pages) as fetch:
            result=micro1.fetch_micro1_snapshot('https://example.test')
        self.assertEqual(fetch.call_count,50)
        self.assertEqual(result.raw_record_count,5000)
        self.assertTrue(evaluate_removal_authorization(result).authorized)

    def test_micro1_page_cap_and_oversized_total_are_partial(self):
        scenarios=[([page([job(str(i))],51) for i in range(50)],50,50),
                   ([page([job('one')],5001)],1,1)]
        for pages,calls,count in scenarios:
            with self.subTest(count=count),patch.object(micro1,'fetch_page',side_effect=pages) as fetch:
                result=micro1.fetch_micro1_snapshot('https://example.test')
                self.assertEqual(fetch.call_count,calls)
                self.assertEqual(result.raw_record_count,count)
                self.assertEqual(result.outcome,ProviderOutcome.PARTIAL)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_micro1_counts_oversized_return_before_deduplication(self):
        with patch.object(micro1,'fetch_page',return_value=page([job('duplicate')]*5001,5001)) as fetch:
            result=micro1.fetch_micro1_snapshot('https://example.test')
        self.assertEqual(fetch.call_count,1);self.assertEqual(result.raw_record_count,5001)
        self.assertEqual(result.normalized_record_count,0)
        self.assertEqual(result.outcome,ProviderOutcome.PARTIAL)
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_catalog_redirects_never_dispatch_a_second_request(self):
        cases=[lambda:alignerr.request_json('https://www.alignerr.com/api/jobs'),
               lambda:mercor.fetch_mercor_observations(mercor.MERCOR_ENDPOINT),
               lambda:micro1.fetch_page('https://prod-api.micro1.ai/api/v1/job/portal',1,100)]
        for call in cases:
            sent=[]
            class Redirect(HTTPSHandler):
                def https_open(self, request):
                    sent.append(request.full_url)
                    headers=Message();headers['Location']='https://unexpected.example/forbidden'
                    result=addinfourl(io.BytesIO(b''),headers,request.full_url,302)
                    result.msg='Found';return result
            with patch('urllib.request.build_opener',side_effect=lambda *handlers:build_opener(Redirect(),*handlers)),limits.refresh_request_budget() as budget:
                with self.assertRaises(HTTPError):call()
                self.assertEqual(len(sent),1);self.assertNotIn('unexpected',sent[0])
                self.assertEqual(budget.summary()['http_transactions'],1)
                self.assertEqual(budget.transactions[0]['status'],302)

    def test_batch_ceiling_counts_catalog_and_detail_attempts_without_retries(self):
        class Failure:
            def open(self,*args,**kwargs):raise OSError('synthetic failed attempt')
        with limits.refresh_request_budget() as budget,patch('urllib.request.build_opener',return_value=Failure()),patch('wahojobs.crawler.provider_details.build_opener',return_value=Failure()):
            for _ in range(500):
                with self.assertRaises(OSError):fetch_detail('alignerr',candidate(CASES[0]))
            with self.assertRaises(limits.RequestBudgetExceeded):fetch_detail('alignerr',candidate(CASES[0]))
            for _ in range(500):
                with self.assertRaises(OSError):limits.open_catalog(Request(mercor.MERCOR_ENDPOINT),timeout=30)
            with self.assertRaises(limits.RequestBudgetExceeded):limits.open_catalog(Request(mercor.MERCOR_ENDPOINT),timeout=30)
            self.assertEqual(budget.summary()['http_transactions'],1000)
            self.assertEqual(budget.detail_requests,500)
        with limits.refresh_request_budget() as independent:self.assertEqual(independent.detail_requests,0)

    def test_catalog_budget_exhaustion_preserves_collected_partial_records(self):
        for provider,first,call in [
            (micro1,page([job('one')],2),lambda:micro1.fetch_micro1_snapshot('https://example.test')),
            (alignerr,dict(jobs=[listing(CASES[0])],total=2,limit=1,offset=0),lambda:alignerr.fetch_alignerr_snapshot('https://example.test'))]:
            name='fetch_page' if provider is micro1 else 'request_json'
            with patch.object(provider,name,side_effect=[first,limits.RequestBudgetExceeded('ceiling')]):
                result=call()
                self.assertEqual(result.outcome,ProviderOutcome.PARTIAL)
                self.assertEqual(len(result.jobs),1)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_partial_and_pending_detail_updates_preserve_accepted_content(self):
        import gc
        with tempfile.TemporaryDirectory(prefix='refresh-ceiling-') as temp:
            target=Path(temp)/'inventory.sqlite3';initialize_database(target);gc.collect()
            a=CASES[0];m=next(c for c in CASES if c['provider']=='micro1')
            run_saved(target,'alignerr',[a]);run_saved(target,'micro1',[m])
            with closing(get_connection(target)) as conn:
                before=[tuple(r) for r in conn.execute("SELECT s.* FROM job_source_contents s JOIN jobs j ON s.job_id=j.id JOIN companies c ON c.id=j.company_id WHERE c.slug='micro1'")]
                absent=dict(conn.execute('SELECT * FROM jobs WHERE external_id=?',(m['external_id'],)).fetchone())
            def consume(provider,item):
                limits.reserve_http_request(Request(item.url),detail=True)
                return response(a if provider=='alignerr' else m)
            with limits.refresh_request_budget() as budget,patch.object(limits,'MAX_DETAIL_REQUESTS',1):
                first,_=run_saved(target,'alignerr',[a],detail_fetch=consume,envelope_change=lambda d:d['jobs'][0].update(pay='changed'))
                second,_=run_saved(target,'micro1',[m],detail_fetch=consume,envelope_change=lambda d:d['data'][0].update(role_type='changed'))
                self.assertIn('accepted=1',first.warnings[-1])
                self.assertIn('pending=1',second.warnings[-1])
                self.assertTrue(second.snapshot_complete)
                self.assertEqual(budget.detail_requests,1)
            with closing(get_connection(target)) as conn:
                after=[tuple(r) for r in conn.execute("SELECT s.* FROM job_source_contents s JOIN jobs j ON s.job_id=j.id JOIN companies c ON c.id=j.company_id WHERE c.slug='micro1'")]
            self.assertEqual(before,after)
            new=dict(m,external_id='synthetic-new',url='https://jobs.micro1.ai/post/synthetic-new')
            partial,_=run_saved(target,'micro1',[new],details=None,envelope_change=lambda d:d.update(total=5001))
            self.assertFalse(partial.snapshot_complete);self.assertFalse(partial.removals_authorized)
            with closing(get_connection(target)) as conn:
                retained=dict(conn.execute('SELECT * FROM jobs WHERE external_id=?',(m['external_id'],)).fetchone())
            self.assertEqual(retained['last_seen_at'],absent['last_seen_at'])
            self.assertEqual(retained['is_active'],1)


if __name__=='__main__':unittest.main()
