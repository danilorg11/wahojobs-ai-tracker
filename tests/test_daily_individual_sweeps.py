"""Synthetic source-boundary regressions; no provider calls or production writes."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request

from tests.evidence_maintenance_support import BytesResponse
from tests.test_surge_record_contract_v1 import INDEX, DETAIL, URL
from wahojobs import daily_inventory, daily_source_policy as policy
from wahojobs.crawler.companies.outlier import crawl_outlier
from wahojobs.crawler.companies.surge import crawl_surge
from wahojobs.crawler.local_inventory import refresh_request_budget
from wahojobs.crawler.providers import outlier, dataforce, surge
from wahojobs.crawler.types import evaluate_removal_authorization
from wahojobs.source_capture import (SourceCaptureContext, prepare_source_capture,
    prepare_record_promotion_attestation)


def outlier_row(identity):
    return dict(id=identity, title=f'Android AI Evaluator - Language {identity}',
        absolute_url=f'https://app.outlier.ai/opportunities/{identity}',
        content='<p>Help evaluate mobile AI experiences remotely.</p>',
        internal_job_id=str(identity), allowedCountries=['Brazil'],
        location={'name': 'Remote - Brazil'}, skillNames=['Evaluation'], pod_group='AI')


class OutlierDailySweepTests(unittest.TestCase):
    def collect(self, rows, *, budget=51, failed=()):
        calls=[]; events=[]
        payload=dict(jobs=rows,totalCount=len(rows),page=1,totalPages=1)
        class Transport:
            def open(self, request, timeout):
                calls.append(request.full_url)
                if request.full_url==outlier.INDEX_URL:
                    value=payload
                else:
                    identity=int(request.full_url.rsplit('/',1)[-1])
                    if identity in failed:raise TimeoutError('isolated detail failure')
                    value=dict(next(row for row in rows if type(row) is dict and row.get('id')==identity),
                               signupFlowId='validatedRoleSignup')
                return BytesResponse(json.dumps(value).encode(),request.full_url)
        with policy.daily_source('outlier'), refresh_request_budget(http_limit=budget,audit_sink=events.append), \
                patch('urllib.request.build_opener',return_value=Transport()):
            result=crawl_outlier(outlier.INDEX_URL)
        return result,calls,events

    def test_explicit_larger_budget_sweeps_every_current_supported_id(self):
        rows=[outlier_row(9000000000+i) for i in range(20)]
        result,calls,events=self.collect(rows)
        self.assertEqual(len(result.jobs),20)
        self.assertEqual(len(calls),21)
        self.assertEqual([e['identities'] for e in events if e['event']=='pending_qualification'],[[]])
        self.assertFalse(evaluate_removal_authorization(result).authorized)
        self.assertFalse(result.snapshot_complete)
        self.assertEqual(policy.default_sources()['outlier']['http_max'],9)
        self.assertEqual(policy.POLICY['outlier']['http_max'],51)
        with policy.daily_source('outlier'), policy.observed_outlier_details(
                tuple(outlier.DETAIL_PREFIX+str(row['id']) for row in rows),index_ids={r['id'] for r in rows}):
            policy.validate_request(Request(outlier.DETAIL_PREFIX+str(rows[-1]['id'])))
        context=SourceCaptureContext.from_crawl_result(1,result)
        for job in result.jobs:
            prepare_record_promotion_attestation(job,prepare_source_capture(job),context,
                provider='outlier',source_type=result.source_type)

    def test_cap_remains_partial_and_records_every_unverified_identity(self):
        rows=[outlier_row(9000000000+i) for i in range(60)]
        result,calls,events=self.collect(rows)
        self.assertEqual((len(result.jobs),len(calls)),(50,51))
        self.assertFalse(result.snapshot_complete)
        self.assertFalse(evaluate_removal_authorization(result).authorized)
        pending=next(e['identities'] for e in events if e['event']=='pending_qualification')
        self.assertEqual(set(pending),{r['id'] for r in rows}-{int(j.external_id) for j in result.jobs})
        self.assertIn('outlier_index_records_unqualified:10',result.warnings)
        small,small_calls,_=self.collect(rows[:20],budget=9)
        self.assertEqual((len(small.jobs),len(small_calls)),(8,9))

    def test_bad_card_duplicate_and_failed_detail_do_not_erase_other_exact_observations(self):
        rows=[outlier_row(9000000000+i) for i in range(5)]
        rows.extend([None, {'id': str(rows[1]['id']), 'title': 'ambiguous identity'}, {'unexpected': True}])
        result,calls,events=self.collect(rows,failed={rows[2]['id']})
        self.assertEqual({j.external_id for j in result.jobs},
            {str(rows[i]['id']) for i in (0,3,4)})
        self.assertNotIn(outlier.DETAIL_PREFIX+str(rows[1]['id']),calls)
        pending=next(e['identities'] for e in events if e['event']=='pending_qualification')
        self.assertEqual(set(pending),{rows[1]['id'],rows[2]['id']})
        context=SourceCaptureContext.from_crawl_result(1,result)
        for job in result.jobs:
            prepare_record_promotion_attestation(job,prepare_source_capture(job),context,
                provider='outlier',source_type=result.source_type)


class DataForceDailySweepTests(unittest.TestCase):
    def family(self, language):
        path='/project/thyme-freelance-writer-'+language
        return SimpleNamespace(url='https://dataforcecommunity.transperfect.com'+path,
            external_id='dataforce::'+path.lstrip('/'), title='Thyme Freelance Writer - '+language,
            commitment='Remote',department='Text',expertise='Text',location='Brazil',
            source_metadata={'Country':'Brazil','Type':'Remote'})

    def test_new_supported_family_members_receive_same_daily_priority(self):
        known=[self.family(path.removeprefix('/project/thyme-freelance-writer-'))
               for path in sorted(dataforce.QUALIFIED_DETAIL_PATHS)]
        new=[self.family('language-'+str(i)) for i in range(6)]
        other=SimpleNamespace(url='https://dataforcecommunity.transperfect.com/project/other',
            title='Other remote work',commitment='Remote')
        for day in range(4):
            selected=dataforce.select_daily_detail_pages(known+new+[other],len(known)+len(new),day)
            self.assertEqual(set(map(id,selected)),set(map(id,known+new)))
        days=[dataforce.select_daily_detail_pages(known+new+[other],10,day) for day in range(2)]
        self.assertEqual(set(map(id,sum(days,[]))),set(map(id,known+new)))
        self.assertTrue(all(other not in selected for selected in days))


class SurgeIndependentDetailTests(unittest.TestCase):
    def collect(self, *, failure=None):
        second_url=URL+'-second'
        second_index=INDEX.replace('ai-specialist','ai-specialist-second').replace('AI Specialist','AI Specialist Second')
        second_detail=DETAIL.replace('ai-specialist','ai-specialist-second').replace('AI Specialist','AI Specialist Second')
        calls=[]; events=[]
        def fetch(url, label):
            calls.append(url)
            if url=='https://surgehq.ai/workforce':
                return surge.SurgePage(True,INDEX+second_index,'HTTP 200','success')
            if url.endswith('/fellowship') or failure==url:
                raise TimeoutError('isolated unavailable page')
            return surge.SurgePage(True,DETAIL if url==URL else second_detail,'HTTP 200','success')
        with refresh_request_budget(http_limit=20,audit_sink=events.append), \
                patch.object(surge,'fetch_required_page',side_effect=fetch):
            result=crawl_surge('https://surgehq.ai')
        return result,calls,events,second_url

    def test_excluded_fellowship_failure_keeps_every_workforce_positive(self):
        result,calls,events,_=self.collect()
        self.assertEqual((len(result.jobs),result.raw_record_count,result.rejected_record_count),(2,2,0))
        self.assertIn('surge_fellowship_unavailable_unpublished',result.warnings)
        self.assertFalse(result.snapshot_complete)
        self.assertFalse(evaluate_removal_authorization(result).authorized)
        self.assertEqual(len(calls),4)

    def test_failed_detail_keeps_sibling_and_reports_exact_pending_identity(self):
        result,calls,events,second_url=self.collect(failure=URL)
        self.assertEqual([job.url for job in result.jobs],[second_url])
        self.assertEqual((result.raw_record_count,result.normalized_record_count,
            result.filtered_record_count,result.rejected_record_count),(2,1,0,1))
        self.assertFalse(evaluate_removal_authorization(result).authorized)
        context=SourceCaptureContext.from_crawl_result(1,result)
        candidate=result.jobs[0]
        prepare_record_promotion_attestation(candidate,prepare_source_capture(candidate),context,
            provider='surge',source_type=result.source_type)
        instant=datetime(2026,9,27,tzinfo=timezone.utc)
        report={'events':[{'event':'source_transport','data':event} for event in events]}
        summary=daily_inventory.summarize_source(dict(plan_id='fixture',config=dict(providers=['surge']),
            sources=[dict(jobs=[])]),report,instant,instant)
        self.assertEqual(summary['pending_qualification_ids'],['surge::workforce::ai-specialist'])
        self.assertEqual(summary['pending_qualification_count'],1)


if __name__=='__main__':unittest.main()
