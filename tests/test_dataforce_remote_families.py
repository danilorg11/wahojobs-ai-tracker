"""Retained public response replay; no network or production catalog authority."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from wahojobs import daily_source_policy as policy
from wahojobs.crawler.companies.dataforce import crawl_dataforce
from wahojobs.crawler.local_inventory import refresh_request_budget
from wahojobs.crawler.providers import dataforce
from wahojobs.crawler.providers import dataforce_families as families
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome
from wahojobs.source_capture import (prepare_source_capture, prepare_record_promotion_attestation,
    SourceCaptureContext)
from wahojobs.db.repository import (get_connection, create_crawl_run, finish_crawl_run,
    get_job_source_capture_evidence)
from wahojobs.tracking.service import track_crawl_result
from tests.evidence_maintenance_support import BytesResponse
from tests.test_remaining_source_qualification import install_base_schema


class DataForceRemoteFamilies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture=Path(__file__).parent/'fixtures/dataforce_remote_families_v3.json.gz'
        payload=json.loads(gzip.decompress(fixture.read_bytes()))
        cls.pages=payload['pages'];cls.bodies={page['url']:page['body'] for page in cls.pages}
        for page in cls.pages:
            assert sha256(page['body'].encode()).hexdigest()==page['sha256']
        cls.index=[]
        for page in cls.pages:
            if urlparse(page['url']).path=='/projects':
                cls.index.extend(dataforce.parse_jobs_page(page['body'],page['url']))
        cls.at=datetime.fromisoformat(cls.pages[-1]['observed_at'])

    def collect(self, limit=70, fail=None):
        calls=[];events=[];bodies=self.bodies
        class RetainedTransport:
            def open(self,request,timeout):
                calls.append(request.full_url)
                if request.full_url==fail:raise TimeoutError('labelled retained replay detail timeout')
                response=BytesResponse(bodies[request.full_url].encode(),request.full_url)
                response.headers.replace_header('Content-Type','text/html; charset=UTF-8')
                return response
        with patch('urllib.request.build_opener',return_value=RetainedTransport()),\
                patch('socket.create_connection',side_effect=AssertionError('No network in retained replay')),\
                policy.daily_source('dataforce'), refresh_request_budget(http_limit=limit,detail_limit=0,
                    audit_sink=events.append) as budget:
            result=crawl_dataforce(families.ORIGIN+'/projects')
            requests=budget.summary()['http_transactions']
        self.assertEqual(requests,len(calls))
        return result,calls,events

    @staticmethod
    def validate(candidate):
        result=CompanyCrawlResult([candidate],False,'retained contract test','dataforce-community-html',
            outcome=ProviderOutcome.PARTIAL,raw_record_count=1,normalized_record_count=1,
            payload_shape='dataforce_index_detail_record_v1',schema_fingerprint='dataforce_index_detail_record_v1')
        prepared=prepare_source_capture(candidate)
        return prepare_record_promotion_attestation(candidate,prepared,SourceCaptureContext.from_crawl_result(1,result),
            provider='dataforce',source_type=result.source_type)

    def test_all_retained_paid_remote_families_qualify_in_one_daily_bounded_sweep(self):
        self.assertEqual((len(self.pages),len(self.index)),(53,54))
        result,calls,events=self.collect()
        self.assertEqual(len(calls),35)  # three current index pages plus all 32 supported details
        self.assertEqual((len(result.jobs),result.raw_record_count,result.filtered_record_count),(32,54,22))
        self.assertFalse(result.snapshot_complete);self.assertFalse(result.pagination_complete)
        self.assertEqual(result.outcome,ProviderOutcome.PARTIAL)
        self.assertEqual(result.warnings,())
        contracts=Counter(job.record_promotion_attestation.contract_id for job in result.jobs)
        self.assertEqual(contracts,{'dataforce_index_detail_record_v1':8,families.CONTRACT_ID:24})
        family_counts=Counter(job.source_metadata.get('dataforce_remote_family') for job in result.jobs
            if job.record_promotion_attestation.contract_id==families.CONTRACT_ID)
        self.assertEqual(family_counts,{'cadence':10,'ronia':8,'gardenia':4,'triton':1,'tts_casting':1})
        for job in result.jobs:self.validate(job)
        pending=next(event['identities'] for event in events if event.get('event')=='pending_qualification')
        self.assertEqual(pending,['dataforce::study/viola-voice-collection-bengali-pune'])
        self.assertTrue(all('minor' not in url and 'menor' not in url and '/study/' not in url for url in calls))

    def test_real_lifecycle_replay_catalog_clocks_and_partial_omissions(self):
        from scripts.profile_match_digest import get_active_rows
        result,_,_=self.collect()
        with tempfile.TemporaryDirectory() as directory:
            connection=get_connection(Path(directory)/'dataforce.sqlite3')
            try:
                install_base_schema(connection)
                company=connection.execute("INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) "
                    "VALUES('DataForce','dataforce',?,'core','live_feed','count_live')",(families.ORIGIN+'/projects',)).lastrowid
                first=self.at.isoformat();run=create_crawl_run(connection,company,first)
                summary=track_crawl_result(connection,company,run,result,first,model_enrichment=False)
                finish_crawl_run(connection,run,summary,first,status='partial',error_message='partial')
                self.assertEqual((summary.jobs_new,summary.jobs_removed),(32,0))
                rows=list(get_active_rows(connection,source_slugs=['dataforce']))
                self.assertEqual(len(rows),32)
                for row in rows:
                    self.assertEqual(row['latest_successful_source_run_at'],first)
                    evidence=get_job_source_capture_evidence(connection,row['job_id'])
                    self.assertEqual(evidence['state'],'accepted_current')
                casting=connection.execute("SELECT * FROM jobs WHERE external_id LIKE '%voice-talent-casting%'").fetchone()
                self.assertEqual(casting['opportunity_kind'],'public_inventory_opportunity')
                self.assertEqual(casting['include_in_live_market_estimate'],0)
                held=result.jobs[0]
                later=(self.at+timedelta(days=1)).isoformat()
                reduced=replace(result,jobs=result.jobs[1:],normalized_record_count=31,filtered_record_count=23)
                run=create_crawl_run(connection,company,later)
                summary=track_crawl_result(connection,company,run,reduced,later,model_enrichment=False)
                finish_crawl_run(connection,run,summary,later,status='partial',error_message='partial')
                self.assertEqual(summary.jobs_removed,0)
                omitted=connection.execute('SELECT id,is_active,last_seen_at FROM jobs WHERE external_id=?',(held.external_id,)).fetchone()
                self.assertEqual((omitted['is_active'],omitted['last_seen_at']),(1,first))
                self.assertEqual(get_job_source_capture_evidence(connection,omitted['id'])['last_confirmed_at'],first)
                self.assertEqual(connection.execute('SELECT count(*) FROM jobs').fetchone()[0],32)
            finally:connection.close()

    def test_isolated_detail_failure_does_not_discard_siblings_or_claim_full_coverage(self):
        url=families.ORIGIN+'/project/cadence-evaluation-project-br'
        result,calls,events=self.collect(fail=url)
        self.assertEqual(len(calls),35);self.assertEqual(len(result.jobs),31)
        self.assertNotIn('dataforce::project/cadence-evaluation-project-br',{j.external_id for j in result.jobs})
        self.assertIn('dataforce_in_scope_records_unqualified:1',result.warnings)
        pending=next(event['identities'] for event in events if event.get('event')=='pending_qualification')
        self.assertEqual(len(pending),2)
        self.assertFalse(result.snapshot_complete)

    def test_reduced_configuration_exposes_unverified_work_without_exceeding_cap(self):
        result,calls,events=self.collect(limit=15)
        self.assertEqual(len(calls),15);self.assertEqual(len(result.jobs),12)
        self.assertIn('dataforce_in_scope_records_unqualified:20',result.warnings)
        self.assertEqual(len(next(event['identities'] for event in events if event.get('event')=='pending_qualification')),21)
        self.assertEqual(policy.POLICY['dataforce']['http_max'],70)
        self.assertEqual(policy.default_sources()['dataforce']['http_max'],15)
        self.assertFalse(policy.default_sources()['dataforce']['enabled'])
        self.assertEqual(policy.MAX_EXECUTION_SECONDS,2580)

    def test_conflicting_viola_onsite_minors_and_unrelated_roles_never_gain_family_authority(self):
        unsupported=[job for job in self.index if not dataforce.supported_daily_record(job)]
        self.assertEqual(len(unsupported),22)
        for job in unsupported:
            self.assertIsNone(families.supported_family(job))
            if job.url in self.bodies:
                with self.assertRaises(ValueError):families.qualify_record(job,self.bodies[job.url])
        viola=next(job for job in self.index if '/study/viola-voice-collection-bengali-pune' in job.url)
        self.assertEqual(viola.commitment,'Remote')  # contradictory index does not override onsite detail
        self.assertIn('onsite',self.bodies[viola.url].casefold())

    def test_exact_identity_paid_remote_application_and_provenance_tampering_refuse(self):
        job=next(job for job in self.index if job.url.endswith('/cadence-evaluation-project-br'))
        body=self.bodies[job.url]
        changes=(body.replace(job.url,job.url+'-other'),
            body.replace(job.title,'Cadence Evaluation Project - German'),
            body.replace('VMa3uWBvBG','pAWrU5bwgO'),
            body.replace('hub.transperfect.com','attacker.invalid'),
            body.replace('Compensation is task-based','Compensation is unavailable'),
            body.replace('This is a fully&nbsp;<strong>remote</strong>&nbsp;project','This is an onsite project'),
            body.replace('multilingual learning application','unrelated purpose'),
            body.replace('field--name-body','field--name-unrelated'),
            body.replace('</head>',f'<link rel="canonical" href="{job.url}"></head>'))
        for modified in changes:
            with self.subTest(change=sha256(modified.encode()).hexdigest()):
                self.assertNotEqual(modified,body)
                with self.assertRaises(ValueError):families.qualify_record(job,modified)
        candidate=families.qualify_record(job,body)
        for metadata in (dict(candidate.source_metadata,index_card_html=candidate.source_metadata['index_card_html'].replace('Brazil','Germany')),
                         dict(candidate.source_metadata,index_page_url='https://attacker.invalid/projects'),
                         dict(candidate.source_metadata,dataforce_remote_family='ronia')):
            with self.assertRaises(ValueError):self.validate(replace(candidate,source_metadata=metadata))

    def test_visible_adult_eligibility_is_required_and_child_only_contradictions_refuse(self):
        gardenia=next(job for job in self.index if job.url.endswith('/gardenia-speech-collection-fr-non-native'))
        body=self.bodies[gardenia.url]
        # Retain remote/pay/task facts while changing actual visible eligibility.
        adult_html='18 years </strong>or older'
        self.assertTrue(adult_html in body)
        for replacement in ('under 18 years old','available for recording'):
            with self.assertRaisesRegex(ValueError,'eligibility'):
                families.qualify_record(gardenia,body.replace(adult_html,replacement+'</strong>'))
        for contradiction in ('Participants under 18 are eligible.', 'Contributors must be minors.',
                              'Children are welcome.', 'This is a child-only project.'):
            modified=body.replace('</h1>','</h1><p>'+contradiction+'</p>',1)
            self.assertNotEqual(modified,body)
            with self.assertRaisesRegex(ValueError,'eligibility'):
                families.qualify_record(gardenia,modified)
        cadence=next(job for job in self.index if job.url.endswith('/cadence-evaluation-project-br'))
        accepted=families.qualify_record(cadence,self.bodies[cadence.url])
        self.assertIn('students aged',accepted.source_body)
        with self.assertRaisesRegex(ValueError,'eligibility'):
            families.qualify_record(cadence,self.bodies[cadence.url].replace('qualified as a middle or high school educator','interested in education'))
        for attribute in ('inert','aria-hidden="TRUE"','hidden','style="display:none"'):
            modified=body.replace('<main','<main '+attribute,1)
            self.assertNotEqual(modified,body)
            with self.assertRaises(ValueError):families.qualify_record(gardenia,modified)

    def test_new_evidenced_same_family_variant_can_qualify_without_hardcoded_id(self):
        # Explicitly synthetic extension of the retained Ronia country template.
        original=next(job for job in self.index if job.url.endswith('/ronia-remote-photo-collection-denmark'))
        card=original.source_metadata['index_card_html'].replace('denmark','brazil').replace('Denmark','Brazil')
        page='<div class="views-row">'+card
        job=dataforce.parse_jobs_page(page,families.ORIGIN+'/projects')[0]
        detail=self.bodies[original.url].replace('denmark','brazil').replace('Denmark','Brazil').replace('3EGVOCnA8U','RoniaBR1234')
        candidate=families.qualify_record(job,detail)
        self.validate(candidate)
        self.assertEqual(candidate.location,'Brazil')
        self.assertEqual(candidate.record_promotion_attestation.contract_id,families.CONTRACT_ID)
        with self.assertRaises(ValueError):
            families.qualify_record(job,detail.replace('RoniaBR1234','3EGVOCnA8U'))

    def test_casting_preserves_unpaid_screening_and_excludes_live_market_count(self):
        job=next(job for job in self.index if '/voice-talent-casting-' in job.url)
        candidate=families.qualify_record(job,self.bodies[job.url])
        self.assertIn('Sample submissions are not compensated.',candidate.source_body)
        self.assertIn('confirmed upon selection',candidate.source_body)
        self.assertEqual(candidate.opportunity_kind,'public_inventory_opportunity')
        self.assertIs(candidate.include_in_live_market_estimate,False)
        self.validate(candidate)
        with self.assertRaises(ValueError):self.validate(replace(candidate,include_in_live_market_estimate=True))


if __name__=='__main__':unittest.main()
