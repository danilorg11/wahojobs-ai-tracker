"""Focused labelled fixtures; real retained-source checks live with the review receipt."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest

from wahojobs import public_job_page as detail, public_jobs_catalog as catalog
from wahojobs.catalog_display import advertised_compensation, location_summary
from wahojobs.candidate_source_display import pay_facts
from wahojobs.crawler.provider_details import DETAIL_KEY, DetailResponse, recover_detail, reprocess_saved_detail
from wahojobs.crawler.types import JobCandidate
from wahojobs.opportunity_enrichment import blank_document
from tests.test_public_job_page import seed_public_job, JOB_PATH, OBSERVED_AT


class CatalogDisplayTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close); seed_public_job(self.db)
        self.job = detail.load_public_job(self.db, JOB_PATH, now=datetime.fromisoformat(OBSERVED_AT))
        self.job['enrichment'] = blank_document()
        self.job.update(source_location='Remote', source_commitment=None, rich_body='', rich_metadata_json='{}',
                        rich_provider=self.job['company_slug'])
        catalog.prepare_catalog_presentation(self.job)

    def card(self, job=None):
        return catalog.render_job_card(job or self.job, return_to='/jobs?language=English', include_variant=True)

    def page(self):
        return detail.render_public_job_page(self.job, public_origin='https://app.test')

    def pay(self, metadata=None, body=''):
        self.job.update(rich_metadata_json=json.dumps(metadata or {}), rich_body=body)
        return advertised_compensation(self.job)

    def test_unknown_omitted_without_changing_filter_or_shared_eligibility(self):
        for provider in ('acme-ai', 'mercor'):
            self.job.update(company_slug=provider, rich_provider=provider)
            catalog.prepare_catalog_presentation(self.job)
            before = deepcopy(self.job)
            shared = deepcopy(detail.candidate_job_eligibility(self.job))
            card, page = self.card(), self.page()
            self.assertEqual(location_summary(self.job), 'Remote')
            self.assertNotIn('unconfirmed', card)
            self.assertNotIn('Where you can work from', page)
            self.assertIn('<dt>Work arrangement</dt>', page)
            self.assertNotIn('<dt>Location</dt>', page)
            self.assertEqual(catalog.build_catalog([self.job], {'location':'Brazil'})['result_count'], 0)
            self.assertEqual(self.job, before)
            self.assertEqual(detail.candidate_job_eligibility(self.job), shared)

    def test_known_locations_and_material_restrictions_remain_visible(self):
        arrangement = self.job['enrichment']['attributes']['work_arrangement']
        for countries, expected in [(['Brazil'], 'Brazil'), ([], 'Location restrictions apply')]:
            arrangement.update(workplace_mode='remote', location_scope='remote_restricted',
                               eligible_countries=countries)
            catalog.prepare_catalog_presentation(self.job)
            self.assertIn(expected, self.card())
            self.assertIn(expected, self.page())
        self.job.update(company_slug='mercor', rich_provider='mercor',
            applicant_country_requirements=[dict(dimension='location', mode='exclude', countries=['France'])],
            applicant_geography_evidence=dict(job_id=self.job['job_id'], source_url=self.job['listing_url']))
        self.assertIn('Location restrictions apply', self.card())
        self.assertIn('France', self.page())

    def test_no_optional_elements_or_separators_for_missing_information(self):
        self.job['source_location'] = ''
        catalog.prepare_catalog_presentation(self.job)
        card = self.card()
        self.assertNotIn("class='job-location'", card)
        self.assertNotIn("class='job-pay'", card)
        self.assertNotIn(' · ', card)
        self.assertNotIn('Confirm with employer', card)

    def test_literal_rates_consistent_in_browse_and_detail(self):
        for value in ('$50/hr', '$35-55/hr', 'up to $90/hr', 'From EUR 50 per task',
                      'GBP 800 per project', 'USD 100,000 per year', 'CAD 4,000 per month'):
            with self.subTest(value=value):
                self.assertEqual(self.pay({'pay':value}), value)
                self.assertIn(value, self.card())
                self.assertIn(value, self.page())
                self.assertNotIn('Currency not specified', self.card())
        self.assertIsNone(self.pay({}, 'Competitive compensation.'))
        self.assertNotIn("class='job-pay'", self.card())

    def test_normalized_pay_and_manual_unknown_or_undisclosed_are_authoritative(self):
        comp=self.job['enrichment']['attributes']['compensation']
        comp.update(disclosed=True, amount_min=20, amount_max=30, currency='EUR', period='hour', amount_type='range')
        self.assertEqual(self.pay(), 'EUR 20–EUR 30 per hour')
        self.job['overridden_fields']=['attributes.compensation.disclosed']
        comp['disclosed']=None
        self.assertIsNone(self.pay({'pay':'$50/hr'}))
        comp['disclosed']=False
        self.assertIsNone(self.pay({'pay':'$50/hr'}))
        comp.update(disclosed=True, amount_min=42, amount_max=42)
        self.assertEqual(self.pay({'pay':'$50/hr'}), 'EUR 42 per hour')

    def test_exact_source_binding_and_variants_do_not_share_pay(self):
        sibling=deepcopy(self.job)
        self.pay({'pay':'$20/hr'})
        sibling['rich_metadata_json']='{"pay":"$80/hr"}'
        self.assertEqual(advertised_compensation(self.job),'$20/hr')
        self.assertEqual(advertised_compensation(sibling),'$80/hr')
        for key in ('rich_external_id','rich_source_url','rich_provider'):
            wrong=deepcopy(self.job);wrong[key]='incompatible-sibling'
            self.assertIsNone(advertised_compensation(wrong))
        packet={DETAIL_KEY:dict(provider='other', external_id='other',url='https://other.test',
            record=dict(salaryType='HOURLY',lowerBoundHourlyRate=500,upperBoundHourlyRate=800))}
        self.assertIsNone(self.pay(packet))

    def test_referrals_bonuses_and_unrelated_amounts_are_not_pay(self):
        for body in ('Referral reward: $50 per task', 'Bonus: $50 per hour',
                     'Software subscription costs $50 per month.',
                     '# Referrals\n\nEarn $50 per task.\n\n# Responsibilities\n\nReview text.'):
            self.assertIsNone(self.pay({}, body), body)
        self.assertEqual(self.pay({}, '# Compensation\n\nPay: $30 per hour\n\n# Referrals\n\nEarn $50 per task'), '$30 per hour')
        self.assertIsNone(self.pay({'pay':'Referral bonus $50 per task'}))

    def test_html_plain_sections_and_normalized_referral_false_positives(self):
        self.job['rich_body_format']='text/html'
        self.assertIsNone(self.pay({}, '<h2>Referrals</h2><p>Earn $50 per hour</p><h2>Responsibilities</h2><p>Review text.</p>'))
        self.job['rich_body_format']='text/plain'
        self.assertIsNone(self.pay({}, 'Referrals\n\nEarn $50 per hour\n\nResponsibilities\n\nReview text.'))
        self.job['enrichment']['attributes']['compensation'].update(disclosed=True, amount_min=50, amount_max=50, period='hour', amount_type='exact')
        self.assertIsNone(self.pay({}, 'About this role: review text.\n\nReferral reward: $50 per hour'))

    def test_supported_normalized_pay_conflicts_fail_closed(self):
        comp=self.job['enrichment']['attributes']['compensation']
        comp.update(disclosed=True, amount_min=50, amount_max=50, period='hour', amount_type='exact',currency='USD')
        for value in ('$90/hr','EUR 50/hr','$50 per month'):
            self.assertIsNone(self.pay({'pay':value}),value)
        self.assertEqual(self.pay({'pay':'Up to $50/hr'}),'Up to $50/hr')

    def test_content_capture_does_not_change_last_verified_display(self):
        self.job['last_captured_at']='2026-08-19T12:00:00+00:00'
        self.assertIn('Last verified:</strong> August 16, 2026',self.page())

    def test_normalized_agreement_preserves_all_supported_literal_formats(self):
        comp=self.job['enrichment']['attributes']['compensation']
        for value, amount, currency, period in [('USD 100,000 per year',100000,'USD','year'),
                ('From EUR 50 per task',50,'EUR','task'),('50 per hour USD',50,'USD','hour')]:
            comp.update(disclosed=True,amount_min=amount,amount_max=amount,currency=currency,period=period,amount_type='exact')
            self.assertEqual(self.pay({'pay':value}),value)

    def test_conflicting_source_rates_not_silently_selected(self):
        self.assertIsNone(self.pay({'pay':'$50/hr'}, 'Compensation: EUR 70 per hour'))
        self.assertEqual(self.pay({'pay':'$50-70/hr'}, 'Earn up to $70/hr'), '$50-70/hr')

    def test_equal_bounds_and_currency_evidence_preserved(self):
        packet={DETAIL_KEY:dict(provider=self.job['company_slug'],external_id=self.job['external_id'],
            url=self.job['listing_url'], record=dict(salaryType='HOURLY',lowerBoundHourlyRate=50,upperBoundHourlyRate=50))}
        self.assertEqual(self.pay(packet), '50 per hour')
        packet[DETAIL_KEY]['record']['salaryCurrency']='USD'
        self.assertEqual(self.pay(packet), '50 per hour USD')
        self.assertEqual(pay_facts({'pay':'up to $50/hr'}, '')['label'], 'up to $50/hr')
        self.assertEqual(pay_facts({'pay':'$50/hr'}, '')['currency_note'], 'Currency not specified in the listing.')


def mercor_page(candidate, *, rate=50, other_id=None, currency='USD', description=None):
    """Synthetic official-page contract, not a real captured observation."""
    role=dict(listingId=other_id or candidate.external_id,title=candidate.title,
              description=description or candidate.source_body,status='active',isPrivate=False,deletedAt=None,
              rateMin=rate,rateMax=rate,payRateFrequency='hourly')
    props=dict(role=role,structuredData=dict(jobPosting=dict(title=candidate.title,
        identifier=dict(value=role['listingId']),baseSalary=dict(currency=currency,
        value=dict(minValue=rate,maxValue=rate,unitText='HOUR')))))
    body=('<h2 data-test="listing-rate-range">$'+str(rate)+'</h2>'
          '<div data-test="listing-rate-range-text">per hour</div>'
          '<h2>Earn $250 by referring</h2><script id="__NEXT_DATA__">'
          +json.dumps(dict(props=dict(pageProps=props)))+'</script>').encode()
    return DetailResponse(candidate.url+'/test-role',body,'2026-08-16T13:00:00+00:00')


class MercorPayCaptureTests(unittest.TestCase):
    def setUp(self):
        self.candidate=JobCandidate(external_id='list_fixture',title='Fixture role',location='Remote',
            url='https://work.mercor.com/jobs/list_fixture',source_body='Existing description. '*30,
            source_body_format='text/plain',source_metadata=dict(payRateFrequency='hourly'))

    def test_only_exact_salary_content_is_added_and_actual_observation_kept(self):
        before=deepcopy(self.candidate)
        response=mercor_page(self.candidate)
        result=recover_detail('mercor',self.candidate,response)
        self.assertEqual(self.candidate,before)
        self.assertEqual(result.source_body,before.source_body)
        self.assertEqual(result.location,before.location)
        self.assertEqual(result.source_metadata['pay'],'$50 per hour')
        packet=result.source_metadata[DETAIL_KEY]
        self.assertEqual(packet['response_url'],response.url)
        self.assertEqual(packet['observed_at'],response.observed_at)
        self.assertEqual(packet['record']['salaryCurrency'],'USD')
        self.assertNotIn('250',json.dumps(packet))
        self.assertNotIn('applicantLocationRequirements',json.dumps(packet))
        self.assertEqual(packet['display_text'],before.source_body)
        self.assertEqual(recover_detail('mercor',result,response),result)

    def test_conflicting_identity_description_or_retained_pay_is_rejected(self):
        for response in (mercor_page(self.candidate,other_id='list_other'),
                         mercor_page(self.candidate,description='Different description.')):
            with self.assertRaises(ValueError): recover_detail('mercor',self.candidate,response)
        from dataclasses import replace
        candidate=replace(self.candidate,source_metadata={'pay':'$60 per hour'})
        with self.assertRaises(ValueError): recover_detail('mercor',candidate,mercor_page(candidate))

    def test_currency_is_not_assumed_from_symbol(self):
        result=recover_detail('mercor',self.candidate,mercor_page(self.candidate,currency=None))
        self.assertNotIn('salaryCurrency',result.source_metadata[DETAIL_KEY]['record'])

    def test_content_only_capture_never_renews_availability(self):
        from tests.test_catalog_source_verification import MercorCatalogContractTests
        from tests.test_mercor_observation_semantics import NOW
        f=MercorCatalogContractTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        f.observe(['list_fixture'])
        old=f.load()[0]
        candidate=JobCandidate(external_id=old['external_id'],title=old['source_title'],
            location=old['source_location'],url=old['listing_url'],source_body=old['rich_body'],
            source_body_format=old['rich_body_format'],source_metadata=json.loads(old['rich_metadata_json']))
        response=mercor_page(candidate)
        from dataclasses import replace
        response=replace(response,observed_at=(f.now+timedelta(hours=1)).isoformat())
        with f.conn: reprocess_saved_detail(f.conn,old['job_id'],response)
        after=f.load()[0]
        self.assertEqual(old['availability_trust'],after['availability_trust'])
        self.assertEqual(old['source_run_id'],after['source_run_id'])
        self.assertEqual(old['source_location'],after['source_location'])
        self.assertEqual(f.load(f.now+timedelta(hours=73)), [])
