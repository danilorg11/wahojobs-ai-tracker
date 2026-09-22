"""Actual retained public Mercor regression through tracking and shared consumers."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from wahojobs import public_job_page, public_jobs_catalog
from wahojobs.catalog_display import advertised_compensation
from wahojobs.crawler import pipeline
from wahojobs.crawler.providers.mercor import parse_mercor_observations, MERCOR_ENDPOINT
from wahojobs.crawler.provider_details import DETAIL_KEY, reprocess_saved_detail
from wahojobs.crawler.types import JobCandidate, RecordPromotionAttestation, PROVIDER_DETAIL_RECORD_CONTRACT_ID
from wahojobs.db.repository import (install_base_schema, create_crawl_run, finish_crawl_run,
    upsert_job_source_content, verify_job_source_acceptance_integrity)
from wahojobs.source_capture import SourceCaptureContext
from wahojobs.tracking.service import track_crawl_result
from tests.test_catalog_display_feedback import mercor_page
from scripts.profile_match_digest import get_active_rows

FIXTURE = json.loads((Path(__file__).parent/'fixtures/mercor_supplemental_retention.json').read_text(encoding='utf8'))

class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close); install_base_schema(self.db)
        self.company=self.db.execute("INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES('Mercor','mercor',?,'core','live_feed','count_live')",(MERCOR_ENDPOINT,)).lastrowid
        self.old=FIXTURE['historical'];self.refresh=FIXTURE['refresh']
        fields=json.loads(self.refresh['semantic_job_fields_json'])
        self.listing=dict(json.loads(self.refresh['metadata_json']),listingId=fields['external_id'],
            title=fields['title'], location=fields['location'],commitment=fields['commitment'],
            description=self.refresh['body'],status='active',isPrivate=False,deletedAt=None)
        if self.refresh['source_updated_at']:self.listing['updatedAt']=self.refresh['source_updated_at']
        self.at=datetime.fromisoformat(self.refresh['observed_at'])
        self.observe(self.listing,self.old['observed_at'])
        self.job=self.db.execute('SELECT * FROM jobs').fetchone()
        metadata=json.loads(self.old['metadata_json']);d=metadata[DETAIL_KEY]
        candidate=JobCandidate(**json.loads(self.old['semantic_job_fields_json']),
            source_body=self.old['body'],source_body_format=self.old['body_format'],source_metadata=metadata,
            source_updated_at=self.old['source_updated_at'],record_promotion_attestation=RecordPromotionAttestation(
                contract_id=PROVIDER_DETAIL_RECORD_CONTRACT_ID,body_observation='present',authority_evidence={
                    'url':d['url'],'external_id':d['external_id'],'response_sha256':d['response_sha256'],
                    'observed_at':d['observed_at'],'content_only':True}))
        context=SourceCaptureContext(None,'partial',False,False,False,False,1,1,1,0,
            PROVIDER_DETAIL_RECORD_CONTRACT_ID,PROVIDER_DETAIL_RECORD_CONTRACT_ID)
        result=upsert_job_source_content(self.db,self.job['id'],'mercor','mercor-marketplace',candidate,
            self.old['observed_at'],capture_context=context)
        self.accepted=result.capture_id;self.db.commit()
        self.baseline=dict(self.db.execute('SELECT * FROM job_source_contents').fetchone())

    def observe(self,listing,at,*,outcome=None):
        result=parse_mercor_observations({'listings':[listing]})
        if outcome is not None:result=replace(result,outcome=outcome)
        run=create_crawl_run(self.db,self.company,at)
        summary=track_crawl_result(self.db,self.company,run,result,at,model_enrichment=False)
        finish_crawl_run(self.db,run,summary,at,status='partial',error_message='partial catalog')
        self.db.commit();return run

    def load(self):
        return public_job_page.load_public_job(self.db,public_job_page.public_job_path(self.job['canonical_opportunity_id']),now=self.at,selected_job_id=self.job['id'])

    def test_real_omission_and_repeated_refresh_preserve_pay_provenance_and_availability(self):
        for offset in (0,24,48):
            at=self.at+timedelta(hours=offset)
            run=self.observe(self.listing,at.isoformat())
            self.assertEqual(dict(self.db.execute('SELECT * FROM job_source_contents').fetchone()),self.baseline)
            verify_job_source_acceptance_integrity(self.db,self.job['id'])
            view=self.load()
            self.assertEqual(advertised_compensation(view),'$50 per hour USD')
            self.assertEqual(view['source_run_id'],run)
            self.assertEqual(view['latest_successful_source_run_at'],at.isoformat())
            self.assertIn('$50 per hour USD',public_jobs_catalog.render_job_card(view,return_to='/jobs',include_variant=True))
            self.assertIn('$50 per hour USD',public_job_page.render_public_job_page(view,public_origin='https://beta.example'))
            matching=next(dict(r) for r in get_active_rows(self.db) if r['job_id']==self.job['id'])
            self.assertEqual(matching['job_id'],view['job_id'])
            self.assertEqual(matching['latest_successful_source_run_at'],view['latest_successful_source_run_at'])
            self.assertEqual(view['source_commitment'],'hourly')

    def test_retained_refresh_through_bounded_maintenance_keeps_pay_and_original_date(self):
        import tempfile
        from wahojobs import daily_inventory as daily, evidence_maintenance as maintenance
        from tests.evidence_maintenance_support import BytesResponse
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'inventory.sqlite3'
            with closing(sqlite3.connect(path)) as disk:self.db.backup(disk)
            plan=maintenance.build_plan(path,['mercor'],now=self.at,http_limit=1,details=None,phase='source',daily_discovery=True)
            class RetainedRefresh:
                def open(inner,request,timeout):
                    self.assertEqual(request.full_url,MERCOR_ENDPOINT)
                    return BytesResponse(json.dumps({'listings':[self.listing]}).encode(),request.full_url)
            with patch('urllib.request.build_opener',return_value=RetainedRefresh()),patch('socket.create_connection',side_effect=AssertionError('No network')),patch('wahojobs.crawler.pipeline.utc_now',return_value=self.at.isoformat()):
                report=maintenance.execute_plan(plan,root/'journal',authorized=True,authorize_sources=True,now=self.at)
            summary=daily.summarize_source(plan,report,self.at,self.at)
            self.assertTrue(summary['qualifying_observation']);self.assertEqual(summary['requests_used'],1)
            with maintenance.read_connection(path) as disk:
                accepted=dict(disk.execute('SELECT * FROM job_source_contents').fetchone())
                self.assertEqual(accepted,self.baseline)
                verify_job_source_acceptance_integrity(disk,self.job['id'])
                view=public_job_page.load_public_job(disk,public_job_page.public_job_path(self.job['canonical_opportunity_id']),now=self.at,selected_job_id=self.job['id'])
                self.assertEqual(advertised_compensation(view),'$50 per hour USD')

    def test_summary_explicit_null_pay_is_unknown_but_changed_terms_invalidate_reuse(self):
        self.observe(dict(self.listing,payRate=None),self.at.isoformat())
        self.assertEqual(advertised_compensation(self.load()),'$50 per hour USD')
        self.observe(dict(self.listing,payRate=80), (self.at+timedelta(hours=1)).isoformat())
        current=json.loads(self.db.execute('SELECT metadata_json FROM job_source_contents').fetchone()[0])
        self.assertNotIn(DETAIL_KEY,current)
        self.assertEqual(current['payRate'],80)
        verify_job_source_acceptance_integrity(self.db,self.job['id'])

    def test_actual_refresh_staged_then_published_keeps_original_pay_and_verification_dates(self):
        import tempfile
        from wahojobs import evidence_maintenance as maintenance, daily_inventory as daily
        from wahojobs.crawler import staged_observation as staged
        from tests.evidence_maintenance_support import BytesResponse
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'inventory.sqlite3';journal=root/'journal';run=root/'run'
            with closing(sqlite3.connect(path)) as disk:self.db.backup(disk)
            class RetainedRefresh:
                def open(inner,request,timeout):
                    self.assertEqual(request.full_url,MERCOR_ENDPOINT)
                    return BytesResponse(json.dumps({'listings':[self.listing]}).encode(),request.full_url)
            with patch('urllib.request.build_opener',return_value=RetainedRefresh()),patch('socket.create_connection',side_effect=AssertionError('No network')),patch.object(pipeline,'utc_now',return_value=self.at.isoformat()):
                staged.collect('mercor',MERCOR_ENDPOINT,run,run_id='retained',code_commit='a'*40,http_max=1,journal_root=journal)
            later=self.at+timedelta(minutes=20)
            with patch.object(pipeline,'utc_now',return_value=later.isoformat()),patch.object(maintenance,'clock_now',return_value=later),patch('urllib.request.build_opener',side_effect=AssertionError('No publication HTTP')):
                observation,collection=staged.load(run,'mercor',run_id='retained',code_commit='a'*40,journal_root=journal,consume=True)
                plan=maintenance.build_plan(path,['mercor'],now=later,http_limit=1,details=None,phase='source',daily_discovery=True)
                from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR
                lease=acquire_database_lifetime_ownership(path,role=ROLE_OFFLINE_OPERATOR)
                try:published=maintenance.execute_plan(plan,journal,authorized=True,authorize_sources=True,now=later,observation=observation,ownership=lease)
                finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=path)
            self.assertEqual(published['events'][-1]['data']['request_usage']['http_transactions'],0)
            summary=daily.summarize_source(plan,staged.publication_report(collection,published),self.at,later)
            self.assertTrue(summary['qualifying_observation'],summary)
            self.assertEqual(daily.parse(summary['last_qualifying_verification']),self.at)
            with maintenance.read_connection(path) as disk:
                self.assertEqual(dict(disk.execute('SELECT * FROM job_source_contents').fetchone()),self.baseline)
                view=public_job_page.load_public_job(disk,public_job_page.public_job_path(self.job['canonical_opportunity_id']),now=later,selected_job_id=self.job['id'])
                self.assertEqual(advertised_compensation(view),'$50 per hour USD')
                self.assertEqual(view['latest_successful_source_run_at'],self.at.isoformat())

    def test_changed_body_and_explicit_non_pay_empty_are_not_silently_merged(self):
        self.observe(dict(self.listing,description=self.listing['description']+'\nUpdated workload: 5 hours.'),self.at.isoformat())
        self.assertIn(DETAIL_KEY,json.loads(self.db.execute('SELECT metadata_json FROM job_source_contents').fetchone()[0]))
        self.assertEqual(advertised_compensation(self.load()), '$50 per hour USD')
        self.assertIn('Updated workload: 5 hours.', self.load()['rich_body'])
        self.observe(dict(self.listing,description=self.listing['description']+'\nUpdated workload: 5 hours.'),(self.at+timedelta(hours=1)).isoformat())
        verify_job_source_acceptance_integrity(self.db,self.job['id'])
        self.assertEqual(advertised_compensation(self.load()), '$50 per hour USD')

    def test_later_validated_pay_supersedes_with_own_date_and_currency(self):
        fields=json.loads(self.old['semantic_job_fields_json'])
        candidate=JobCandidate(**fields,source_body=self.old['body'],source_body_format=self.old['body_format'])
        response=replace(mercor_page(candidate,rate=60,currency='CAD'),observed_at=self.at.isoformat())
        reprocess_saved_detail(self.db,self.job['id'],response)
        self.assertEqual(advertised_compensation(self.load()),'$60 per hour CAD')
        detail=json.loads(self.db.execute('SELECT metadata_json FROM job_source_contents').fetchone()[0])[DETAIL_KEY]
        self.assertEqual(detail['observed_at'],self.at.isoformat())
        older=replace(mercor_page(candidate,rate=70),observed_at=self.old['observed_at'])
        with self.assertRaises(ValueError):reprocess_saved_detail(self.db,self.job['id'],older)
        verify_job_source_acceptance_integrity(self.db,self.job['id'])

    def test_sibling_and_manual_overrides_do_not_inherit_retained_pay(self):
        self.observe(dict(self.listing,listingId='list_sibling'),self.at.isoformat())
        self.observe(self.listing,self.at.isoformat())
        sibling=self.db.execute("SELECT metadata_json FROM job_source_contents WHERE external_id='list_sibling'").fetchone()[0]
        self.assertNotIn(DETAIL_KEY,json.loads(sibling))
        view=self.load();view['overridden_fields']=['attributes.compensation.disclosed']
        view['enrichment']['attributes']['compensation']['disclosed']=None
        self.assertIsNone(advertised_compensation(view))
        view['enrichment']['attributes']['compensation'].update(disclosed=True,amount_min=40,amount_max=40,currency='EUR',period='hour',amount_type='exact')
        self.assertEqual(advertised_compensation(view),'EUR 40 per hour')


    def test_composed_pay_replaced_by_later_detail_stays_replaced_on_next_workload_change(self):
        updated=dict(self.listing,description=self.listing['description']+'\nWorkload: 5 hours per week.')
        self.observe(updated,self.at.isoformat())
        from wahojobs.authenticated_card_evidence import load_card_sources,_source_text
        self.assertIn('Workload: 5 hours per week.',_source_text(load_card_sources(self.db,[{'job_id':self.job['id']}])[self.job['id']]))
        fields=json.loads(self.old['semantic_job_fields_json'])
        candidate=JobCandidate(**fields,source_body=updated['description'],source_body_format='text/plain')
        response=replace(mercor_page(candidate,rate=60,currency='CAD'),observed_at=(self.at+timedelta(hours=1)).isoformat())
        reprocess_saved_detail(self.db,self.job['id'],response)
        self.observe(dict(updated,description=updated['description']+'\nNew responsibility: review tasks.'),(self.at+timedelta(hours=2)).isoformat())
        self.assertEqual(advertised_compensation(self.load()),'$60 per hour CAD')
        packet=json.loads(self.load()['rich_metadata_json'])[DETAIL_KEY]
        self.assertEqual(packet['observed_at'],response.observed_at)
        verify_job_source_acceptance_integrity(self.db,self.job['id'])

    def test_old_composition_cannot_resurrect_superseded_pay_and_rolls_back(self):
        self.observe(dict(self.listing,description=self.listing['description']+'\nWorkload: 5 hours per week.'),self.at.isoformat())
        capture=dict(self.db.execute('SELECT * FROM job_source_content_captures ORDER BY id DESC LIMIT 1').fetchone())
        self.observe(dict(self.listing,payRate=80),(self.at+timedelta(hours=1)).isoformat())
        before=self.db.total_changes;count=self.db.execute('SELECT COUNT(*) FROM job_source_content_captures').fetchone()[0]
        candidate=JobCandidate(**json.loads(capture['semantic_job_fields_json']),source_body=capture['body'],
            source_body_format=capture['body_format'],source_metadata=json.loads(capture['metadata_json']),
            source_updated_at=capture['source_updated_at'],record_promotion_attestation=RecordPromotionAttestation(
                contract_id=capture['record_promotion_contract_id'],body_observation=capture['body_observation'],
                authority_evidence=json.loads(capture['authority_evidence_json'])))
        from wahojobs.mercor_supplemental import CONTRACT
        context=SourceCaptureContext(None,'partial',False,False,False,False,1,1,1,0,CONTRACT,CONTRACT)
        with self.assertRaises(ValueError):upsert_job_source_content(self.db,self.job['id'],'mercor','mercor-marketplace',candidate,capture['observed_at'],capture_context=context)
        self.assertEqual(count,self.db.execute('SELECT COUNT(*) FROM job_source_content_captures').fetchone()[0])
        self.assertNotIn(DETAIL_KEY,json.loads(self.load()['rich_metadata_json']))
        verify_job_source_acceptance_integrity(self.db,self.job['id'])

    def test_explicit_unpaid_correction_withdraws_retained_pay(self):
        self.observe(dict(self.listing,description=self.listing['description']+'\nThis role is not paid.'),self.at.isoformat())
        self.assertNotIn(DETAIL_KEY,json.loads(self.load()['rich_metadata_json']))
        self.assertNotEqual(advertised_compensation(self.load()),'$50 per hour USD')

    def test_same_valid_catalog_timestamp_allows_omission_and_later_detail(self):
        from copy import deepcopy
        fixture=deepcopy(FIXTURE)
        for name in ('historical','refresh'):fixture[name]['source_updated_at']='2026-09-20T00:00:00+00:00'
        with patch.dict(FIXTURE,fixture):
            case=RetentionTests();case.setUp()
            try:
                run=case.observe(case.listing,case.at.isoformat())
                self.assertEqual(case.load()['source_run_id'],run)
                self.assertEqual(advertised_compensation(case.load()),'$50 per hour USD')
                case.test_later_validated_pay_supersedes_with_own_date_and_currency()
            finally:case.doCleanups()


    def test_historical_policy_reproduces_observed_pay_loss(self):
        with patch('wahojobs.db.repository.MERCOR_PROMOTION_POLICY_VERSION','mercor_record_promotion_v1'):
            self.observe(self.listing,self.at.isoformat())
        self.assertIsNone(advertised_compensation(self.load()))
        historical=self.db.execute('SELECT metadata_json FROM job_source_content_captures WHERE id=?',(self.accepted,)).fetchone()[0]
        self.assertEqual(json.loads(historical)['pay'],'$50 per hour')

    def test_existing_hosted_v1_detail_history_survives_new_v2_summary_policy(self):
        with patch('wahojobs.db.repository.MERCOR_PROMOTION_POLICY_VERSION','mercor_record_promotion_v1'),patch('wahojobs.db.repository.MERCOR_DETAIL_PROMOTION_POLICY_VERSION','provider_detail_content_promotion_v1'):
            case=RetentionTests();case.setUp()
        try:
            old_policy=case.db.execute('SELECT promotion_policy_version FROM job_source_content_captures WHERE id=?',(case.accepted,)).fetchone()[0]
            self.assertEqual(old_policy,'provider_detail_content_promotion_v1')
            case.observe(case.listing,case.at.isoformat())
            self.assertEqual(advertised_compensation(case.load()),'$50 per hour USD')
            self.assertEqual(json.loads(case.load()['rich_metadata_json'])[DETAIL_KEY]['observed_at'],case.old['observed_at'])
            verify_job_source_acceptance_integrity(case.db,case.job['id'])
        finally:case.doCleanups()

    def test_unqualified_geography_does_not_clear_prior_qualifying_restriction(self):
        from wahojobs.matching.source_geography import apply_mercor_applicant_geography
        restricted=dict(self.listing,eligibleResidenceLocation=['USA'])
        self.observe(restricted,self.at.isoformat())
        self.observe(dict(self.listing,eligibleResidenceLocation=[]),(self.at+timedelta(hours=1)).isoformat(),outcome='anomalous')
        def projected():return apply_mercor_applicant_geography(self.db,[dict(job_id=self.job['id'],source_slug='mercor')])[0]
        self.assertEqual(projected()['applicant_geography_evidence']['fields']['eligibleResidenceLocation'],['USA'])
        self.observe(dict(self.listing,eligibleResidenceLocation=[]),(self.at+timedelta(hours=2)).isoformat())
        self.assertNotIn('applicant_geography_evidence',projected())

    def test_retained_response_through_normal_run_crawl(self):
        import tempfile
        from tests.evidence_maintenance_support import BytesResponse
        with tempfile.TemporaryDirectory(prefix='retained-mercor-refresh-') as directory:
            path=Path(directory)/'inventory.sqlite3'
            with closing(sqlite3.connect(path)) as copy:self.db.backup(copy)
            raw=json.dumps({'listings':[self.listing]}).encode()
            with patch('wahojobs.crawler.providers.mercor.urlopen',return_value=BytesResponse(raw,MERCOR_ENDPOINT)),patch.object(pipeline,'utc_now',return_value=self.at.isoformat()):
                company,summary=pipeline.run_crawl('mercor',db_path=path,details=None)
            with closing(sqlite3.connect(path)) as copy:
                copy.row_factory=sqlite3.Row
                view=public_job_page.load_public_job(copy,public_job_page.public_job_path(self.job['canonical_opportunity_id']),now=self.at,selected_job_id=self.job['id'])
                self.assertEqual(advertised_compensation(view),'$50 per hour USD')
                self.assertEqual(json.loads(view['rich_metadata_json'])[DETAIL_KEY]['observed_at'],self.old['observed_at'])
                verify_job_source_acceptance_integrity(copy,self.job['id'])
            self.assertEqual(summary.jobs_removed,0)

if __name__=='__main__':unittest.main()
