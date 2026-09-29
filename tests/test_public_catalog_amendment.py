"""Retained role fixtures; no employer transport or applicant information."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_catalog_display_feedback as display_fixtures
from tests import test_mercor_supplemental_retention as retention_fixtures
mercor_page=display_fixtures.mercor_page
from wahojobs.catalog_source_presentation import dataforce_description, surge_role_fields, contribution_context
from wahojobs.catalog_display import advertised_compensation
from wahojobs.candidate_source_display import markdown, pay_facts
from wahojobs.crawler.provider_details import recover_detail, DetailResponse, DETAIL_KEY, reprocess_saved_detail
from wahojobs.crawler.types import JobCandidate
from wahojobs.public_catalog_reader import prepare_publication, PublicCatalogReader
from wahojobs import public_jobs_catalog as catalog, public_job_page as detail

FIXTURE=json.loads((Path(__file__).parent/'fixtures/public_catalog_amendment.json').read_text(encoding='utf8'))


class PublicAmendmentTests(unittest.TestCase):
    setUp=display_fixtures.CatalogDisplayTests.setUp
    pay=display_fixtures.CatalogDisplayTests.pay
    def source(self, provider):
        fixture=FIXTURE[provider]
        self.job.update(company_slug=provider,rich_provider=provider,source_title=fixture['title'],
            external_id=fixture.get('external_id','project-fixture'),rich_external_id=fixture.get('external_id','project-fixture'),
            listing_url=fixture['url'],official_url=fixture['url'],rich_source_url=fixture['url'],
            rich_body=fixture['body'],rich_body_format='text/plain',rich_metadata_json='{}')
        return fixture

    def test_exact_surge_header_is_used_in_both_views_without_borrowing_other_role(self):
        fixture=self.source('surge')
        other='<div data-slug="other" class="workforce-popup"><div data-job="title">Other</div><div data-job="pay-rate">$999 / hour</div></div>'
        self.job['rich_metadata_json']=json.dumps(dict(detail_page_html=other+fixture['html'],index_record={'fields':{'pay-rate':'Contractor'}}))
        before=deepcopy(self.job)
        self.assertEqual(advertised_compensation(self.job),'$200–400 / hour')
        groups,_=prepare_publication([self.job]); job=groups[0]
        self.assertEqual(self.job,before)
        self.assertEqual(job['_public_kind'],'AI training & evaluation')
        self.assertIn('model-generated investment analyses',job['catalog_summary'])
        self.assertFalse(job['catalog_summary'].startswith('- '))
        for page in (catalog.render_job_card(job,return_to='/jobs?q=vc',include_variant=True),
                     detail.render_public_job_page(job,public_origin='https://www.wahojobs.com',public_reader=True)):
            self.assertIn('$200–400 / hour',page)
            self.assertIn('Venture Capital Partner',page)
            self.assertIn('AI training &amp; evaluation',page)
            self.assertNotIn('$999',page)
            self.assertNotIn('USD',page)
        for key in ('source_title','rich_external_id','listing_url'):
            wrong=deepcopy(self.job);wrong[key]='different'
            self.assertEqual(surge_role_fields(wrong),{})

    def test_malformed_optional_surge_header_does_not_block_catalog_readiness(self):
        fixture=self.source('surge')
        for html in ('<div>'*130+fixture['html']+'</div>'*130, ' '*2_000_001):
            self.job['rich_metadata_json']=json.dumps(dict(detail_page_html=html))
            prepared,_=prepare_publication([self.job])
            self.assertEqual(len(prepared),1)
            self.assertIsNone(prepared[0]['_public_compensation'])

    def test_dataforce_exact_article_preserves_french_conditions_and_links(self):
        fixture=self.source('dataforce')
        self.job['rich_metadata_json']='{"retained":true}'
        self.job['rich_body_format']='text/html'
        self.job['rich_body']=('<nav>Apply Here</nav>'+fixture['body']+
            '<aside>Apply Here Share this job</aside><footer><div class="field--name-body">Follow Us Copyright Opt Out Data Privacy</div></footer>')
        text=dataforce_description(self.job)
        for value in ('30 $ USD','15 $ USD','75 $ USD','PayPal','14 à 17','50 $ USD','Tremendous',
                      'frais','NE SERONT PAS','Postuler ici','DataForce.Sourcing@transperfect.com','TransPerfect'):
            self.assertIn(value,text)
        for value in ('Apply Here','Share this job','Follow Us','Copyright','Opt Out','Data Privacy'):
            self.assertNotIn(value,text)
        self.assertNotIn('\n\nImage\n\n',text)
        rendered=markdown(text)
        self.assertIn('<h3>',rendered);self.assertIn('<ul>',rendered);self.assertIn('<a href=',rendered)
        self.assertNotIn('<script',rendered)
        prepared,_=prepare_publication([self.job])
        pay=advertised_compensation(prepared[0])
        for value in ('30 $ USD par tâche terminée et acceptée','bonus de 15 $ USD','total de 75 $ USD'):
            self.assertIn(value,pay)
        self.assertNotIn('50 $ USD',pay)  # transfer minimum is not a reward
        wrong=deepcopy(self.job);wrong['listing_url']=wrong['rich_source_url']='https://dataforcecommunity.transperfect.com/project/other'
        with self.assertRaises(ValueError):dataforce_description(wrong)

    def test_source_unknown_and_company_mission_do_not_invent_role_duties(self):
        label,quote=contribution_context(self.job,'Our mission is to build AI for everyone.\n\nCompetitive pay.',None)
        self.assertEqual((label,quote),('',''))
        self.assertIsNone(advertised_compensation(self.job))

    def test_explicit_source_discipline_conflict_is_held_until_corrected(self):
        self.job.update(company_slug='turing',rich_provider='turing',source_title='Electrical Engineering',
            rich_body='Role Overview:\n\nWe are seeking experienced Aerospace / Flight-Dynamics Engineer to author aero/flight tasks.\n\n'
                'Key Requirements:\n\nCandidates must have a minimum of 5+ years of experience working in Aerospace / Flight-Dynamics. '+
                'Validate designs and document methodologies. '*5)
        prepared,decisions=prepare_publication([self.job])
        self.assertFalse(prepared)
        self.assertEqual(decisions[0]['reason'],'known_title_body_conflict')
        self.job['rich_body']=self.job['rich_body'].replace('Aerospace / Flight-Dynamics','Electrical')
        self.assertTrue(prepare_publication([self.job])[0])

    def test_role_and_future_project_headings_keep_explicit_ai_contributions(self):
        for text in ('## About Mercor projects\n\nTraining and evaluating AI models in Compliance & Risk.',
                     '## About the role\n\nHelp fine-tune large language models with your expertise.',
                     '## Role overview\n\nAnalyze the AI work against production standards.'):
            self.assertEqual(contribution_context(self.job,text,None)[0],'AI training & evaluation')
        from wahojobs.public_catalog_reader import opportunity_label
        self.assertEqual(opportunity_label(dict(source_title='Lawyer Talent Network'),''),'Talent network — future consideration')

    def test_annotator_title_with_explicit_infrastructure_ownership_is_held(self):
        self.job.update(company_slug='turing',rich_provider='turing',
            source_title='Agentic Coding Annotator - Online / Offline Tasks',
            rich_body='Role Overview\n\nWe are looking for an experienced DevOps Engineer to build and operate GPU infrastructure '
                'and production LLM serving systems on Google Cloud Platform (GCP). '
                'You will own infrastructure across the lifecycle—from GPU provisioning and container orchestration '
                'to scalable model inference, observability, and cost optimization.')
        prepared,decisions=prepare_publication([self.job])
        self.assertFalse(prepared)
        self.assertEqual(decisions[0]['reason'],'known_title_body_conflict')
        self.job['source_title']='DevOps and Cloud Infrastructure Engineer'
        self.assertTrue(prepare_publication([self.job])[0])
        self.job['source_title']='Agentic Coding Annotator - Online / Offline Tasks'
        self.job['rich_body']='Review coding tasks and annotate agent trajectories for AI evaluation. '*5
        self.assertTrue(prepare_publication([self.job])[0])

    def test_retained_turing_voice_role_does_not_inherit_company_ai_mission(self):
        from wahojobs.authenticated_card_evidence import _source_text
        fixture=FIXTURE['turing_company_context']
        text=_source_text(dict(source_slug='turing',external_id='voice-fixture',url='https://example.test/voice',
            body=fixture['body'],body_format=fixture['body_format'],metadata_json='{}'))
        job=dict(self.job,source_title=fixture['title'])
        self.assertEqual(contribution_context(job,text,None),('',''))

    def test_literal_one_time_qualifiers_and_non_pay_remain_separate(self):
        for wording in ('$60 / one-time','Up to $75 one-time','From EUR 50 per accepted task','$20–30 per project','~$100+/hour'):
            self.assertEqual(self.pay({'pay':wording}),wording)
        self.assertIsNone(self.pay({},'Referral bonus: $60 one-time'))

    def test_range_and_matching_upper_teaser_survive_old_automatic_flattening(self):
        comp=self.job['enrichment']['attributes']['compensation']
        comp.update(disclosed=True,amount_min=90,amount_max=90,amount_type='exact',period='hour',
            notes='Get paid up to $90/hr to teach AI.')
        self.assertEqual(self.pay({'pay':'$60-90/hr'},'Get paid up to $90/hr to teach AI.'),'$60-90/hr')
        self.assertIsNone(self.pay({'pay':'$60-90/hr'},'Different unconfirmed terms.'))
        self.job['overridden_fields']=['attributes.compensation.amount_min']
        self.assertNotEqual(self.pay({'pay':'$60-90/hr'},'Get paid up to $90/hr to teach AI.'),'$60-90/hr')

    def test_line_boundaries_and_end_of_non_pay_section_are_preserved(self):
        self.assertEqual(self.pay({},'Project Details:\nPay Rate: $20 USD/hour\nRequirements:\nReview language.\nReferral bonus: $5 per task'),'$20 USD/hour')
        self.assertEqual(self.pay({},'Bonuses:\nCFA qualification.\nPerks of freelancing:\nCompensation: ~$100+/hour depending on experience.'),'~$100+/hour')
        self.job['rich_body_format']='text/plain'
        self.assertEqual(self.pay({},'<p><strong>Bonuses:</strong></p><p>Optional CFA.</p><p><strong>Perks:</strong></p><p>Compensation: ~$100+/hour.</p>'),'~$100+/hour')

    def test_conflicting_literal_pay_is_a_variant_hold_not_a_provider_hold(self):
        self.job.update(rich_body='We evaluate AI outputs. '*15+' Pay: $80–$110/hr.',rich_metadata_json='{"pay":"$60-100/hr"}')
        prepared,decisions=prepare_publication([self.job])
        self.assertFalse(prepared)
        self.assertEqual(decisions[0]['reason'],'conflicting_source_compensation')

    def test_prepared_reader_does_not_parse_sources_again_after_cache_expiry(self):
        from tests.test_public_catalog_reader import NOW
        fixture=self.source('surge')
        self.job['rich_metadata_json']=json.dumps(dict(detail_page_html=fixture['html']))
        now=[NOW]
        reader=PublicCatalogReader([self.job],clock=lambda:now[0])
        with patch('wahojobs.catalog_source_presentation.Document',side_effect=AssertionError('no navigation normalization')):
            for elapsed in (0,301):
                now[0]+=timedelta(seconds=elapsed)
                for path in ('/jobs','/jobs/opportunity-'+str(self.job['canonical_opportunity_id'])):
                    response=reader.handle('GET',path,(('Host','www.wahojobs.com'),))
                    self.assertEqual(response.status,200)
                    self.assertIn('$200–400 / hour',response.body.decode())
        self.assertEqual(reader.generation['presentation_version'],4)


class OneTimeRecoveryTests(unittest.TestCase):
    def test_exact_fitness_page_uses_explicit_unit_not_generic_hourly_contract(self):
        fixture=FIXTURE['mercor']
        candidate=JobCandidate(external_id=fixture['external_id'],title=fixture['title'],url=fixture['url'],location='Remote',
            commitment='hourly',source_body=fixture['body'],source_body_format='text/markdown',
            source_metadata={'payRateFrequency':'one-time'})
        hourly=mercor_page(candidate,rate=60)
        packet=hourly.body.decode().replace('per hour','one-time').replace('"payRateFrequency": "hourly"','"payRateFrequency": "one-time"')
        packet=packet.replace('"baseSalary": {"currency": "USD", "value": {"minValue": 60, "maxValue": 60, "unitText": "HOUR"}}','"baseSalary": null')
        response=DetailResponse(fixture['url']+'/fitness-data-contributor',packet.encode(),fixture['observed_at'])
        result=recover_detail('mercor',candidate,response)
        self.assertEqual(result.source_body,fixture['body'])
        self.assertEqual(result.commitment,'hourly')  # raw source label stays evidence
        self.assertEqual(result.source_metadata['pay'],'$60 one-time')
        record=result.source_metadata[DETAIL_KEY]['record']
        self.assertNotIn('salaryCurrency',record)
        self.assertEqual(set(record),{'listingId','title','description','status','isPrivate','deletedAt','rateMin','rateMax','payRateFrequency'})
        for value in ('clinical notes','no consent, no submission','Withdraw consent anytime'):
            self.assertIn(value.lower(),result.source_body.lower())
        self.assertEqual(pay_facts(result.source_metadata,result.source_body)['label'],'$60 one-time')
        bad=replace(response,body=response.body.replace(b'"payRateFrequency": "one-time"',b'"payRateFrequency": "hourly"'))
        with self.assertRaisesRegex(ValueError,'disagree'):recover_detail('mercor',candidate,bad)


class OneTimePublicationTests(unittest.TestCase):
    setUp=retention_fixtures.RetentionTests.setUp
    observe=retention_fixtures.RetentionTests.observe
    load=retention_fixtures.RetentionTests.load
    def test_normal_source_refresh_keeps_separately_dated_one_time_pay(self):
        # Use the ordinary accepted-capture and composition mechanism, not a
        # special presentation constant or a direct update to the source row.
        fields=json.loads(self.refresh['semantic_job_fields_json'])
        candidate=JobCandidate(**fields,source_body=self.refresh['body'],source_body_format='text/plain',
            source_metadata={'payRateFrequency':'one-time'})
        response=mercor_page(candidate,rate=60,currency=None)
        packet=response.body.replace(b'per hour',b'one-time').replace(b'"payRateFrequency": "hourly"',b'"payRateFrequency": "one-time"')
        response=replace(response,body=packet,observed_at=(self.at+timedelta(hours=1)).isoformat())
        # A current returned listing explicitly establishes the new payment unit.
        self.listing['payRateFrequency']='one-time'
        self.observe(self.listing,self.at.isoformat())
        from hashlib import sha256
        from scripts.apply_reviewed_catalog_detail import apply_reviewed_detail
        manifest=dict(version=1,job_id=self.job['id'],expected_accepted_capture_id=self.db.execute(
            'SELECT accepted_capture_id FROM job_source_content_acceptances WHERE job_id=?',(self.job['id'],)).fetchone()[0],
            response_url=response.url,observed_at=response.observed_at,response_sha256=sha256(response.body).hexdigest())
        result=apply_reviewed_detail(self.db,manifest,response.body)
        self.assertTrue(result['listing_and_lifecycle_preserved'])
        with self.assertRaisesRegex(ValueError,'acceptance_drift'):apply_reviewed_detail(self.db,manifest,response.body)
        for offset in (2,24):
            self.observe(self.listing,(self.at+timedelta(hours=offset)).isoformat())
            self.assertEqual(advertised_compensation(self.load()),'$60 one-time')
            accepted=json.loads(self.db.execute('SELECT metadata_json FROM job_source_contents').fetchone()[0])
            self.assertEqual(accepted[DETAIL_KEY]['observed_at'],response.observed_at)
