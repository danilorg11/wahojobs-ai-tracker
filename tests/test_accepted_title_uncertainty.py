"""Accepted source qualifications remain distinct from title and career totals."""
from copy import deepcopy
import unittest

from tests import test_accepted_task_matching as task_support
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_confirmed_activity_matching import candidate, v2
from wahojobs import authenticated_profile_matches as browser
from wahojobs.candidate_condition_comparisons import compare_conditions, _professional_background
from wahojobs.matching.accepted_tasks import needs_accepted_task_comparison
from wahojobs.profiles.canonical import field_sources_for_profile


BODY = """## About the role

We are hiring expert Evaluators in **Customer success / support operations** to review and assess AI-generated work products (documents, spreadsheets, and slide decks) for accuracy, rigor, and domain quality. You will apply deep subject-matter expertise to grade outputs.

This is a remote, hourly engagement.

## Requirements (must have)

1. **5+ years of relevant professional experience in Customer success / support operations.**

2. **Native or professional fluency in English.**

3. **Highly proficient in Microsoft Office and Google Workspace, especially Slides** (Google Slides / PowerPoint).

## Preferred (nice to have)

- Advanced degree (Master's or higher) from a reputable institution.

## What you'll do

- Evaluate AI-generated artifacts against domain-specific quality rubrics.

- Identify factual, aesthetic, and presentation errors.

- Provide clear, structured written feedback."""


def profile(role=None, total=None):
    result = candidate(['Data annotation', 'Model output evaluation'])
    result['experience']['recent_roles'] = [role] if role else []
    result['experience']['total_years'] = total
    result['provenance']['field_sources'] = field_sources_for_profile(result, 'user_confirmation', explicit=True)
    return v2(result)


