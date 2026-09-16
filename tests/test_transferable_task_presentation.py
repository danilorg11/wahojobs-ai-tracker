"""Presentation of recorded derived relevance, not an admission-policy oracle.

The accepted Generalist source is unchanged. Mechanism and authenticated-client
tests separately establish which task links the product may actually produce.
"""
from copy import deepcopy
from html import unescape
import json
import unittest

from tests.private_beta_matching_support import sources
from tests.test_authenticated_card_evidence import card
from tests.test_candidate_condition_comparisons import confirmed
from tests.test_candidate_source_display import detail
from wahojobs.authenticated_card_evidence import prepare_card_evidence, render_card_evidence
from wahojobs.authenticated_source_detail import prepare_detail_display, render_authenticated_job_page
from wahojobs.candidate_decision import attach_decision, render_reasons, render_assessment, render_limits, render_fit_support


def explanation_fixture():
    saved = next(row for row in sources() if row['title'] == 'Generalist')
    source = dict(job_id=saved['job_id'], canonical_opportunity_id=saved['canonical_id'],
        external_id=saved['external_id'], content_external_id=saved['external_id'],
        url=saved['url'], source_url=saved['url'], source_slug=saved['provider'],
        body=saved['body'], body_format=saved['body_format'], metadata_json=json.dumps(saved['metadata']),
        material_content_sha256=saved['body_sha256'], last_captured_at=saved['observed_at'],
        location=saved['location'], commitment=saved['commitment'])
    activity = {'path': 'experience.specialties[0]', 'text': 'review written responses'}
    quote = next(line[2:] for line in source['body'].splitlines()
        if line.startswith('* Review and evaluate'))
    fact = {'quote': quote, 'block_reference': 'accepted text paragraph 6', 'professional_domains': []}
    reference = {key: source[key] for key in ('job_id', 'canonical_opportunity_id', 'external_id',
        'source_url', 'source_slug', 'material_content_sha256')}
    accepted = dict(basis='transferable_activity', source_reference=reference,
        facts=[fact], profile_facts=[activity], scope_evidence=[{'source_reference': reference}],
        task_links=[dict(quote=quote, block_reference=fact['block_reference'],
            profile_fact=activity, task_family='review_written_material', support_kind='transferable_activity')])
    match = dict(card(source), accepted_task_fit=accepted, affirmative_fit={'supported_evidence': [dict(
        requirement='Transferable activity for entry-level tasks', profile_evidence=activity['text'],
        source='transferable_activity')]})
    profile = {'experience': {'specialties': [activity['text']]},
        'location': {'country': 'Brazil'}, 'provenance': {'field_sources': []}}
    confirmed(profile, activity['path'])
    return source, match, profile


