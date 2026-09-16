"""Synthetic presentation contrasts; no personal records or live requests."""
from copy import deepcopy
from html import unescape
import json
import unittest

from tests import test_accepted_task_matching as task_tests
from tests.test_confirmed_activity_matching import candidate, v2
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs.authenticated_card_evidence import prepare_card_evidence, render_card_evidence, load_card_sources
from wahojobs.authenticated_source_detail import prepare_detail_display, render_authenticated_job_page
from wahojobs.authenticated_variant_details import load_scoped_snapshot, resolve_scoped_variant, prepare_variant_notice
from wahojobs.crawler.provider_details import DETAIL_KEY
from wahojobs.matching.source_geography import prepare_applicant_location_support


INVITATION = "We're looking for reviewers based in São Paulo and across Brazil to evaluate AI outputs."
FIELD = 'props.pageProps.job.longDescription'


class GeographyEvidencePresentationTests(unittest.TestCase):
    role = task_tests.AcceptedTaskMatchingTests.role
    source = task_tests.AcceptedTaskMatchingTests.source

    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.f.profile = v2(candidate(['Model output evaluation']))
        self.role('Portuguese AI Data Reviewer', 'Remote')

    def setup_case(self, country='Brazil', invitation=True, posting='United States', extra=''):
        from wahojobs.profiles.canonical import field_sources_for_profile
        profile = candidate(['Model output evaluation'])
        profile['location']['country'] = country
        profile['location']['residence'] = country
        profile['provenance']['field_sources'] = field_sources_for_profile(profile, 'user_confirmation', explicit=True)
        self.f.profile = v2(profile)
        body = ('## About the Role\n\n' + (INVITATION if invitation else 'Remote review work.')
                + '\n\n' + extra + '\n\n## What You\'ll Do\n\nEvaluate AI outputs.'
                + '\n\n## Preferred Qualifications\n\nExperience reviewing audio is preferred.')
        with self.f.provider() as c:
            external, url = c.execute('SELECT external_id,url FROM jobs WHERE id=7003').fetchone()
        detail = dict(provider='configured-production', external_id=external, url=url,
                      record={'location': posting}, display_text=body,
                      applicant_location_support=prepare_applicant_location_support(body, FIELD))
        self.source(body, body_format='text/markdown', metadata={DETAIL_KEY: detail})
        return self.prepared()

    def prepared(self):
        with self.f.provider() as c:
            snapshot = load_scoped_snapshot(c, 7002, 7003, now=self.f.now)
            source = load_card_sources(c, [{'job_id': 7003}])[7003]
        job, local = resolve_scoped_variant(snapshot, self.f.profile, None, 7003, now=self.f.now)
        prepare_variant_notice(job, None, local=local, membership_known=False)
        packet = prepare_detail_display(job, self.f.profile)
        card = render_card_evidence(packet, 'example')
        page = render_authenticated_job_page(job, profile=self.f.profile, navigation='')
        return dict(snapshot=snapshot, source=source, job=job, local=local, packet=packet, card=card, page=page)

    def test_brazil_invitation_shared_rendering_and_original_reference(self):
        result = self.setup_case()
        self.assertEqual(result['local']['match']['location_eligibility_status'], 'eligible')
        for html in [result['card'], result['page']]:
            html = unescape(html)
            self.assertNotIn('Source location field:', html)
            self.assertNotIn('Other location information', html)
            self.assertNotIn('Listing location: United States.', html)
        self.assertEqual(unescape(result['page']).count('The description explicitly mentions applicants based in Brazil.'), 1)
        ref = result['packet']['location_context']['references'][0]
        self.assertEqual(ref['source_quote'], INVITATION)
        self.assertEqual(ref['source_field'], FIELD + ':line 3')
        self.assertEqual(ref['job_id'], 7003)
        self.assertEqual(ref['source_url'], result['source']['url'])
        self.assertEqual(result['packet']['location_context']['other'], 'Source location field: “United States”')
        self.assertTrue(result['packet']['location_context']['omit_opaque_other'])

    def test_invitation_does_not_grant_other_or_unknown_country_permission(self):
        for country in ['Portugal', '']:
            r = self.setup_case(country=country)
            self.assertEqual(r['local']['match']['location_eligibility_status'], 'unknown')
            self.assertIn('applicants based in Brazil', r['packet']['location_context']['applicant'])
            self.assertTrue(any('confirmation' in x or 'isn’t specified' in x for x in r['packet']['caveats']))

    def test_metadata_alone_and_eligible_boolean_do_not_invent_applicant_support(self):
        r = self.setup_case(invitation=False)
        self.assertEqual(r['local']['match']['location_eligibility_status'], 'unknown')
        self.assertFalse(r['packet']['location_context']['applicant'])
        match = dict(r['local']['match'], location_eligibility_status='eligible')
        packet = prepare_card_evidence(match, r['source'], self.f.profile)
        self.assertFalse(packet['location_context']['applicant'])
        self.assertEqual(packet['location_context']['other'], 'Source location field: “United States”')
        self.assertTrue(packet['location_context']['omit_opaque_other'])
        self.assertNotIn('Other location information', r['page'])
        self.assertIn('Eligibility from Brazil needs confirmation', r['page'])

    def test_mandatory_restriction_is_not_softened(self):
        r = self.setup_case(invitation=False, extra='## Requirements\n\nApplicants must be based in United States.')
        self.assertEqual(r['local']['match']['location_eligibility_status'], 'incompatible')
        self.assertFalse(r['packet']['location_context']['applicant'])
        self.assertIn('conflicts with your profile', str(r['packet']['caveats']))
        self.assertIn('Source location field:', r['page'])

    def test_conflicting_invitation_does_not_replace_existing_uncertainty(self):
        r = self.setup_case(extra='Applicants must be based in United States.')
        self.assertEqual(r['local']['match']['location_eligibility_status'], 'unknown')
        self.assertIn('Conflicting source', r['local']['match']['location_eligibility_reason'])
        self.assertFalse(r['packet']['location_context']['applicant'])
        self.assertIn('Source location field:', r['page'])

    def test_agreeing_field_has_no_redundant_caution(self):
        r = self.setup_case(posting='Brazil')
        self.assertTrue(r['packet']['location_context']['applicant'])
        self.assertFalse(r['packet']['location_context']['other'])
        self.assertNotIn('Other location information', r['page'])
        self.assertFalse(any('Listing location:' in x for x in r['packet']['caveats']))

    def test_wrong_variant_or_changed_body_cannot_supply_context(self):
        r = self.setup_case()
        for field, value in [('external_id', 'other'), ('url', 'https://example.test/other'), ('provider', 'other')]:
            source = deepcopy(r['source']); metadata = json.loads(source['metadata_json'])
            metadata[DETAIL_KEY][field] = value; source['metadata_json'] = json.dumps(metadata)
            packet = prepare_card_evidence(r['local']['match'], source, self.f.profile)
            self.assertFalse(packet['location_context']['applicant'])
            self.assertFalse(packet['location_context']['other'])
        source = dict(r['source'], body='A different accepted description.')
        self.assertFalse(prepare_card_evidence(r['local']['match'], source, self.f.profile)['location_context']['applicant'])

    def test_presentation_does_not_mutate_decisions_or_source(self):
        r = self.setup_case(); match = deepcopy(r['local']['match']); source = deepcopy(r['source'])
        for _ in range(2):
            prepare_card_evidence(r['local']['match'], r['source'], self.f.profile)
        self.assertEqual(match, r['local']['match'])
        self.assertEqual(source, r['source'])

    def assert_restrictive_field_attributed(self, invitation):
        for value in ['Applicants must reside in United States', 'Applicants must be based in Canada',
                      'Applicants must reside in <Country> & hold "permission"']:
            r = self.setup_case(invitation=invitation, posting=value)
            self.assertEqual(r['packet']['location_context']['other'], f'Source location field: “{value}”')
            for html in [r['card'], r['page']]:
                self.assertIn(f'Check the source’s location information: “{value}”.', unescape(html))
                self.assertNotIn('without explaining', html)
                self.assertNotIn('<Country>', html)
            self.assertIn(f'Source location field: “{value}”', unescape(r['page']))
            if invitation:
                self.assertEqual(r['packet']['location_context']['references'][0]['source_quote'], INVITATION)

    def test_restrictive_field_alongside_invitation_is_not_mislabelled(self):
        self.assert_restrictive_field_attributed(True)

    def test_restrictive_field_in_metadata_fallback_is_not_mislabelled(self):
        self.assert_restrictive_field_attributed(False)

    def test_actual_conditional_card_and_details_share_location_context(self):
        import re
        from wahojobs import authenticated_profile_matches as browser
        self.role('AI Generalist', 'Remote')
        self.setup_case(extra="## Requirements\n\nBachelor's in Biology.")
        response = self.f.get()
        self.assertEqual(response.status, 200)
        context = self.f.last_run().recommendation_context
        conditional = browser._conditional_presentation_matches(context)
        self.assertEqual([m['job_id'] for m in conditional], [7003])
        self.assertEqual(browser._primary_presentation_matches(context), [])
        self.assertEqual(conditional[0]['source_task_fit']['conditions'][0]['status'], 'not_established')
        article = re.search(r"<article\b(?=[^>]*\bclass='match-card')"
                            r"(?=[^>]*\bid='opportunity-7003')(?=[^>]*\bdata-action-card\b)"
                            r"[^>]*>.*?</article>", response.body.decode(), re.S)
        self.assertIsNotNone(article)
        html = article.group()
        detail = self.f.get('/job/opportunity-7002?variant=7003')
        self.assertEqual(detail.status, 200)
        for rendered in [html, detail.body.decode()]:
            rendered = unescape(rendered)
            self.assertNotIn('Source location field:', rendered)
            self.assertNotIn('Other location information', rendered)
            self.assertNotIn('Listing location: United States.', rendered)
            self.assertNotIn('Eligibility from Brazil needs confirmation', rendered)
            self.assertIn('Biology', rendered)
        self.assertEqual(unescape(detail.body.decode()).count('The description explicitly mentions applicants based in Brazil.'), 1)
        self.assertIn('Check the requirement:', html)
        self.assertEqual(context['_card_evidence'][7003]['location_context']['references'][0]['source_quote'], INVITATION)

    def test_opaque_country_aliases_omit_only_display_and_free_text_stays(self):
        for value in ['Canada', 'USA', 'Brasil', 'US citizens only', 'Europe with travel', 'Remote - location to confirm',
                      'Vancouver', 'Boston']:
            r = self.setup_case(invitation=False, posting=value)
            is_bare_country = value in ['Canada', 'USA', 'Brasil']
            self.assertEqual(r['packet']['location_context']['omit_opaque_other'], is_bare_country)
            self.assertEqual(json.loads(r['source']['metadata_json'])[DETAIL_KEY]['record']['location'], value)
            self.assertEqual('Source location field:' in r['page'], not is_bare_country)
            self.assertEqual('Check the source’s location information:' in r['card'], value == 'US citizens only')
            self.assertEqual(r['local']['match']['location_eligibility_status'], 'unknown')

    def test_legacy_description_context_does_not_repeat_opaque_posting_field(self):
        r = self.setup_case(country='Portugal')
        source = deepcopy(r['source']); metadata = json.loads(source['metadata_json'])
        detail = metadata[DETAIL_KEY]
        detail.pop('applicant_location_support')
        detail['record'].update(countryCode='BR', city='São Paulo')
        source['metadata_json'] = json.dumps(metadata)
        packet = prepare_card_evidence(r['local']['match'], source, self.f.profile)
        html = unescape(render_card_evidence(packet, 'legacy'))
        self.assertIn('The description mentions São Paulo, Brazil.', html)
        self.assertIn('Eligibility from Portugal needs confirmation', html)
        self.assertNotIn('United States', html)

    def test_actual_main_card_omits_metadata_without_mutating_source(self):
        from wahojobs import authenticated_profile_matches as browser
        self.setup_case()
        response = self.f.get()
        self.assertEqual(response.status, 200)
        context = self.f.last_run().recommendation_context
        self.assertEqual([m['job_id'] for m in browser._primary_presentation_matches(context)], [7003])
        html = unescape(response.body.decode())
        self.assertEqual(context['_card_evidence'][7003]['location_context']['references'][0]['source_quote'], INVITATION)
        self.assertNotIn('Eligibility from Brazil needs confirmation', html)
        self.assertNotIn('Source location field:', html)
        self.assertNotIn('Other location information', html)

    def test_legacy_free_text_warning_rejects_stale_or_foreign_prepared_binding(self):
        value='US citizens only'
        result=self.setup_case(invitation=True,posting=value)
        self.assertIn(value,result['card'])
        self.assertEqual(result['packet']['location_context']['other_field']['value'],value)
        for field in ('provider','external_id','url','body_sha256'):
            with self.subTest(field=field):
                source=deepcopy(result['source'])
                metadata=json.loads(source['metadata_json'])
                target=(metadata[DETAIL_KEY]['applicant_location_support'] if field=='body_sha256'
                        else metadata[DETAIL_KEY])
                target[field]='foreign-or-stale'
                source['metadata_json']=json.dumps(metadata)
                packet=prepare_card_evidence(result['local']['match'],source,self.f.profile)
                self.assertNotIn('other_field',packet['location_context'])
                self.assertNotIn(value,render_card_evidence(packet,'stale'))
        source=dict(result['source'],body='Different accepted body.')
        packet=prepare_card_evidence(result['local']['match'],source,self.f.profile)
        self.assertNotIn('other_field',packet['location_context'])
        self.assertNotIn(value,render_card_evidence(packet,'changed'))

    def test_bound_legacy_restriction_is_visible_through_actual_list_and_detail(self):
        from tests.test_recommendation_presentation import DisclosureText
        for invitation in (True,False):
            with self.subTest(invitation=invitation):
                self.setup_case(invitation=invitation,posting='US citizens only')
                listing=self.f.get()
                packet=self.f.last_run().recommendation_context['_card_evidence'][7003]
                detail=self.f.get('/job/opportunity-7002?variant=7003')
                for response in (listing,detail):
                    self.assertEqual(response.status,200)
                    main=''.join(DisclosureText(response.body.decode()).main)
                    self.assertIn('Check the source’s location information: “US citizens only”.',main)
                    self.assertNotIn('location requirement conflicts with your profile',main)
                # The field does not grant eligibility or override the independent invitation.
                self.assertEqual(packet['decision_location_status'],'eligible' if invitation else 'unknown')

    def test_unprepared_legacy_field_requires_unchanged_exact_accepted_body(self):
        result=self.setup_case(invitation=False,posting='US citizens only')
        self.assertIsNone(json.loads(result['source']['metadata_json'])[DETAIL_KEY]['applicant_location_support'])
        self.assertIn('US citizens only',result['card'])
        for field in ('provider','external_id','url','display_text','body'):
            with self.subTest(field=field):
                source=deepcopy(result['source'])
                if field=='body':
                    source[field]='Different accepted body.'
                else:
                    metadata=json.loads(source['metadata_json'])
                    metadata[DETAIL_KEY][field]='foreign-or-stale'
                    source['metadata_json']=json.dumps(metadata)
                packet=prepare_card_evidence(result['local']['match'],source,self.f.profile)
                self.assertNotIn('other_field',packet['location_context'])
                self.assertNotIn('Check the source’s location information:',render_card_evidence(packet,'stale'))


if __name__ == '__main__':
    unittest.main()
