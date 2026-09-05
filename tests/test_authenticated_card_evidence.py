from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_card_evidence import prepare_card_evidence, render_card_evidence


SOURCES = {r['job_id']: r for r in json.loads(
    (Path(__file__).parent / 'fixtures/card_source_wording.json').read_text(encoding='utf-8'))}
PROFILE = {'education': {'degrees': ['PhD in Molecular Biology']},
           'experience': {'specialties': ['cell biology', 'molecular biology']}}


def card(source):
    return {**{k: source[k] for k in ('job_id', 'canonical_opportunity_id', 'url', 'source_slug')},
            'matched_core_domains': ['biology'], 'location_eligibility_status': 'unknown'}


class AuthenticatedCardEvidenceTests(unittest.TestCase):
    def prepared(self, identity):
        source = deepcopy(SOURCES[identity])
        return prepare_card_evidence(card(source), source, PROFILE)

    def test_coding_source_required_preferred_alternatives_and_unassessed_tools(self):
        evidence = self.prepared(11242)
        body = render_card_evidence(evidence, 'match-9')
        self.assertNotIn('Your profile lists', body)
        self.assertNotIn('topical fit only', body)
        self.assertIn('<h4>Required</h4>', body)
        self.assertIn('<h4>Preferred</h4>', body)
        self.assertIn('Python, R, or another relevant programming language', body)
        self.assertIn('Git/GitHub and running code in Docker', body)
        self.assertIn('depth in at least two', body)
        self.assertIn('20+ hours per week', body)
        self.assertIn('Not assessed against your profile', body)
        self.assertNotIn('you satisfy', body)
        self.assertNotIn('you lack', body)

    def test_ideal_qualification_and_conditional_degree_are_not_promoted_to_required(self):
        body = render_card_evidence(self.prepared(11271), 'match-7')
        self.assertIn('<h4>Ideal Qualifications</h4>', body)
        self.assertNotIn('<h4>Required</h4>', body)
        self.assertIn('doctoral candidate', body)
        self.assertIn('exceptional depth in a specific subdomain', body)
        self.assertIn('strong plus', body)
        self.assertIn('10+ hours/week', body)
        self.assertIn('Applicant-location eligibility isn’t specified.', body)

    def test_future_network_is_source_supported_and_title_independent(self):
        evidence = self.prepared(1039)
        self.assertEqual(evidence['kind'], 'Talent network — future consideration')
        source = deepcopy(SOURCES[1039]); match = card(source)
        match['display_title'] = 'Unrelated demonstration title'
        self.assertEqual(prepare_card_evidence(match, source, PROFILE)['kind'], evidence['kind'])
        body = render_card_evidence(evidence, 'match-8')
        self.assertIn('not a specific job posting', body)
        self.assertIn('future projects', body)
        self.assertIn('15-30 hours per week', str(evidence['facts']))
        self.assertNotIn('Application acceptance has been verified', body)

    def test_variant_identity_mismatch_never_decorates_another_card(self):
        source = deepcopy(SOURCES[11242])
        for field in ('job_id', 'canonical_opportunity_id', 'url', 'source_slug'):
            match = card(source); match[field] = 'different'
            self.assertIsNone(prepare_card_evidence(match, source, PROFILE))
        source['content_external_id'] = 'another-record'
        self.assertIsNone(prepare_card_evidence(card(source), source, PROFILE))

    def test_missing_source_and_uncompared_evidence_do_not_invent_a_reason(self):
        source = deepcopy(SOURCES[11242]); source['body'] = ''
        self.assertIsNone(prepare_card_evidence(card(source), source, PROFILE))
        source['metadata_json'] = '[]'
        self.assertIsNone(prepare_card_evidence(card(source), source, PROFILE))
        body = render_card_evidence(None, 'match-1')
        self.assertIn('Full requirements', body)
        self.assertNotIn('PhD', body)
        source = deepcopy(SOURCES[11242]); match = card(source); match['matched_core_domains'] = []
        evidence = prepare_card_evidence(match, source, PROFILE)
        self.assertEqual(evidence['reason'], '')
        self.assertNotIn('supports topical fit', render_card_evidence(evidence, 'match-1'))
        from wahojobs.crawler.provider_details import DETAIL_KEY
        source['metadata_json'] = json.dumps({DETAIL_KEY: None})
        self.assertIsNotNone(prepare_card_evidence(card(source), source, PROFILE))

    def test_live_feed_does_not_create_a_vacancy_or_ongoing_claim(self):
        source = deepcopy(SOURCES[11242]); source['body'] = 'Remote biology work.'
        match = dict(card(source), inventory_model='live_feed', opportunity_kind='live_posting')
        self.assertEqual(prepare_card_evidence(match, source, PROFILE)['kind'], 'Opportunity type not established')
        source['body'] = 'We are continuously recruiting biology specialists.'
        self.assertEqual(prepare_card_evidence(match, source, PROFILE)['kind'], 'Ongoing recruiting')
        source['body'] = 'We are not continuously recruiting biology specialists.'
        self.assertEqual(prepare_card_evidence(match, source, PROFILE)['kind'], 'Opportunity type not established')

    def test_conflicting_workload_and_equipment_wording_remain_unassessed(self):
        # Synthetic edge case; does not claim these fields occur in the capture.
        source = deepcopy(SOURCES[11242]); source['commitment'] = 'Full-time'
        source['body'] = '**Equipment**\n\nA Mac is required OR an approved alternative.\n\n**Engagement**\n\nPart-time, 10 hours/week.'
        body = render_card_evidence(prepare_card_evidence(card(source), source, PROFILE), 'match-1')
        self.assertIn('Full-time', body)
        self.assertIn('Part-time, 10 hours/week', body)
        self.assertIn('OR an approved alternative', body)
        self.assertIn('Confirm the schedule', body)

    def test_recovered_micro1_plain_headings_do_not_extend_preferred_label(self):
        from tests.test_provider_detail_recovery import CASES, candidate, response
        from wahojobs.crawler.provider_details import DETAIL_KEY, recover_detail
        case = next(c for c in CASES if c['provider'] == 'micro1')
        recovered = recover_detail('micro1', candidate(case), response(case))
        source = dict(SOURCES[11242], source_slug='micro1', external_id=case['external_id'],
                      content_external_id=case['external_id'], url=case['url'], source_url=case['url'],
                      body=recovered.source_body, metadata_json=json.dumps(recovered.source_metadata))
        evidence = prepare_card_evidence(card(source), source, PROFILE)
        blocks = {b['heading']: b['text'] for b in evidence['blocks']}
        self.assertIn('PhD in biology', blocks['Preferred Qualifications'])
        self.assertNotIn('output-based', blocks['Preferred Qualifications'])
        self.assertIn('output-based', blocks['Compensation Structure'])
        self.assertIn('OR minimum weekly commitment required', blocks['Application screening questions'])
        self.assertIn('Published hourly-rate fields', blocks['Other published fields (read alongside the description)'])
        self.assertIn('OR minimum weekly commitment required', evidence['workload'])
        self.assertEqual(recovered.source_metadata[DETAIL_KEY]['observed_at'], case['observed_at'])

    def test_source_text_is_escaped_and_native_disclosure_is_keyboard_accessible(self):
        source = deepcopy(SOURCES[11242]); source['body'] += '\n\n**Equipment**\n<script>alert(1)</script>'
        body = render_card_evidence(prepare_card_evidence(card(source), source, PROFILE), 'match-1')
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('<details', body)
        self.assertIn('<summary', body)
        self.assertIn("data-source-variant='11242'", body)

    def test_card_source_read_is_visible_only_and_preserves_matching_and_actions(self):
        fixture = SyntheticMatcherFixture(); self.addCleanup(fixture.close)
        first = fixture.get(); self.assertEqual(first.status, 200)
        run = fixture.last_run()
        before = deepcopy(run.recommendation_context['matches'])
        from wahojobs.authenticated_card_evidence import load_card_sources
        calls = []
        def counted(conn, matches):
            calls.append([m['job_id'] for m in matches])
            return load_card_sources(conn, matches)
        with patch('wahojobs.authenticated_card_evidence.load_card_sources', side_effect=counted):
            again = fixture.get('/find-matches?run=' + run.match_run_id)
        self.assertEqual(again.status, 200)
        self.assertEqual(run.recommendation_context['matches'], before)
        self.assertEqual(calls, [[m['job_id'] for m in browser._primary_presentation_matches(run.recommendation_context)]])
        self.assertIn(b'action="/action"', again.body)
        self.assertIn(b'name="opportunity_key"', again.body)
        with patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_with_card_evidence', side_effect=AssertionError('detail must not load cards')):
            self.assertEqual(fixture.get('/job/opportunity-7002?variant=7003').status, 200)


if __name__ == '__main__':
    unittest.main()
