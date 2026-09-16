"""Source fidelity through authenticated consumers, without changing eligibility."""
from copy import deepcopy
from html import unescape
import json
import unittest

from tests.geographic_provenance_support import GeographyFixture, PROVENANCE, JOB, Page
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_card_evidence import prepare_card_evidence, load_card_sources, _source_text
from wahojobs.authenticated_source_detail import append_authenticated_source_detail
from wahojobs.crawler.provider_details import DETAIL_KEY

BODY='## About the Role\n\nLocation: Remote\n\n## Responsibilities\n\nReview and evaluate AI-generated content.\n\n## Who You Are\n\nNo prior AI experience required'


class GeographicProvenanceTests(unittest.TestCase):
    def fixture(self,**kwargs):
        f=GeographyFixture(**kwargs);self.addCleanup(f.close);return f

    def test_exact_public_detail_retains_provenance_outside_employer_wording(self):
        f=self.fixture();response,run,context,m=f.current();detail,packet=f.detail(run)
        self.assertEqual(m['location_eligibility_status'],'unknown')
        self.assertEqual([r['job_id'] for r in browser._conditional_presentation_matches(context)],[JOB])
        self.assertFalse(browser._primary_presentation_matches(context))
        self.assertEqual([r['source']['quote'] for r in m['source_task_fit']['conditions']],
                         ['Clear written communication skills in English'])
        generic = m['non_decisive_source_questions']
        self.assertEqual(len(generic), 4)
        self.assertTrue(all(r['status'] == 'unresolved' and not r['admission_decisive'] for r in generic))
        self.assertEqual({r['source']['quote'] for r in generic}, {
            'Strong attention to detail with a systematic, thorough approach to tasks',
            'Comfortable evaluating a broad variety of topics and content formats',
            'Self-motivated and reliable when working independently',
            'Able to follow structured guidelines and apply them consistently'})
        for page in (response.body,detail.body):
            html=unescape(page.decode())
            self.assertNotIn('Vietnam',html)  # this exact body contains no Vietnam
            self.assertNotIn('This location tag does not establish',html)
            self.assertNotIn('Geographic source provenance',html)
            self.assertNotIn('Employer page location field',html)
            self.assertIn('Eligibility from Brazil needs confirmation.',html)
            self.assertNotIn('Listing location: Vietnam',html)
        self.assertNotIn('Vietnam',packet['text'])
        self.assertIn("<details class='employer-description'>", detail.body.decode())
        self.assertIn('No prior AI, tech, or content moderation experience required', detail.body.decode())
        ref=packet['location_context']['published_field']
        self.assertEqual(ref['external_id'],PROVENANCE['original']['url'].rsplit('/',1)[1])
        self.assertEqual(ref['source_field'],'props.pageProps.job.location')
        self.assertEqual(ref['observed_at'],PROVENANCE['original']['observed_at'])
        self.assertEqual(packet['comparisons'],m['source_qualification_comparisons'])

    def test_remote_and_country_tag_do_not_grant_or_deny_brazil(self):
        for posting in ('Vietnam','Canada','Remote'):
            with self.subTest(posting=posting):
                f=self.fixture(body=BODY,posting=posting);_,run,ctx,m=f.current();detail,packet=f.detail(run)
                self.assertEqual(m['location_eligibility_status'],'unknown')
                self.assertEqual(m['applicant_location_requirements'],'')
                self.assertEqual(m['job_remote_status'],'remote')
                self.assertIn(b'Eligibility from Brazil needs confirmation.',detail.body)
                self.assertNotIn('Vietnam',str(packet['comparisons']))
                self.assertNotIn('Employer page location field',detail.body.decode())
                if posting != 'Remote':
                    self.assertEqual(packet['location_context']['published_field']['value'],posting)
                    self.assertNotIn(posting,detail.body.decode())

    def test_explicit_restriction_and_conflict_keep_existing_gates(self):
        for extra,status in [('\n\nApplicants must be based in Canada.','incompatible'),
            ("\n\n## About the Role\n\nWe're looking for reviewers based in Brazil to evaluate AI outputs.\n\n## Requirements\n\nApplicants must be based in Canada.",'unknown')]:
            with self.subTest(status=status):
                f=self.fixture(body=BODY+extra,posting='Vietnam');_,run,ctx,m=f.current();detail,packet=f.detail(run)
                self.assertEqual(m['location_eligibility_status'],status)
                self.assertFalse(browser._primary_presentation_matches(ctx))
                self.assertFalse(browser._conditional_presentation_matches(ctx))
                self.assertIn(b'Applicants must be based in Canada.',detail.body)
                self.assertIn('Canada',str(packet['comparisons']))
                self.assertNotIn(b'Vietnam',detail.body)

    def test_explicit_vietnam_residence_survives_unrelated_page_tag(self):
        body=BODY+'\n\nApplicants must reside in Vietnam.'
        f=self.fixture(body=body,posting='Canada');_,run,ctx,m=f.current();detail,packet=f.detail(run)
        self.assertEqual(m['location_eligibility_status'],'incompatible')
        self.assertFalse(browser._primary_presentation_matches(ctx))
        self.assertFalse(browser._conditional_presentation_matches(ctx))
        self.assertIn(b'Applicants must reside in Vietnam.',detail.body)
        self.assertIn('Vietnam',str(packet['comparisons']))
        self.assertNotIn(b'Canada',detail.body)
        self.assertEqual(packet['location_context']['published_field']['value'],'Canada')

    def test_generic_tag_cannot_distort_independent_applicant_invitation(self):
        body=BODY+"\n\n## About the Role\n\nWe're looking for reviewers based in Brazil to evaluate AI outputs."
        f=self.fixture(body=body,posting='Canada');response,run,ctx,m=f.current();detail,packet=f.detail(run)
        self.assertEqual(m['location_eligibility_status'],'eligible')
        for content in (response.body,detail.body):
            self.assertNotIn(b'Canada',content)
            self.assertNotIn(b'Employer page location field',content)
            self.assertNotIn(b'Eligibility from Brazil needs confirmation.',content)
        self.assertIn(b'applicants based in Brazil',detail.body)
        self.assertIn('applicants based in Brazil',packet['location_context']['applicant'])
        self.assertEqual(packet['location_context']['published_field']['value'],'Canada')

    def test_older_internal_packet_cannot_reintroduce_generic_tag(self):
        from wahojobs.authenticated_card_evidence import render_card_evidence
        f=self.fixture();_,run,_,_=f.current();_,packet=f.detail(run)
        prior=deepcopy(packet)
        prior['location_context']['published_field'].pop('generic_country_tag')
        rendered=render_card_evidence(prior,'older-packet')
        self.assertNotIn('Vietnam',rendered)
        self.assertNotIn('Geographic source provenance',rendered)
        self.assertEqual(prior['location_context']['published_field']['value'],'Vietnam')

    def test_bound_free_text_location_wording_is_preserved_without_new_gate(self):
        f=self.fixture(body=BODY,posting='US citizens only');response,run,_,m=f.current();detail,packet=f.detail(run)
        for content in (response.body,detail.body):
            from tests.test_recommendation_presentation import DisclosureText
            visible = ''.join(DisclosureText(content.decode()).main)
            self.assertIn('Check the source’s location information: “US citizens only”.',visible)
            self.assertNotIn(b'Employer page location field',content)
        self.assertIn(b'Location information:</strong> US citizens only',detail.body)
        self.assertFalse(packet['location_context']['published_field']['generic_country_tag'])
        self.assertEqual(m['location_eligibility_status'],'unknown')

    def test_stale_display_text_never_supplies_country_or_qualifications(self):
        from wahojobs.source_clause_materiality import clause_catalog
        f=self.fixture();_,run,ctx,m=f.current()
        with f.connections.read_only_connection_provider() as c:
            source=load_card_sources(c,[m])[JOB]
        before=deepcopy(source)
        meta=json.loads(source['metadata_json'])
        meta[DETAIL_KEY]['display_text']='## Requirements\n\nMust be based in Canada.\n\nListing location: Canada'
        source['metadata_json']=json.dumps(meta)
        self.assertEqual(_source_text(source),before['body'])
        self.assertNotIn('Canada',_source_text(source))
        def catalog(s):
            return clause_catalog(dict(rich_content=[dict(s,metadata=json.loads(s['metadata_json']),provider='alignerr',
                source_url=s['url'],variant_ref='synthetic-exact',authority={'accepted_capture_ref':'synthetic-capture'})]))
        self.assertEqual(catalog(before),catalog(source))
        self.assertEqual(len(catalog(source)),9)

    def test_plain_city_metadata_remains_in_disclosure_without_primary_warning(self):
        from tests.test_recommendation_presentation import DisclosureText
        for city in ('Vancouver', 'Boston'):
            with self.subTest(city=city):
                f=self.fixture(body=BODY,posting=city)
                response,run,_,m=f.current();detail,packet=f.detail(run)
                for content in (response.body,detail.body):
                    self.assertNotIn(city, ''.join(DisclosureText(content.decode()).main))
                self.assertIn('Location information:</strong> '+city, detail.body.decode())
                self.assertEqual(packet['location_context']['published_field']['value'],city)
                self.assertFalse(packet['location_context']['published_field']['generic_country_tag'])
                self.assertEqual(m['location_eligibility_status'],'unknown')

    def test_foreign_or_changed_body_metadata_cannot_supply_location(self):
        from tests.test_accepted_title_uncertainty import profile
        f=self.fixture();_,_,_,m=f.current()
        with f.connections.read_only_connection_provider() as c:source=load_card_sources(c,[m])[JOB]
        original=deepcopy(source)
        for field,value in [('external_id','other'),('url','https://www.alignerr.com/jobs/other'),('provider','other'),
                            ('record_id','other'),('record_body','Other accepted wording'),('version',2),
                            ('field',[]),('field',{}),('observed_at',[]),('observed_at','not a timestamp')]:
            s=deepcopy(original);meta=json.loads(s['metadata_json']);d=meta[DETAIL_KEY]
            if field=='record_id':d['record']['id']=value
            elif field=='record_body':d['record']['longDescription']=value
            else:d[field]=value
            s['metadata_json']=json.dumps(meta)
            packet=prepare_card_evidence(m,s,profile())
            self.assertIsNone(packet['location_context']['published_field'],field)
            self.assertFalse(packet['location_context']['other'],field)
        self.assertEqual(source,original)

    def test_same_canonical_siblings_and_my_jobs_keep_exact_country(self):
        f=self.fixture(sibling=True);_,run,_,_=f.current();detail,packet=f.detail(run)
        saved=f.save_from_detail(detail);self.assertEqual(saved.status,200,saved.body)
        tracker=f.get('/tracker');self.assertEqual(tracker.status,200)
        link=next(x for x in Page(tracker.body).links if x.startswith('/tracker/item?'))
        item=f.get(link);self.assertEqual(item.status,200,item.body)
        self.assertNotIn('Vietnam',unescape(item.body.decode()))
        self.assertEqual(packet['location_context']['published_field']['value'],'Vietnam')
        self.assertEqual(f.get(link,owner=1).status,404)
        sibling,other=f.detail(run,JOB+1)
        self.assertEqual(other['location_context']['published_field']['value'],'Canada')
        self.assertNotIn(b'Vietnam',sibling.body)
        self.assertNotIn(b'Canada',sibling.body)
        self.assertNotIn(b'Employer page location field',tracker.body)

    def test_legacy_appender_and_original_similar_prose_remain_distinct(self):
        f=self.fixture(body=BODY+'\n\nListing location: Vietnam is the employer\'s wording.',posting='Canada')
        _,run,_,m=f.current();detail,packet=f.detail(run)
        self.assertIn("Listing location: Vietnam is the employer's wording.",unescape(detail.body.decode()))
        with f.connections.read_only_connection_provider() as c:s=load_card_sources(c,[m])[JOB]
        job=dict(company_slug='alignerr',external_id=s['external_id'],official_url=s['url'],rich_body=s['body'],
                 rich_body_format=s['body_format'],rich_metadata_json=s['metadata_json'])
        rendered=append_authenticated_source_detail("<div class='job-description'>",job,authenticated=True)
        self.assertNotIn('Canada',unescape(rendered))
        self.assertIn("Listing location: Vietnam is the employer's wording.",unescape(rendered))
        self.assertNotIn('Listing location: Canada',rendered)
        metadata=json.loads(job['rich_metadata_json']);metadata[DETAIL_KEY].pop('display_text')
        job['rich_metadata_json']=json.dumps(metadata)
        self.assertEqual(rendered,append_authenticated_source_detail("<div class='job-description'>",job,authenticated=True))
        job['rich_body']='Changed body';self.assertEqual(append_authenticated_source_detail('unchanged',job,authenticated=True),'unchanged')

    def test_non_geographic_page_fields_survive_without_becoming_qualifications(self):
        f=self.fixture(body=BODY,posting='Canada');response,run,_,_=f.current();detail,packet=f.detail(run)
        self.assertNotIn('Other employer page fields',response.body.decode())
        html=detail.body.decode()
        self.assertIn("<details class='employer-description'>",html)
        self.assertIn('Other employer page fields',html)
        self.assertIn('<dt>Engagement type</dt><dd>CONTRACT</dd>',html)
        self.assertIn('<dt>Hourly range minimum</dt><dd>40</dd>',html)
        self.assertIn('<dt>Hourly range maximum</dt><dd>120</dd>',html)
        self.assertNotIn('Engagement type',packet['text'])
        self.assertEqual([c['kind'] for c in packet['comparisons']],['waiver'])

    def test_html_description_fallback_has_the_same_provenance_and_uncertainty(self):
        body='<h2>Responsibilities</h2><p>Review and evaluate AI-generated content.</p><h2>Who You Are</h2><p>No prior AI experience required</p><p>Location: Remote</p>'
        f=self.fixture(body=body,posting='Vietnam',html=True)
        response,run,_,m=f.current();detail,packet=f.detail(run)
        self.assertEqual(m['location_eligibility_status'],'unknown')
        self.assertNotIn('Vietnam',unescape(detail.body.decode()))
        self.assertNotIn('Listing location: Vietnam',detail.body.decode())
        self.assertNotIn('Vietnam',packet['text'])
        self.assertIn('Review and evaluate AI-generated content.',packet['text'])

if __name__=='__main__':unittest.main()
