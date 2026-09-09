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
            self.assertEqual(html.count('The description explicitly mentions applicants based in Brazil.'), 1)
            self.assertEqual(html.count('Source location field: “United States”'), 1)
            self.assertIn("class='candidate-note'><strong>Other location information", html)
            self.assertNotIn('Listing location: United States.', html)
        ref = result['packet']['location_context']['references'][0]
        self.assertEqual(ref['source_quote'], INVITATION)
        self.assertEqual(ref['source_field'], FIELD + ':line 3')
        self.assertEqual(ref['job_id'], 7003)
        self.assertEqual(ref['source_url'], result['source']['url'])

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

    def test_mandatory_restriction_is_not_softened(self):
        r = self.setup_case(invitation=False, extra='## Requirements\n\nApplicants must be based in United States.')
        self.assertEqual(r['local']['match']['location_eligibility_status'], 'incompatible')
        self.assertFalse(r['packet']['location_context']['applicant'])
        self.assertIn('conflicts with your profile', str(r['packet']['caveats']))

    def test_conflicting_invitation_does_not_replace_existing_uncertainty(self):
        r = self.setup_case(extra='Applicants must be based in United States.')
        self.assertEqual(r['local']['match']['location_eligibility_status'], 'unknown')
        self.assertIn('Conflicting source', r['local']['match']['location_eligibility_reason'])
        self.assertFalse(r['packet']['location_context']['applicant'])

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
                self.assertIn(f'Source location field: “{value}”', unescape(html))
                self.assertNotIn('without explaining', html)
                self.assertNotIn('<Country>', html)
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
        html = re.search(r"<article class='relaxation-preview-card' id='opportunity-7003'>.*?</article>",
                         response.body.decode(), re.S).group()
        detail = self.f.get('/job/opportunity-7002?variant=7003')
        self.assertEqual(detail.status, 200)
        for rendered in [html, detail.body.decode()]:
            rendered = unescape(rendered)
            self.assertEqual(rendered.count('The description explicitly mentions applicants based in Brazil.'), 1)
            self.assertEqual(rendered.count('Source location field: “United States”'), 1)
            self.assertNotIn('Listing location: United States.', rendered)
            self.assertNotIn('Eligibility from Brazil needs confirmation', rendered)
            self.assertIn('Biology', rendered)
        self.assertIn(conditional[0]['source_task_fit']['candidate_note'], unescape(html))


if __name__ == '__main__':
    unittest.main()
