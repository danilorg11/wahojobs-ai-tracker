"""Offline all-source integration. Transport envelopes are labelled fixtures.

Existing recorded Alignerr content and Greenhouse contract fixtures are reused.
The other small API envelopes are synthetic contract tests, not new observations
or representations of retained raw responses. All network sockets are denied.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from urllib.request import Request

from wahojobs import daily_inventory as daily, evidence_maintenance as maintenance
from wahojobs import daily_source_policy as policy
from wahojobs.crawler import pipeline
from wahojobs.crawler.local_inventory import refresh_request_budget, open_public, request_deadline
from scripts import daily_inventory as cli
from tests.evidence_maintenance_support import new_inventory, source_execute, T0, BytesResponse, records, CASES, listing
from tests.test_micro1_provider_contract import job as micro_job, page as micro_page


def lever(board, *, corporate=False):
    return dict(id='fixture-'+board+('-corporate' if corporate else ''),
        text='Corporate Administrator' if corporate else 'AI Data Specialist',
        hostedUrl='https://jobs.lever.co/'+board+'/fixture',
        descriptionPlain='Evaluate artificial intelligence responses. Work remotely for 10 hours per week.',
        categories=dict(location='Brazil',department='Corporate' if corporate else 'Welo Data - AI Services',commitment='Contract'))


def oneforma():
    return dict(id=42,title={'rendered':'Speech Review Project'},
        content={'rendered':'<p>Record and review natural speech. Remote in Brazil.</p>'},
        link='https://www.oneforma.com/job/42',_embedded={'wp:term':[]},
        acf={'apply_job':[{'language':'English','apply_url':'https://my.oneforma.com/jobs/1'},
                          {'language':'Portuguese','apply_url':'https://my.oneforma.com/jobs/2'}]})


class Transport:
    def __init__(self,fail=None):self.calls=[];self.fail=fail
    def open(self,request,timeout):
        source=policy.current_source();url=request.full_url
        self.calls.append((source,url,timeout))
        if source==self.fail:raise TimeoutError('labelled isolated source timeout')
        headers={}
        if source=='alignerr':payload=dict(jobs=[listing(CASES[0])],total=1,limit=120,offset=0)
        elif source=='mercor':payload=dict(listings=records(absent=False),nextCursor='uninterpreted fixture continuation')
        elif source in ('appen','rws','welocalize'):
            payload=[lever(source)]
            if source!='appen':payload.append(lever(source,corporate=True))
        elif source=='meridial':
            fixtures=json.loads((Path(__file__).parent/'fixtures/greenhouse_provider_contract.json').read_text())
            payload=fixtures['valid_department_hierarchy' if 'departments' in url else 'valid_jobs_inventory']
        elif source=='micro1':payload=micro_page([micro_job('fixture-1')],1)
        elif source=='mindrift':
            if request.get_method()=='GET':payload='Public fixture probe'
            else:payload=dict(total=2,results=[dict(shortcode='M1',title='AI Reviewer',state='published',isInternal=False,
                description='Review AI answers in Portuguese.',location={'country':'Brazil'}),
                dict(shortcode='PRIVATE',title='Private job',state='published',isInternal=True)],nextPage=None)
        elif source=='oneforma':payload=[oneforma()];headers={'X-WP-TotalPages':'1'}
        elif source=='turing':payload=dict(success=True,totalCount=1,jobs=[dict(id='fixture-turing',jobCode='FT1',title='AI Evaluator',description='Review AI-generated code.')])
        else:raise AssertionError('Unexpected fixture source '+str(source))
        response=BytesResponse(json.dumps(payload).encode(),url)
        for name,value in headers.items():response.headers[name]=value
        return response


@contextmanager
def offline(at,transport):
    with ExitStack() as stack:
        stack.enter_context(patch('urllib.request.build_opener',return_value=transport))
        stack.enter_context(patch('socket.create_connection',side_effect=AssertionError('Network prohibited')))
        stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('Unaudited transport prohibited')))
        stack.enter_context(patch('wahojobs.crawler.pipeline.utc_now',return_value=at.isoformat()))
        stack.enter_context(patch.object(maintenance,'clock_now',return_value=at))
        stack.enter_context(patch.object(daily,'now',return_value=at))
        yield


class CoverageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=new_inventory(self.root/'fixture');self.journal=self.root/'fixture/journal'
        source_execute(self.db,self.journal,T0)
        self.config=dict(database=str(self.db),journal=str(self.journal),state_directory=str(self.root/'state'),
            sources=policy.default_sources(),code_commit='a'*40,first_run_at=daily.stamp(T0.replace(hour=6)))

    def test_all_ready_adapters_use_audited_transport_and_publish_new_records(self):
        from scripts.profile_match_digest import get_active_rows
        from wahojobs.public_job_page import load_public_job, public_job_path
        before=daily.protected_domains(self.db);at=T0+timedelta(days=1);transport=Transport()
        with offline(at,transport):daily.collect(self.config,'fixture')
        self.assertEqual(before,daily.protected_domains(self.db))
        self.assertEqual(set(s for s,_,_ in transport.calls),set(policy.READY_SOURCES))
        for source in policy.CORE_SOURCES:
            row=daily.read_json(self.root/'state'/(source+'-state.json'))
            with self.subTest(source=source):
                if source not in policy.READY_SOURCES:
                    self.assertEqual(row['outcome'],'blocked');self.assertFalse(row['qualifying_observation']);self.assertEqual(row['requests_used'],0)
                else:
                    self.assertTrue(row['qualifying_observation'],row)
                    self.assertGreater(row['observed_canonical_opportunities'],0)
                    self.assertEqual(row['requests_used'],sum(s==source for s,_,_ in transport.calls))
                    self.assertLessEqual(row['requests_used'],policy.POLICY[source]['http_max'])
        rows={s:daily.read_json(self.root/'state'/(s+'-state.json')) for s in policy.READY_SOURCES}
        self.assertEqual(rows['oneforma']['upstream_records'],1);self.assertEqual(rows['oneforma']['observed'],2)
        self.assertEqual(rows['welocalize']['upstream_records'],2);self.assertEqual(rows['welocalize']['filtered_records'],1)
        self.assertEqual(rows['mindrift']['upstream_records'],2);self.assertEqual(rows['mindrift']['filtered_records'],1)
        self.assertEqual(rows['mindrift']['requests_used'],3);self.assertEqual(rows['meridial']['requests_used'],2)
        self.assertEqual(rows['mercor']['outcome'],'partial_individual');self.assertEqual(rows['mercor']['confirmed_closed'],0)
        with maintenance.read_connection(self.db) as db:
            matches=list(get_active_rows(db,source_slugs=list(policy.READY_SOURCES)))
            for source in policy.READY_SOURCES:
                jobs=db.execute('SELECT j.* FROM jobs j JOIN companies c ON c.id=j.company_id WHERE c.slug=? AND j.is_active=1',(source,)).fetchall()
                job=next(j for j in jobs if any(r['job_id']==j['id'] for r in matches))
                self.assertIsNotNone(job['canonical_opportunity_id'])
                detail=load_public_job(db,public_job_path(job['canonical_opportunity_id']),now=at,selected_job_id=job['id'])
                self.assertIsNotNone(detail,source)
                accepted=db.execute('SELECT count(*) FROM job_source_content_acceptances a JOIN jobs j ON j.id=a.job_id JOIN companies c ON c.id=j.company_id WHERE c.slug=?',(source,)).fetchone()[0]
                self.assertGreater(accepted,0,source)

    def test_failure_does_not_prevent_later_sources_and_cooldown_is_separate_from_freshness(self):
        at=T0+timedelta(days=1);transport=Transport(fail='appen')
        with offline(at,transport):daily.collect(self.config,'failure')
        self.assertIn('welocalize',{s for s,_,_ in transport.calls})
        self.assertTrue(daily.read_json(self.root/'state/welocalize-state.json')['qualifying_observation'])
        self.assertFalse(daily.read_json(self.root/'state/appen-state.json')['qualifying_observation'])
        next_plan=daily.coverage_plan(self.config,self.db,at+timedelta(hours=1))
        self.assertEqual(next_plan['alignerr']['state'],'due')
        self.assertEqual(next_plan['mindrift']['state'],'cooldown')
        self.assertEqual(daily.parse(next_plan['mindrift']['next_eligible_at']),at+timedelta(hours=12))
        bounded=maintenance.build_plan(self.db,['mindrift'],now=at+timedelta(hours=1),http_limit=70,details=None,phase='source',daily_discovery=True)
        self.assertIn('source_success_cooldown',next(o for o in bounded['operations'] if o['kind']=='catalog_observation')['blocked'])
        self.assertEqual(daily.coverage_plan(self.config,self.db,at+timedelta(hours=12))['mindrift']['state'],'due')

    def test_sample_output_is_rejected_before_tracking_even_if_a_ready_adapter_regresses(self):
        from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate
        result=CompanyCrawlResult(jobs=[JobCandidate('Synthetic','Remote','https://fixture.invalid')],used_sample_data=True,source_type='fixture',source_message='fixture')
        before=daily.protected_domains(self.db)
        with maintenance.read_connection(self.db) as db:count=db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        with policy.daily_source('appen'),patch.dict(pipeline.CRAWLERS,appen=lambda _:result),self.assertRaisesRegex(ValueError,'synthetic'):
            pipeline.run_crawl('appen',db_path=self.db)
        with maintenance.read_connection(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],count)
        self.assertEqual(before,daily.protected_domains(self.db))

    def test_ordinary_maintenance_cannot_bypass_budget_and_preserves_previous_inventory(self):
        at=T0+timedelta(days=1);transport=Transport()
        with offline(at,transport):daily.collect(self.config,'seed')
        before=daily.protected_domains(self.db)
        with maintenance.read_connection(self.db) as db:
            old=[tuple(r) for r in db.execute("SELECT j.* FROM jobs j JOIN companies c ON c.id=j.company_id WHERE c.slug='oneforma' ORDER BY j.id")]
        class TwoPages(Transport):
            def open(self,request,timeout):
                response=super().open(request,timeout)
                response.headers.replace_header('X-WP-TotalPages','2')
                return response
        transport=TwoPages();at+=timedelta(days=1)
        plan=maintenance.build_plan(self.db,['oneforma'],now=at,http_limit=1,details=None,phase='source')
        # Force ordinary maintenance due through a stale source; this is not the
        # daily flag, which previously was the only path enforcing the budget.
        at+=timedelta(days=4)
        plan=maintenance.build_plan(self.db,['oneforma'],now=at,http_limit=1,details=None,phase='source')
        with offline(at,transport):result=maintenance.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=at)
        summary=daily.summarize_source(plan,result,at,at)
        self.assertEqual(len(transport.calls),1);self.assertFalse(summary['qualifying_observation'])
        self.assertTrue(summary['request_cap_reached'])
        responses=[e['data'] for e in result['events'] if e['event']=='source_transport' and e['data'].get('event')=='response']
        self.assertEqual(responses[0]['contract_headers'],{'X-WP-TotalPages':'2'})
        with maintenance.read_connection(self.db) as db:
            current=[tuple(r) for r in db.execute("SELECT j.* FROM jobs j JOIN companies c ON c.id=j.company_id WHERE c.slug='oneforma' ORDER BY j.id")]
        self.assertEqual(old,current);self.assertEqual(before,daily.protected_domains(self.db))

    def test_held_detail_stays_dated_and_is_visible_while_availability_renews(self):
        from tests.evidence_maintenance_support import offline_transport
        at=T0+timedelta(days=1)
        with maintenance.read_connection(self.db) as db:
            old=dict(db.execute("SELECT s.* FROM job_source_contents s JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id WHERE c.slug='alignerr'").fetchone())
        plan=maintenance.build_plan(self.db,['alignerr'],now=at,http_limit=100,details=None,phase='source',daily_discovery=True)
        with offline_transport(at,changed=True):result=maintenance.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=at)
        summary=daily.summarize_source(plan,result,at,at)
        self.assertTrue(summary['qualifying_observation']);self.assertEqual(summary['held_catalog_content'],1)
        self.assertIn('catalog_cannot_replace_accepted_detail',summary['content_hold_reasons'])
        with maintenance.read_connection(self.db) as db:
            current=dict(db.execute("SELECT s.* FROM job_source_contents s JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id WHERE c.slug='alignerr'").fetchone())
        self.assertEqual(current,old);self.assertEqual(summary['requests_used'],1)


class CoveragePolicyTests(unittest.TestCase):
    def test_activation_artifacts_match_the_executable_policy(self):
        root=Path(__file__).parents[1]/'deploy/private-beta'
        manifest=json.loads((root/'daily-inventory-activation-manifest.json').read_text())
        example=json.loads((root/'daily-inventory-v1.example.json').read_text())
        for row in manifest['sources']:
            self.assertEqual({k:row[k] for k in policy.POLICY[row['source']]},policy.POLICY[row['source']])
        self.assertEqual(example['sources'],policy.default_sources())
        self.assertFalse(example['enabled']);self.assertFalse(example['alert_delivery']['approved'])
        self.assertEqual(example['alert_delivery']['recipient'],policy.ALERT_RECIPIENT)
        self.assertEqual(manifest['aggregate']['http_max'],policy.aggregate(example['sources'])['http_max'])
        self.assertIn('TimeoutStartSec='+str(daily.EXECUTION_SECONDS),(root/'wahojobs-inventory.service').read_text())
        self.assertIn(':40:',(root/'wahojobs-inventory-health.timer').read_text())

    def test_policy_accounts_for_every_source_and_rejects_widening_or_blocked_activation(self):
        settings=policy.default_sources();policy.validate_sources(settings)
        self.assertEqual(len(settings),15);self.assertEqual(len(policy.READY_SOURCES),10)
        self.assertEqual(policy.aggregate(settings),dict(http_max=232,execution_seconds=2040))
        for source in settings:
            changed=deepcopy(settings);changed[source]['http_max']+=1
            with self.assertRaises(ValueError):policy.validate_sources(changed)
            if source not in policy.READY_SOURCES:
                changed=deepcopy(settings);changed[source]['enabled']=True
                with self.assertRaises(ValueError):policy.validate_sources(changed)
                with self.assertRaises(ValueError),policy.daily_source(source):pass

    def test_exact_endpoint_body_and_cross_source_guards(self):
        endpoints=[('appen','https://api.lever.co/v0/postings/rws?mode=json&expand=location'),
            ('mercor','https://aws.api.mercor.com/work/listings-explore-page?cursor=guessed'),
            ('micro1','https://jobs.micro1.ai/post/fixture'),('mindrift','https://apply.workable.com/api/v3/accounts/another/jobs')]
        for source,url in endpoints:
            with policy.daily_source(source),self.assertRaises(ValueError):policy.validate_request(Request(url))
        with policy.daily_source('turing'):
            for payload in ({},dict(searchQuery='',expertise=[],location=[],pageNumber=True,pageSize=500,sortingCriteria='newest')):
                with self.assertRaises(ValueError):policy.validate_request(Request('https://work.turing.com/api/jobs/all',data=json.dumps(payload).encode()))
        request=Request('https://api.lever.co/v0/postings/appen?mode=json&expand=location')
        request.add_unredirected_header('Authorization','fixture-do-not-send')
        with policy.daily_source('appen'),self.assertRaises(ValueError):policy.validate_request(request)

    def test_request_cap_redirect_and_time_limit_reserve_before_dispatch(self):
        url='https://api.lever.co/v0/postings/appen?mode=json&expand=location'
        with policy.daily_source('appen'),refresh_request_budget(http_limit=1),patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=HTTPError(url,301,'Moved',{'Location':'https://example.invalid'},io.BytesIO(b'redirect'))
            with self.assertRaises(HTTPError):open_public(Request(url),timeout=30)
            with self.assertRaises(OSError):open_public(Request(url),timeout=30)
            self.assertEqual(opener.return_value.open.call_count,1)
        with policy.daily_source('appen'),refresh_request_budget(http_limit=1),request_deadline(1),patch('wahojobs.crawler.local_inventory.time.monotonic',return_value=2),patch('urllib.request.build_opener') as opener:
            with self.assertRaises(TimeoutError):open_public(Request(url),timeout=30)
            opener.assert_not_called()

    def test_workable_429_has_no_daily_retry_and_totals_are_integral(self):
        from wahojobs.crawler.providers import workable_markdown as workable, turing, micro1
        with policy.daily_source('mindrift'),patch.object(workable,'urlopen',side_effect=HTTPError('fixture',429,'rate limited',{},None)) as transport,patch.object(workable.time,'sleep') as sleep:
            with self.assertRaises(HTTPError):workable.fetch_api_page('https://apply.workable.com/api/v3/accounts/toloka-ai/jobs',{})
            self.assertEqual(transport.call_count,1);sleep.assert_not_called()
        with self.assertRaises(ValueError):micro1.validated_total({'total':0.5})
        for module,key in ((workable,'total'),(turing,'totalCount')):
            with self.assertRaises(ValueError):module.validated_total_count({key:1.5})

    def test_native_source_timeout_continues_and_phase_reservations_do_not_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);config=dict(state_directory=temp,sources=policy.default_sources())
            target=root/'runs/fixture';target.mkdir(parents=True)
            daily.write_json(target/'coverage-plan.json',{s:dict(state='due' if s in policy.READY_SOURCES else 'blocked') for s in policy.CORE_SOURCES})
            native=cli.NativeOperations(config,root/'policy');calls=[]
            def phase(run_id,name,deadline):
                calls.append(name)
                if name=='collect-appen':raise TimeoutError()
            with patch.object(native,'phase',side_effect=phase):native.collect('fixture',daily.EXECUTION_SECONDS)
            self.assertEqual(calls,['prepare',*('collect-'+s for s in policy.READY_SOURCES)])
            receipt=dict(run_id='fixture',outcome='running',supervisor_pid=123,execution_deadline_monotonic=1000,
                active_phase=dict(name='appen',deadline=60))
            daily.write_json(target/'run.json',receipt)
            with self.assertRaises(ValueError):cli.claim_dispatch(root,'fixture',123,1,'rws')
            self.assertEqual(cli.claim_dispatch(root,'fixture',123,1,'appen'),59)
            with self.assertRaises(FileExistsError):cli.claim_dispatch(root,'fixture',123,2,'appen')
            with self.assertRaises(ValueError):cli.claim_dispatch(root,'fixture',123,61,'appen')

    def test_native_cancellation_restores_without_dispatching_siblings(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            daily.write_json(target/'coverage-plan.json',{s:dict(state='due') for s in policy.CORE_SOURCES})
            native=cli.NativeOperations(dict(state_directory=temp,sources=policy.default_sources()),'fixture');calls=[]
            def phase(run_id,name,deadline):
                calls.append(name)
                if name=='collect-appen':raise InterruptedError()
            with patch.object(native,'phase',side_effect=phase),self.assertRaises(InterruptedError):native.collect('fixture',2040)
            self.assertEqual(calls,['prepare','collect-alignerr','collect-appen'])

    def test_alerts_batch_all_coverage_issues_to_approved_recipient_without_hourly_repeats(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(state_directory=temp,sources=policy.default_sources(),first_run_at=daily.stamp(T0.replace(hour=6)),
                alert_delivery=dict(recipient=policy.ALERT_RECIPIENT,command=['/reviewed/test-adapter']))
            with patch.object(daily,'_baseline_cohorts',return_value={}):
                state=daily.health(config,T0)
            with patch.object(cli.subprocess,'run') as transport:
                cli.deliver(config,state)
                packet=json.loads(transport.call_args.kwargs['input'])
                self.assertEqual(packet['recipient'],'danilo@wahojobs.com')
                self.assertGreater(len(packet['events']),15)
                with patch.object(daily,'_baseline_cohorts',return_value={}):
                    cli.deliver(config,daily.health(config,T0+timedelta(hours=1)))
                self.assertEqual(transport.call_count,1)


if __name__=='__main__':unittest.main()
