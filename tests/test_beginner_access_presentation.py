"""Recorded beginner-interest explanation packets, not an admission-policy oracle.

Production-path paired profiles and complete-source policy contrasts live in the
separate matching/client suites. These tests preserve their evidence boundaries.
"""
from copy import deepcopy
from html import unescape
import unittest

from tests.test_candidate_condition_comparisons import confirmed
from tests.test_candidate_source_display import detail
from tests.test_recommendation_presentation import ApplicationRegions, assert_conflict_application
from tests.test_transferable_task_presentation import explanation_fixture
from wahojobs.authenticated_card_evidence import _blocks, prepare_card_evidence, render_card_evidence
from wahojobs.authenticated_source_detail import render_authenticated_job_page
from wahojobs.candidate_condition_comparisons import _lines
from wahojobs.candidate_decision import attach_decision, render_application_guidance, render_fit_support, render_reasons


def beginner_fixture(*, history=False, first_job=False):
    source, match, profile = explanation_fixture()
    related = deepcopy(match['accepted_task_fit'])
    scope = [dict(quote=quote, block_reference=block['reference'], line=number,
                  scope_kind='explicit_entry_level_or_non_specialized')
             for block in _blocks(source['body']) for number, quote in _lines(block)
             if quote.startswith('This is a fully remote, flexible contract role')]
    if not scope:
        raise AssertionError('Existing preserved Generalist accessibility paragraph is required')
    interest = dict(path='preferences.target_opportunity_types[0]', text='AI evaluation',
        provenance=[dict(explicit=True, source_kind='user_confirmation', source_ordinals=[1])])
    if not history:
        profile['experience'] = {'specialties': [], 'total_years': None}
        profile['provenance']['field_sources'] = []
    if first_job:
        profile['constraints'] = {'hard_constraints': ['no prior experience']}
        confirmed(profile, 'constraints.hard_constraints[0]')
    profile['preferences'] = {'target_opportunity_types': [interest['text']]}
    confirmed(profile, interest['path'])
    duty = deepcopy(related['facts'][0])
    accepted = dict(basis='beginner_interest', source_reference=deepcopy(related['source_reference']),
        facts=[duty], profile_facts=[deepcopy(interest)], scope_evidence=scope,
        interest_links=[dict(quote=duty['quote'], block_reference=duty['block_reference'],
            profile_fact=deepcopy(interest), task_family='ai_evaluation', support_kind='beginner_interest')])
    if history:
        accepted['related_activity_fit'] = related
    match['accepted_task_fit'] = accepted
    match['affirmative_fit'] = {'supported_evidence': [dict(source='beginner_interest',
        requirement='Beginner-accessible evaluation tasks', profile_evidence=interest['text'])]}
    return source, match, profile


