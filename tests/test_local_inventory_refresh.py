"""Ordinary refresh with captured detail bodies and mocked listing transports.

Catalog envelopes and failure mutations are synthetic, not new live evidence.
All writes are confined to newly created databases; no external calls/models.
"""
from contextlib import closing, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from email.message import Message
import hashlib
import gc
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import crawl
from tests.test_provider_detail_recovery import CASES, response
from wahojobs.crawler import pipeline, provider_details
from wahojobs.crawler.local_inventory import inspect_refresh, local_inventory_connection
from wahojobs.crawler.providers import alignerr, mercor, micro1
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import initialize_database, verify_job_source_acceptance_integrity

ROOT=Path(__file__).parent/'fixtures'
GEOGRAPHY=json.loads((ROOT/'mercor_applicant_geography.json').read_text(encoding='utf-8'))
DESCRIPTION=json.loads((ROOT/'mercor_description_geography.json').read_text(encoding='utf-8'))
CATALOG_TIMES={'alignerr':'2026-09-04T14:59:01+00:00','micro1':'2026-09-04T15:00:32+00:00','mercor':DESCRIPTION['observed_at']}


def listing(case):
    if case['provider']=='alignerr':
        return dict(id=case['external_id'],title=case['title'],applyUrl=case['url'],location='Remote',category='STEM',description='Catalog teaser ...')
    return dict(job_id=case['external_id'],job_name=case['title'],apply_url=case['url'],location_type=None,domain_slug='Biology')


def run_saved(target, source, cases=(), *, at=None, details='needed', records=None, detail_fetch=None, envelope_change=None):
    """Mock only transport responses and observation clock; use real adapters/pipeline."""
    at=at or CATALOG_TIMES[source]
    cases=list(cases)
    if source=='alignerr':
        data=dict(jobs=[listing(c) for c in cases],total=len(cases),limit=120,offset=0)
        if envelope_change:envelope_change(data)
        transport=patch.object(alignerr,'request_json',return_value=data)
    elif source=='micro1':
        data=dict(status=True,total=len(cases),data=[listing(c) for c in cases])
        if envelope_change:envelope_change(data)
        transport=patch.object(micro1,'fetch_page',return_value=data)
    else:
        raw=json.dumps({'listings':records}).encode()
        class Response:
            headers=Message()
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def geturl(self):return mercor.MERCOR_ENDPOINT
            def read(self):return raw
        transport=patch.object(mercor,'urlopen',return_value=Response())
    def detail(provider,candidate):
        case=next(c for c in cases if c['external_id']==candidate.external_id)
        assert provider==case['provider']
        # Validate outside any existing write transaction. An independent
        # writer can acquire/release SQLite's reserved lock during retrieval.
        with closing(get_connection(target)) as probe:
            probe.execute('BEGIN IMMEDIATE');probe.rollback()
        return response(case)
    with transport, patch.object(pipeline,'utc_now',return_value=at), \
            patch.object(provider_details,'fetch_detail',side_effect=detail_fetch or detail) as fetch:
        result=pipeline.run_crawl(source,db_path=target,details=details)
    return result[1],fetch.call_count


class LocalInventoryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='wahojobs-refresh-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)  # legacy initializer's SQLite context is not a closing context
        self.path=Path(self.temp.name)/'inventory.sqlite3'
        initialize_database(self.path)
        for target in ('socket.create_connection','wahojobs.tracking.service.tracking_openai_client'):
            guard=patch(target,side_effect=AssertionError('No external/model calls'))
            guard.start();self.addCleanup(guard.stop)

    def rows(self,table,where='',params=()):
        with closing(get_connection(self.path)) as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM '+table+(' WHERE '+where if where else ''),params)]

    def test_seven_captured_details_survive_ordinary_ingestion_and_preserve_catalog_clocks(self):
        for source in ('alignerr','micro1'):
            cases=[c for c in CASES if c['provider']==source]
            summary,calls=run_saved(self.path,source,cases)
            self.assertEqual(calls,len(cases))
            self.assertIn('accepted='+str(len(cases)),summary.warnings[-1])
        for row in self.rows('jobs'):
            case=next(c for c in CASES if c['external_id']==row['external_id'])
            self.assertEqual(row['last_seen_at'],CATALOG_TIMES[case['provider']])
            source=self.rows('job_source_contents','job_id=?',(row['id'],))[0]
            detail=json.loads(source['metadata_json'])[provider_details.DETAIL_KEY]
            self.assertEqual(detail['observed_at'],case['observed_at'])
            self.assertEqual(detail['catalog_observed_at'],CATALOG_TIMES[case['provider']])
            self.assertEqual(detail['response_sha256'],case['source_response_sha256'])
            self.assertFalse(detail['application_acceptance_verified'])
            self.assertEqual(detail['external_id'],row['external_id'])
            self.assertEqual(detail['url'],row['url'])
            self.assertIn('Nice to Have' if case['provider']=='alignerr' and case['rank']!=3 else ('Preferred Qualifications' if case['provider']=='micro1' else 'Requirements'),source['body'])
            with closing(get_connection(self.path)) as conn:verify_job_source_acceptance_integrity(conn,row['id'])
        restricted=next(r for r in self.rows('jobs') if r['external_id']==CASES[3]['external_id'])
        self.assertIn('United States',restricted['location'])

    def test_unchanged_accepted_details_reused_without_new_capture_time(self):
        run_saved(self.path,'alignerr',[CASES[0]])
        before=self.rows('job_source_contents')
        summary,calls=run_saved(self.path,'alignerr',[CASES[0]],at='2026-09-05T14:00:00+00:00',detail_fetch=lambda *a:self.fail('unnecessary detail request'))
        self.assertEqual(calls,0);self.assertIn('reused=1',summary.warnings[-1])
        self.assertEqual(self.rows('job_source_contents'),before)

    def test_changed_catalog_material_fetches_and_all_explicitly_rechecks(self):
        run_saved(self.path,'alignerr',[CASES[0]])
        _,calls=run_saved(self.path,'alignerr',[CASES[0]],envelope_change=lambda p:p['jobs'][0].update(pay='New published terms'))
        self.assertEqual(calls,1)
        _,calls=run_saved(self.path,'alignerr',[CASES[0]],details='all')
        self.assertEqual(calls,1)

    def test_new_catalog_title_and_matching_detail_update_same_identity(self):
        run_saved(self.path,'alignerr',[CASES[0]])
        changed=dict(CASES[0],title=CASES[0]['title']+' — revised')
        original=response(CASES[0])
        updated=replace(original,body=original.body.replace(CASES[0]['title'].encode(),changed['title'].encode()))
        summary,calls=run_saved(self.path,'alignerr',[changed],detail_fetch=lambda *a:updated)
        self.assertEqual(calls,1);self.assertIn('accepted=1',summary.warnings[-1])
        self.assertEqual(self.rows('jobs')[0]['title'],changed['title'])
        self.assertEqual(len(self.rows('jobs')),1)

    def test_failed_empty_and_wrong_identity_details_retain_complete_content(self):
        run_saved(self.path,'alignerr',[CASES[0]])
        before=self.rows('job_source_contents')
        failures=[OSError('offline failure'),replace(response(CASES[0]),body=b''),replace(response(CASES[0]),url=CASES[1]['url'])]
        for failure in failures:
            def fetch(*a):
                if isinstance(failure,Exception):raise failure
                return failure
            summary,_=run_saved(self.path,'alignerr',[CASES[0]],details='all',detail_fetch=fetch)
            self.assertIn('failed=1',summary.warnings[-1]);self.assertEqual(self.rows('job_source_contents'),before)

    def test_absent_partial_records_are_not_refetched_or_removed(self):
        cases=CASES[:2];run_saved(self.path,'alignerr',cases)
        absent=self.rows('jobs','external_id=?',(cases[1]['external_id'],))[0]
        # Existing Alignerr partial pagination contract, synthetic short page.
        with patch.object(alignerr,'request_json',side_effect=[dict(jobs=[listing(cases[0])],total=2,limit=1,offset=0),dict(jobs=[],total=2,limit=1,offset=1)]), \
                patch.object(pipeline,'utc_now',return_value='2026-09-05T14:00:00+00:00'), \
                patch.object(provider_details,'fetch_detail',side_effect=AssertionError('absent/unchanged detail fetch')):
            summary=pipeline.run_crawl('alignerr',db_path=self.path,details='needed')[1]
        self.assertFalse(summary.removals_authorized)
        self.assertEqual(self.rows('jobs','id=?',(absent['id'],))[0],absent)

    def test_mercor_observation_and_geography_use_real_adapter(self):
        absent=dict(deepcopy(DESCRIPTION['record']),listingId='synthetic-absent')
        run_saved(self.path,'mercor',records=[absent],at='2026-09-01T00:00:00+00:00')
        before=self.rows('jobs','external_id=?',('synthetic-absent',))[0]
        rejected=dict(deepcopy(DESCRIPTION['record']),listingId='synthetic-private',isPrivate=True)
        summary,calls=run_saved(self.path,'mercor',records=[DESCRIPTION['record'],GEOGRAPHY['record'],rejected])
        self.assertEqual(calls,0);self.assertFalse(summary.snapshot_complete);self.assertFalse(summary.removals_authorized)
        self.assertEqual(summary.rejected_record_count,1)
        self.assertEqual(self.rows('jobs','id=?',(before['id'],))[0],before)
        sources=self.rows('job_source_contents')
        description=next(r for r in sources if r['external_id']==DESCRIPTION['record']['listingId'])
        self.assertIn('U.S. only',description['body'])
        self.assertIn('wahojobs_applicant_description_geography_v1',description['metadata_json'])
        structured=next(r for r in sources if r['external_id']==GEOGRAPHY['record']['listingId'])
        self.assertIn('eligibleResidenceLocation',structured['metadata_json'])
        for job in self.rows('jobs'):
            if job['external_id']!='synthetic-absent':self.assertEqual(job['last_seen_at'],DESCRIPTION['observed_at'])

    def test_failed_catalog_does_not_fetch_details_or_change_jobs(self):
        run_saved(self.path,'alignerr',[CASES[0]])
        before=self.rows('jobs');sources=self.rows('job_source_contents')
        with patch.object(alignerr,'request_json',side_effect=OSError('retrieval failed')),patch.object(provider_details,'fetch_detail') as fetch:
            with self.assertRaises(OSError):pipeline.run_crawl('alignerr',db_path=self.path,details='needed')
        fetch.assert_not_called();self.assertEqual(self.rows('jobs'),before);self.assertEqual(self.rows('job_source_contents'),sources)

    def test_inspection_is_read_only_and_uses_explicit_target(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest();names=set(self.path.parent.iterdir())
        with patch.dict(pipeline.CRAWLERS,{k:lambda *a:self.fail('inspection dispatched') for k in pipeline.CRAWLERS}),redirect_stdout(io.StringIO()) as out:
            crawl.main(['alignerr','mercor','micro1','--db',str(self.path),'--details','needed','--inspect'])
        plan=json.loads(out.getvalue());self.assertEqual(plan['database'],str(self.path))
        self.assertEqual([s['method'] for s in plan['sources']],['GET','GET','POST'])
        self.assertEqual(plan['sources'][0]['first_request_url'],'https://www.alignerr.com/api/jobs?limit=120&offset=0')
        self.assertEqual(plan['sources'][2]['first_request_url'],'https://prod-api.micro1.ai/api/v1/job/portal?page=1&limit=100&keyword=')
        self.assertEqual(plan['network_requests'],0)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before);self.assertEqual(set(self.path.parent.iterdir()),names)
        with self.assertRaises(ValueError):inspect_refresh(Path('relative.sqlite'),['mercor'])
        with self.assertRaises(Exception):inspect_refresh(self.path.with_name('missing.sqlite'),['mercor'])

    def test_documented_new_database_migrations_accept_normal_initializer(self):
        import importlib
        for name in ('pipeline_state_migration','accounts_migration','ownership_migration',
                     'persistent_profiles_migration','persistent_profile_canonical_v2_migration',
                     'google_oidc_authorization_transactions_migration','closed_schema_convergence_migration'):
            module=importlib.import_module('scripts.'+name)
            with patch('sys.argv',[name,'--db',str(self.path),'--yes']),redirect_stdout(io.StringIO()):
                try:
                    result=module.main()
                except SystemExit as exit_result:
                    result=exit_result.code
                self.assertIn(result,(None,0),name)
        self.assertEqual(len(inspect_refresh(self.path,['alignerr','mercor','micro1'])['sources']),3)

    def test_runtime_ownership_blocks_refresh_before_dispatch(self):
        from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership,release_database_lifetime_ownership,ROLE_DURABLE_RUNTIME,DatabaseLifetimeOwnershipError
        owner=acquire_database_lifetime_ownership(self.path,role=ROLE_DURABLE_RUNTIME)
        try:
            with patch.dict(pipeline.CRAWLERS,{'mercor':lambda *a:self.fail('live target dispatched')}):
                with self.assertRaises(DatabaseLifetimeOwnershipError):pipeline.run_crawl('mercor',db_path=self.path)
        finally:release_database_lifetime_ownership(owner,role=ROLE_DURABLE_RUNTIME,database_path=self.path)
        self.assertEqual(self.rows('crawl_runs'),[])

    def test_legacy_cli_keeps_existing_default_and_details_requires_selection(self):
        with patch.object(crawl,'run_crawl',return_value=(None,None)) as run,patch.object(crawl,'print_crawl_summary'):
            crawl.main(['mercor']);run.assert_called_once_with('mercor')
        with self.assertRaises(ValueError):pipeline.run_crawl('mercor',details='needed')


if __name__=='__main__':unittest.main()