class TransferableTaskPresentationTests(unittest.TestCase):
    def test_card_and_exact_detail_share_recorded_transferable_explanation(self):
        source, match, profile = explanation_fixture()
        original = deepcopy(match)
        packet = prepare_card_evidence(match, source, profile)
        job = detail(source, match)
        exact = prepare_detail_display(job, profile)
        self.assertEqual(packet['decision_reasons'], exact['decision_reasons'])
        self.assertEqual(packet['transferable_task_links'], exact['transferable_task_links'])
        for rendered in (render_card_evidence(packet, 'transferable-test'),
                         render_authenticated_job_page(job, profile=profile, navigation='')):
            html = unescape(rendered)
            self.assertIn('Your experience reviewing written responses is relevant to this work.', html)
            self.assertNotIn('Comparison not established', html)
            self.assertNotIn('describes AI evaluation or annotation work', html)
        # The owner-approved compact card keeps the full paired proof on detail.
        support = unescape(render_fit_support(packet))
        self.assertIn('Employer task:', support)
        self.assertIn(match['accepted_task_fit']['facts'][0]['quote'], support)
        self.assertIn('does not establish prior professional AI work', support)
        self.assertIn('self-reported', support)
        self.assertEqual(match, original, 'Presentation does not rewrite evidence or admission')

    def test_source_mismatch_cannot_fall_back_to_unbound_transferable_evidence(self):
        source, match, profile = explanation_fixture()
        for field in ('job_id', 'external_id', 'source_url', 'material_content_sha256'):
            with self.subTest(field=field):
                changed = deepcopy(match)
                changed['accepted_task_fit']['source_reference'][field] = 'different source'
                packet = prepare_card_evidence(changed, source, profile)
                self.assertEqual(packet['decision_reasons'], [])
                self.assertEqual(packet['transferable_task_links'], [])
                self.assertEqual(render_reasons(packet), '')

    def test_missing_scope_duty_activity_or_pair_cannot_claim_task_support(self):
        source, match, profile = explanation_fixture()
        for key in ('scope_evidence', 'facts', 'profile_facts', 'task_links'):
            with self.subTest(key=key):
                changed = deepcopy(match)
                changed['accepted_task_fit'][key] = []
                packet = prepare_card_evidence(changed, source, profile)
                self.assertEqual(packet['decision_reasons'], [])
                self.assertEqual(packet['transferable_task_links'], [])

    def test_each_displayed_pair_must_belong_to_recorded_duty_and_profile_evidence(self):
        source, match, profile = explanation_fixture()
        for field in ('quote', 'block_reference', 'path', 'text', 'support_kind', 'task_family'):
            with self.subTest(field=field):
                changed = deepcopy(match)
                link = changed['accepted_task_fit']['task_links'][0]
                # A link's profile reference is a distinct structure in a serialized packet.
                link['profile_fact'] = deepcopy(link['profile_fact'])
                if field in ('path', 'text'):
                    link['profile_fact'][field] = 'unrecorded fact'
                else:
                    link[field] = '' if field == 'task_family' else 'unrecorded relationship'
                self.assertEqual(prepare_card_evidence(changed, source, profile)['decision_reasons'], [])

    def test_partial_relevance_does_not_change_independent_comparisons_or_limits(self):
        source, match, profile = explanation_fixture()
        packet = prepare_card_evidence(card(source), source, profile)
        packet['caveats'].append('Applicant-location eligibility still needs confirmation.')
        comparisons = deepcopy(packet['comparisons'])
        before_assessment, before_limits = render_assessment(packet), render_limits(packet)
        attach_decision(packet, match)
        self.assertEqual(packet['comparisons'], comparisons)
        self.assertEqual(render_assessment(packet), before_assessment)
        self.assertEqual(render_limits(packet), before_limits)
        self.assertIn('satisfaction of every requirement', render_fit_support(packet))
        self.assertNotIn('excellent', render_reasons(packet).lower())
        self.assertNotIn('independent worker', render_reasons(packet).lower())

    def test_interest_without_recorded_task_pairs_is_not_activity_experience(self):
        source, _, profile = explanation_fixture()
        match = dict(card(source), affirmative_fit={'supported_evidence': [dict(
            requirement='General AI evaluation or data work', profile_evidence='interest', source='preference')]})
        packet = prepare_card_evidence(match, source, profile)
        rendered = render_reasons(packet)
        self.assertIn('interest does not establish experience', rendered)
        self.assertNotIn('Your confirmed activity', rendered)
        self.assertNotIn('Employer task:', rendered)

    def test_legacy_ai_work_remains_distinct_and_unknown_basis_has_no_ai_history_claim(self):
        source, match, profile = explanation_fixture()
        for basis in (None, 'confirmed_ai_work', 'unknown_basis'):
            with self.subTest(basis=basis):
                changed = deepcopy(match)
                changed['accepted_task_fit']['basis'] = basis
                packet = prepare_card_evidence(changed, source, profile)
                rendered = render_reasons(packet)
                self.assertEqual('reported AI evaluation or annotation experience' in rendered,
                    basis in (None, 'confirmed_ai_work'))
                self.assertNotIn('Your confirmed activity', rendered)

    def test_reused_packet_drops_previous_transferable_reason_and_disclosure(self):
        source, match, profile = explanation_fixture()
        packet = prepare_card_evidence(match, source, profile)
        self.assertTrue(packet['transferable_task_links'])
        attach_decision(packet, {})
        self.assertEqual(packet['transferable_task_links'], [])
        self.assertEqual(render_reasons(packet), '')

    def test_rendered_candidate_and_employer_values_are_escaped(self):
        packet = {'decision_reasons': ['Activity <script>alert(1)</script>'],
            'transferable_task_links': [{'profile_fact': {'text': '<img src=x onerror=alert(1)>'},
                'quote': '<script>alert(2)</script>'}]}
        html = render_reasons(packet) + render_fit_support(packet)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img ', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('&lt;img ', html)


if __name__ == '__main__':
    unittest.main()