class AcceptedTitleComparisonTests(unittest.TestCase):
    role = task_support.AcceptedTaskMatchingTests.role
    source = task_support.AcceptedTaskMatchingTests.source
    current = task_support.AcceptedTaskMatchingTests.current
    match = task_support.AcceptedTaskMatchingTests.match

    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.f.profile = profile()
        self.role('Customer success / support operations Evaluator')
        self.source(BODY)

    def test_generic_and_practitioner_reach_all_exact_requirements_without_promotion(self):
        for role, years in ((None, None), ('Customer support specialist', 6)):
            with self.subTest(role=role):
                self.f.profile = profile(role, years)
                _, _, context = self.current()
                match = self.match(context)
                rows = match['source_qualification_comparisons']
                self.assertEqual(len(rows), 4)
                self.assertEqual([r['modality'] for r in rows], ['required'] * 3 + ['preferred'])
                self.assertEqual(rows[0]['kind'], 'professional_background')
                self.assertEqual(rows[0]['supported_parts'], [])
                self.assertIn('5+ relevant professional years', rows[0]['message'])
                self.assertIn('relationship between the named source domains remains unresolved', rows[0]['message'])
                self.assertEqual(rows[0]['status'], 'unresolved' if role else 'not_established')
                if role:
                    self.assertIn('6 total career years do not establish', rows[0]['message'])
                facts = {f['field_path']: f for f in rows[0]['profile_facts']}
                self.assertEqual(facts['experience.total_years']['value'], years)
                self.assertEqual(facts['experience.years_by_domain']['value'], [])
                self.assertEqual(facts['experience.recent_roles']['value'], [role] if role else [])
                self.assertIn('Native or professional fluency in English.', rows[1]['source']['quote'])
                self.assertIn('Microsoft Office and Google Workspace', rows[2]['source']['quote'])
                self.assertEqual([r['status'] for r in rows[1:]], ['unresolved'] * 3)
                self.assertTrue(all(r['source']['job_id'] == 7003 for r in rows))
                self.assertTrue(all(r['source']['source_hash'] == rows[0]['source']['source_hash'] for r in rows))
                self.assertFalse(match['primary_recommendation_eligible'])
                self.assertFalse(match['conditional_task_fit'])
                self.assertEqual(match['affirmative_fit_status'], 'uncertain')
                self.assertFalse(match['affirmative_fit']['conflicting_requirements'])
                self.assertNotIn(7003, {m['job_id'] for m in browser._primary_presentation_matches(context) + browser._conditional_presentation_matches(context)})
                self.assertNotIn('source_qualification_comparisons', self.match(context, 7006))

    def test_other_unmodeled_titles_reuse_required_background_comparison(self):
        self.role('Uncatalogued assessment position')
        self.source("Responsibilities\n\nEvaluate AI outputs.\n\nRequirements\n\nHands-on experience in a marketing role")
        self.f.profile = profile('Marketing specialist', 6)
        _, _, context = self.current()
        match = self.match(context)
        rows = match['source_qualification_comparisons']
        self.assertEqual(rows[0]['kind'], 'professional_background')
        self.assertEqual(rows[0]['supported_parts'], ['related role: marketing'])
        self.assertEqual(rows[0]['status'], 'unresolved')
        self.assertTrue(match['conditional_task_fit'])
        self.assertFalse(match['primary_recommendation_eligible'])

    def test_no_task_evidence_or_other_requirements_cannot_enter_new_review_path(self):
        base = dict(accepted_task_fit={'facts': ['bound duty']}, eligible_for_personalized=True,
                    job_id=7003, canonical_opportunity_id=7003, url='https://example.test/role',
                    affirmative_fit_status='uncertain', actionability_cap_reasons=[],
                    affirmative_fit=dict(status='uncertain', missing_requirements=[], conflicting_requirements=[],
                                         unmodeled_requirements=['Title-defining role or specialization']))
        self.assertTrue(needs_accepted_task_comparison(base))
        for change in ({'accepted_task_fit': None}, {'eligible_for_personalized': False},
                       {'professional_domain_hard_gate_applied': True}, {'job_is_active': False},
                       {'location_eligibility_status': 'incompatible'},
                       {'actionability_cap_reasons': ['mandatory_language_proficiency_conflict']},
                       {'actionability_cap_reasons': ['unsupported_specialization']}):
            self.assertFalse(needs_accepted_task_comparison(dict(base, **change)))
        for key in ('missing_requirements', 'conflicting_requirements'):
            changed = deepcopy(base)
            changed['affirmative_fit'][key] = ['Biology PhD']
            self.assertFalse(needs_accepted_task_comparison(changed))

    def test_reached_source_residence_conflict_and_waivers_are_retained(self):
        self.source(BODY + '\n\nRequirements\n\nMust be based in the United States.')
        _, _, context = self.current()
        match = self.match(context)
        self.assertFalse(match['primary_recommendation_eligible'])
        self.assertNotIn(7003, {m['job_id'] for m in browser._conditional_presentation_matches(context)})
        packet = dict(job_id=1, external_id='example', url='https://example.test/role', source_hash='hash',
                      captured_at='2026-09-10', caveats=[], kind='Advertised role', conditions=[dict(
                          heading='Requirements (must have)', reference='source block 1',
                          text='1. No relevant professional experience required.\n2. Native English preferred.')])
        rows = compare_conditions(packet, profile())
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['kind'], 'professional_background')
        self.assertEqual(rows[1]['modality'], 'conflicting')

    def test_duration_clause_reuses_professional_comparison_and_explicit_denials(self):
        clause = '5+ years of relevant professional experience in marketing'
        related = _professional_background(clause, profile('Marketing specialist', 6))
        self.assertEqual(related[0], 'unresolved')
        self.assertEqual(related[3], ['related role: marketing'])
        self.assertIn('6 total career years do not establish', related[1])
        for field in ('marketing', 'Customer success / support operations'):
            candidate_profile = candidate(['Model output evaluation'])
            candidate_profile['constraints']['hard_constraints'] = ['No professional experience in ' + field]
            candidate_profile['provenance']['field_sources'] = field_sources_for_profile(candidate_profile, 'user_confirmation', explicit=True)
            confirmed = v2(candidate_profile)
            result = _professional_background('5+ years of relevant professional experience in ' + field, confirmed)
            self.assertEqual(result[0], 'contradicted')
            self.assertEqual(result[3], [])
            confirmed['provenance']['field_sources'] = []
            self.assertNotEqual(_professional_background('5+ years of relevant professional experience in ' + field, confirmed)[0], 'contradicted')


if __name__ == '__main__':
    unittest.main()