class BeginnerAccessPresentationTests(unittest.TestCase):
    def test_access_and_interest_lead_for_experienced_and_unknown_history_profiles(self):
        reasons = []
        for history in (True, False):
            source, match, profile = beginner_fixture(history=history)
            before = deepcopy((match, profile))
            packet = prepare_card_evidence(match, source, profile)
            reasons.append(packet['decision_short_reason'])
            self.assertIn('open to beginners', reasons[-1])
            self.assertIn('interest in “AI evaluation”', reasons[-1])
            self.assertNotIn('Your experience', reasons[-1])
            self.assertEqual(packet['decision_has_reported_support'], history)
            self.assertEqual(packet['decision_application_facts'], [])
            self.assertEqual((match, profile), before)
        self.assertEqual(reasons[0], reasons[1])

    def test_explicit_first_job_and_unknown_history_never_become_invented_experience(self):
        guidance = []
        for first_job in (False, True):
            source, match, profile = beginner_fixture(first_job=first_job)
            before = deepcopy(profile)
            packet = prepare_card_evidence(match, source, profile)
            guidance.append(unescape(render_application_guidance(packet, employer_name='Alignerr')))
            self.assertIn('In your application on Alignerr’s website, explain what interests you about the evaluation tasks', guidance[-1])
            self.assertIn('how you would approach them', guidance[-1])
            for invented in ('describe your experience', 'background you reported', 'your projects',
                             'your training', 'cover letter', 'upload your', 'professional AI experience'):
                self.assertNotIn(invented, guidance[-1])
            self.assertFalse(packet['decision_has_reported_support'])
            self.assertEqual(packet['transferable_task_links'], [])
            self.assertEqual(profile, before)
            self.assertIsNone(profile['experience']['total_years'])
        self.assertEqual(guidance[0], guidance[1])

    def test_disclosure_leads_with_access_and_interest_and_keeps_activity_secondary(self):
        source, match, profile = beginner_fixture(history=True)
        packet = prepare_card_evidence(match, source, profile)
        support = unescape(render_fit_support(packet))
        self.assertIn('About this recommendation', support)
        self.assertIn(match['accepted_task_fit']['scope_evidence'][0]['quote'], support)
        self.assertIn('Your stated work interest: AI evaluation', support)
        self.assertIn(match['accepted_task_fit']['facts'][0]['quote'], support)
        self.assertIn('Additional related activity you reported: review written responses', support)
        self.assertLess(support.index('The employer says:'), support.index('Your stated work interest:'))
        self.assertLess(support.index('Your stated work interest:'), support.index('Additional related activity'))
        self.assertNotIn('Your confirmed activity:', support)
        advice = unescape(render_application_guidance(packet, employer_name='Alignerr'))
        self.assertIn('In your application on Alignerr’s website, describe your experience reviewing written responses', advice)

    def test_source_only_waiver_or_unbound_interest_cannot_create_beginner_explanation(self):
        for key in ('facts', 'profile_facts', 'scope_evidence', 'interest_links'):
            source, match, profile = beginner_fixture()
            match['accepted_task_fit'][key] = []
            with self.subTest(missing=key):
                packet = prepare_card_evidence(match, source, profile)
                self.assertIsNone(packet['beginner_access'])
                self.assertFalse(packet['decision_has_reported_support'])
                self.assertEqual(packet['decision_application_facts'], [])
                self.assertNotIn('open to beginners', render_reasons(packet) + render_fit_support(packet))
                self.assertNotIn('explain what interests you', render_application_guidance(packet))

    def test_beginner_source_identity_and_current_scope_quote_are_required(self):
        for key in ('job_id', 'external_id', 'source_url', 'material_content_sha256'):
            source, match, profile = beginner_fixture()
            match['accepted_task_fit']['source_reference'][key] = 'other-source'
            with self.subTest(binding=key):
                self.assertIsNone(prepare_card_evidence(match, source, profile)['beginner_access'])
        for key, value in (('quote', 'Beginners welcome in another role'), ('block_reference', 'source block 999'),
                           ('line', 999), ('scope_kind', 'title_only')):
            source, match, profile = beginner_fixture()
            match['accepted_task_fit']['scope_evidence'][0][key] = value
            with self.subTest(scope=key):
                self.assertIsNone(prepare_card_evidence(match, source, profile)['beginner_access'])

    def test_interest_pair_membership_and_confirmed_preference_provenance_are_required(self):
        for key, value in (('quote', 'Unrecorded duty'), ('block_reference', 'other paragraph'),
                           ('support_kind', 'transferable_activity'), ('task_family', 'remote_work')):
            source, match, profile = beginner_fixture()
            match['accepted_task_fit']['interest_links'][0][key] = value
            with self.subTest(pair=key):
                self.assertIsNone(prepare_card_evidence(match, source, profile)['beginner_access'])
        for key, value in (('path', 'experience.specialties[0]'), ('text', 'Unrecorded interest'),
                           ('provenance', []), ('provenance', [dict(explicit=False, source_kind='user_confirmation')]),
                           ('provenance', [dict(explicit=True, source_kind='model_extraction')])):
            source, match, profile = beginner_fixture()
            # Mutate both records to show provenance/path checks are independent
            # of the exact recorded-pair membership check above.
            for fact in (match['accepted_task_fit']['profile_facts'][0],
                         match['accepted_task_fit']['interest_links'][0]['profile_fact']):
                fact[key] = value
            if key == 'text':
                match['accepted_task_fit']['profile_facts'][0]['text'] = 'AI evaluation'
            with self.subTest(fact=key, value=value):
                self.assertIsNone(prepare_card_evidence(match, source, profile)['beginner_access'])

    def test_stale_secondary_activity_does_not_remove_access_or_invent_advice(self):
        source, match, profile = beginner_fixture(history=True)
        match['accepted_task_fit']['related_activity_fit']['source_reference']['material_content_sha256'] = 'old'
        packet = prepare_card_evidence(match, source, profile)
        self.assertTrue(packet['beginner_access'])
        self.assertEqual(packet['transferable_task_links'], [])
        self.assertFalse(packet['decision_has_reported_support'])
        self.assertIn('explain what interests you about the evaluation tasks', render_application_guidance(packet))
        self.assertNotIn('Additional related activity', render_fit_support(packet))

    def test_reattachment_clears_beginner_access_and_all_old_secondary_support(self):
        source, match, profile = beginner_fixture(history=True)
        packet = prepare_card_evidence(match, source, profile)
        self.assertTrue(packet['beginner_access'])
        attach_decision(packet, {}, profile=profile)
        self.assertIsNone(packet['beginner_access'])
        self.assertEqual(packet['transferable_task_links'], [])
        self.assertEqual(packet['decision_application_facts'], [])
        self.assertEqual(render_reasons(packet), '')
        self.assertEqual(render_fit_support(packet), '')

    def test_material_conflict_keeps_external_conflict_first_advice(self):
        source, match, profile = beginner_fixture()
        packet = prepare_card_evidence(match, source, profile)
        packet['decision_profile_context']['workload'] = dict(state='hard_conflict', outcome='fail',
            guidance='Your part-time-only requirement conflicts with this posting’s workload.')
        before = deepcopy(packet)
        advice = render_application_guidance(packet, employer_name='Alignerr')
        assert_conflict_application(self, advice, employer='Alignerr')
        self.assertNotIn('explain what interests you', advice)
        self.assertEqual(packet, before)

    def test_list_and_exact_detail_share_basis_and_preserve_separate_personalization(self):
        source, match, profile = beginner_fixture()
        packet = prepare_card_evidence(match, source, profile)
        job = detail(source, match)
        job['company_name'] = 'Alignerr'
        body = render_authenticated_job_page(job, profile=profile, navigation='', return_run_id='a' * 24)
        for rendered in (render_card_evidence(packet, 'beginner'), body):
            self.assertIn(packet['decision_short_reason'], unescape(rendered))
            self.assertIn('Eligibility from Brazil needs confirmation.', unescape(rendered))
        parsed = ApplicationRegions(body)
        self.assertIn('In your application on Alignerr’s website, explain what interests you', ''.join(parsed.guidance))
        self.assertTrue(parsed.personalization)
        for link in parsed.links:
            if link['attrs'].get('href', '').startswith('/account/profile'):
                self.assertIn('recommendation-personalization', link['regions'])
                self.assertNotIn('before-apply', link['regions'])
                self.assertIn('Wahojobs', ''.join(link['text']))

    def test_interest_and_access_source_text_are_escaped(self):
        source, match, profile = beginner_fixture()
        packet = prepare_card_evidence(match, source, profile)
        # Render-only escaping contrast. Not a claim this hostile string would
        # become a relevant confirmed interest in the actual matching policy.
        packet['beginner_access']['interest_links'][0]['profile_fact']['text'] = '<img src=x onerror=alert(1)>'
        packet['beginner_access']['scope_evidence'][0]['quote'] = '<script>alert(2)</script>'
        support = render_fit_support(packet)
        self.assertNotIn('<img ', support)
        self.assertNotIn('<script>', support)
        self.assertIn('&lt;img ', support)
        self.assertIn('&lt;script&gt;', support)


if __name__ == '__main__':
    unittest.main()
