"""Public requirement wording with anonymous profiles; no personal fixtures."""
import unittest

from tests.test_source_language_proficiency import profile
from tests.test_accepted_task_matching import AcceptedTaskMatchingTests
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs import authenticated_profile_matches as browser
from wahojobs.matching.accepted_tasks import _prepare_eligibility
from wahojobs.matching.languages import prepare_language_conditions, compare_language_condition


CLAUSE = ('Bilingual proficiency in Spanish and English with excellent written '
          'and verbal communication skills.')
HEADING = 'Required Skills and Qualifications'


def candidate(level='advanced'):
    value = profile(level)
    for language in value['languages']:
        if language['language'] == 'German':
            language['language'] = 'Spanish'
    return value


class BilingualConditionTests(unittest.TestCase):
    def test_language_note_requires_current_exact_source_reference(self):
        from copy import deepcopy
        from tests.test_authenticated_card_evidence import SOURCES, card, PROFILE
        from wahojobs.authenticated_card_evidence import prepare_card_evidence
        source = deepcopy(SOURCES[11242])
        ref = dict(job_id=source['job_id'], source_url=source['url'],
                   material_content_sha256=source['material_content_sha256'])
        check = dict(source_reference=ref, modality='required', status='unresolved',
                     message='Confirm the requested language level.')
        match = dict(card(source), source_language_checks=[check])
        self.assertEqual(prepare_card_evidence(match, source, PROFILE)['language_notes'], [check['message']])
        for key in ref:
            stale = deepcopy(match)
            stale['source_language_checks'][0]['source_reference'][key] = 'different'
            self.assertEqual(prepare_card_evidence(stale, source, PROFILE)['language_notes'], [])

    def test_explicit_future_pool_uses_existing_card_and_detail_kind(self):
        from tests.test_candidate_condition_comparisons import prepared
        from wahojobs.authenticated_card_evidence import render_card_evidence
        text = ('Apply now to become part of our exclusive talent pool for Data Annotators. '
                'Once you apply, you can complete a brief AI-powered interview that '
                'helps us assess your fit and qualifies you for upcoming roles.')
        packet = prepared(text)
        self.assertEqual(packet['kind'], 'Talent network — future consideration')
        self.assertIn(text, packet['kind_quote'])
        self.assertIn('Join for future projects', render_card_evidence(packet, 'test'))
        for other in ('Join our talent pool.', 'We are hiring for upcoming roles.',
                      'Do not join our talent pool for future roles.'):
            self.assertNotEqual(prepared(other)['kind'], 'Talent network — future consideration')

    def test_exact_clause_required_pair_and_unresolved_advanced(self):
        clauses, _ = _prepare_eligibility('hash', 'source', 'id', 'https://example.test/job',
            HEADING + '\n\n' + CLAUSE, 'text/plain', '{}')
        self.assertEqual(len(clauses), 1)
        clause = clauses[0]
        self.assertEqual(clause['languages'], ['english', 'spanish'])
        self.assertEqual(clause['operator'], 'all_of')
        self.assertEqual(clause['levels'], ['bilingual'])
        self.assertEqual(clause['modality'], 'required')
        self.assertEqual(clause['quote'], CLAUSE)
        self.assertTrue(clause['source_field'])
        self.assertEqual(compare_language_condition(candidate(), clause)['status'], 'unresolved')

    def test_levels_alternatives_and_missing_proficiency(self):
        for wording in ('Bilingual proficiency in Spanish and English',
                        'Working fluency in Spanish and English',
                        'Working proficiency in Spanish and English',
                        'Native or near-native Spanish and English'):
            clause = prepare_language_conditions(wording, 'required')[0]
            for level, status in [('advanced', 'unresolved'), ('unknown', 'unresolved'),
                                  ('basic', 'contradicted')]:
                self.assertEqual(compare_language_condition(candidate(level), clause)['status'], status)
        alternative = prepare_language_conditions('Bilingual proficiency in Spanish or Portuguese', 'required')[0]
        self.assertEqual(compare_language_condition(candidate('basic'), alternative)['status'], 'supported')
        both = prepare_language_conditions('Bilingual proficiency in Spanish and Portuguese', 'required')[0]
        self.assertEqual(compare_language_condition(candidate('basic'), both)['status'], 'contradicted')
        for clause in prepare_language_conditions('Native Spanish or bilingual proficiency in English', 'required'):
            self.assertEqual(clause['modality'], 'unresolved')

    def test_preferred_negation_and_title_are_not_required(self):
        for text, mode, expected in [
            ('Bilingual proficiency in Spanish and English', 'preferred', 'preferred'),
            ('Bilingual proficiency in Spanish and English not required', 'required', 'not_required'),
            ('Bilingual proficiency in Spanish and English preferred', 'required', 'unresolved')]:
            self.assertEqual(prepare_language_conditions(text, mode)[0]['modality'], expected)
        self.assertEqual(prepare_language_conditions('Spanish Speaker', 'required'), [])
        self.assertEqual(_prepare_eligibility('h', 's', 'i', 'u', CLAUSE, 'text/plain', '{}'), ((), ()))


class AuthenticatedBilingualTests(unittest.TestCase):
    role = AcceptedTaskMatchingTests.role
    source = AcceptedTaskMatchingTests.source
    current = AcceptedTaskMatchingTests.current
    match = AcceptedTaskMatchingTests.match

    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.f.profile = candidate()
        self.role('Spanish AI Data Reviewer', 'Remote - Brazil')

    def test_unknown_conditional_conflict_excluded_and_old_run_revalidated(self):
        self.source('Scope of Work\n\nEvaluate AI outputs.')
        _, old, ctx = self.current()
        score = self.match(ctx)['score']
        self.source('Scope of Work\n\nEvaluate AI outputs.\n\n' + HEADING + '\n\n' + CLAUSE
                    + '\n\n## Additional Information\n\nJoin our talent pool for upcoming roles.')
        response, _, ctx = self.current('/find-matches?run=' + old.match_run_id)
        match = self.match(ctx)
        self.assertEqual(match['score'], score)
        self.assertFalse(match['primary_recommendation_eligible'])
        self.assertTrue(match['conditional_task_fit'])
        self.assertEqual(match['source_language_checks'][0]['status'], 'unresolved')
        self.assertIn(b'Join for future projects', response.body)
        self.assertIn(b'Confirm whether your stated', response.body)
        self.assertEqual(match['source_language_checks'][0]['source_reference']['job_id'], 7003)
        self.assertNotIn(7003, {m['job_id'] for m in browser._primary_presentation_matches(ctx)})
        self.assertIn(7003, {m['job_id'] for m in browser._conditional_presentation_pool(ctx)})
        self.f.profile = candidate('basic')
        _, _, ctx = self.current('/find-matches?run=' + old.match_run_id)
        match = self.match(ctx)
        self.assertIn('mandatory_language_proficiency_conflict', match['actionability_cap_reasons'])
        self.assertNotIn(7003, {m['job_id'] for m in browser._primary_presentation_matches(ctx)
                              + browser._conditional_presentation_pool(ctx)})
        self.assertFalse(browser.local_product.recent_cached_match_is_usable(match, allow_conditional_task_fit=True))
        self.assertTrue(all(7003 not in {m['job_id'] for m in s['matches']}
                            for s in browser._presented_relaxation_scenarios(ctx)))


if __name__ == '__main__':
    unittest.main()
