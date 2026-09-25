"""Exact computation-reuse contracts, without inventory or external services."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import threading
import unittest
from unittest.mock import patch

from scripts import profile_to_matches_preview as preview
from wahojobs.matching import evaluation_memo as memo
from wahojobs.matching import evergreen, fit_evidence
from wahojobs.matching.fit_evidence import (
    AffirmativeFitAssessment, FitEvidence, RequirementGroup,
)
from wahojobs.matching.opportunity_trust import OpportunityTrustAssessment

matcher = preview.matcher


class ProfileMemoTests(unittest.TestCase):
    def tearDown(self):
        self.assertIsNone(memo._current.get())
        self.assertIsNone(memo._profile.get())

    def test_nested_scope_and_exception_restore_outer_identity_and_cache(self):
        calls = []
        outer_profile, inner_profile = {'value': 'outer'}, {'value': 'inner'}

        @memo.memoized_profile
        def project(profile):
            calls.append(profile['value'])
            return profile['value']

        @memo.evaluation_scope
        def inner(profile):
            self.assertEqual(project(profile), 'inner')
            self.assertEqual(project(profile), 'inner')
            raise ValueError('expected')

        @memo.evaluation_scope
        def outer(profile):
            self.assertEqual(project(profile), 'outer')
            outer_cache = memo._current.get()
            with self.assertRaises(ValueError):
                inner(inner_profile)
            self.assertIs(memo._profile.get(), profile)
            self.assertIs(memo._current.get(), outer_cache)
            self.assertEqual(project(profile), 'outer')

        outer(outer_profile)
        self.assertEqual(calls, ['outer', 'inner'])

    def test_threads_with_overlapping_evaluations_do_not_share_profiles(self):
        barrier = threading.Barrier(2)
        calls = []

        @memo.memoized_profile
        def project(profile):
            calls.append(profile['value'])
            return {profile['value']}

        @memo.evaluation_scope
        def evaluate(profile):
            first = project(profile)
            barrier.wait(timeout=5)
            second = project(profile)
            self.assertIs(memo._profile.get(), profile)
            return first, second

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(evaluate, [{'value': 'a'}, {'value': 'b'}]))
        self.assertEqual(results, [({'a'}, {'a'}), ({'b'}, {'b'})])
        self.assertCountEqual(calls, ['a', 'b'])

    def test_changed_profile_next_scope_and_other_identity_are_not_stale(self):
        calls = []

        @memo.memoized_profile
        def project(profile):
            calls.append(profile['value'])
            return profile['value']

        @memo.evaluation_scope
        def evaluate(profile):
            other = {'value': 'other'}
            self.assertEqual(project(other), 'other')
            other['value'] = 'changed-other'
            self.assertEqual(project(other), 'changed-other')
            return project(profile), project(profile)

        profile = {'value': 'before'}
        self.assertEqual(evaluate(profile), ('before', 'before'))
        profile['value'] = 'after'
        self.assertEqual(evaluate(profile), ('after', 'after'))
        self.assertEqual(calls.count('before'), 1)
        self.assertEqual(calls.count('after'), 1)
        self.assertEqual(project(profile), 'after')
        self.assertEqual(calls.count('after'), 2)

    def test_returned_sets_are_independent_and_cache_is_bounded(self):
        calls = []

        @memo.memoized_profile
        def project(profile, label):
            calls.append(label)
            return {profile['value'], label}

        @memo.evaluation_scope
        def evaluate(profile):
            project(profile, 'first').add('caller-mutation')
            self.assertEqual(project(profile, 'first'), {'value', 'first'})
            for index in range(140):
                project(profile, str(index))
            self.assertEqual(len(memo._current.get()[project.__wrapped__]), 128)
            project(profile, '139')

        evaluate({'value': 'value'})
        self.assertEqual(calls.count('first'), 1)
        self.assertEqual(calls.count('139'), 2)

    def test_keyword_arguments_remain_valid_inside_and_outside_scope(self):
        @memo.memoized_profile
        def project(profile, label='default'):
            return profile['value'] + label

        @memo.evaluation_scope
        def evaluate(profile):
            self.assertEqual(project(profile=profile, label='keyword'), 'valuekeyword')
            self.assertEqual(project(profile), 'valuedefault')
            self.assertTrue(preview.profile_has_term(profile, terms=('python',)))
            self.assertTrue(preview.profile_confirms_credential(profile, label='bachelor'))

        profile = {'value': 'value', 'skills': ['python'], 'education_level': 'bachelor'}
        evaluate(profile=profile)
        self.assertEqual(project(profile=profile, label='outside'), 'valueoutside')


class ExactSerializerTests(unittest.TestCase):
    def assert_exact(self, actual, expected):
        self.assertIs(type(actual), type(expected))
        self.assertEqual(actual, expected)
        if isinstance(expected, dict):
            self.assertEqual(list(actual), list(expected))
            for key in expected:
                self.assert_exact(actual[key], expected[key])
        elif isinstance(expected, (tuple, list)):
            for first, second in zip(actual, expected):
                self.assert_exact(first, second)

    def test_trust_serializer_matches_asdict_for_primitives_and_tuple_reasons(self):
        value = OpportunityTrustAssessment(
            status='unverified_source', reasons=('first', 'second'),
            job_is_active=True, canonical_is_active=None, job_last_seen_at='2026-09-06',
            latest_successful_source_run_at='', source_age_hours=None,
            inventory_model='live_feed', market_count_policy='count_live',
            freshness_max_age_hours=72, source_run_id=None,
            source_run_qualifies=False, selected_variant_id=42,
        )
        self.assert_exact(value.as_dict(), asdict(value))
        output = value.as_dict()
        output['status'] = 'caller-mutation'
        self.assert_exact(value.as_dict(), asdict(value))

    def test_fit_serializer_preserves_nested_shapes_without_mutable_aliases(self):
        value = AffirmativeFitAssessment(
            status='supported',
            supported_evidence=(FitEvidence('Python', 'Python work', 'profile'),),
            required_groups=(RequirementGroup('role:python', 'Python', 'all_of', ('python',), 'title'),),
            satisfied_groups=('Python',), missing_requirements=(),
            conflicting_requirements=(), unmodeled_requirements=(),
            location_and_locale_evidence=('Brazil',), adjacencies_used=(),
            why_fit_statements=('Grounded explanation',),
        )
        output = value.as_dict()
        self.assert_exact(output, asdict(value))
        output['supported_evidence'][0]['requirement'] = 'caller-mutation'
        output['required_groups'][0]['concepts'] = ('changed',)
        self.assert_exact(value.as_dict(), asdict(value))
        self.assertIsNot(output['required_groups'][0], value.as_dict()['required_groups'][0])


class AcceptedTaskReuseTests(unittest.TestCase):
    def test_explicit_none_does_not_recompute_and_matches_omitted_results(self):
        row = dict(title='Generic AI evaluator', canonical_title='', source_category='',
                   expertise='', department='')
        features = {'professional_domains': set()}
        with patch.object(matcher, 'detect_profile_match_features', return_value=features), \
             patch.object(matcher, 'detect_role_match_features', return_value=features), \
             patch.object(matcher, 'has_meaningful_positive_evidence', return_value=False), \
             patch.object(matcher, 'has_generic_only_evidence', return_value=True), \
             patch('wahojobs.matching.accepted_tasks.matched_accepted_tasks', return_value=None) as tasks:
            expected = matcher.match_quality_gate_penalties({}, row, text='generic')
            self.assertEqual(tasks.call_count, 2)
            tasks.reset_mock()
            actual = matcher.match_quality_gate_penalties({}, row, text='generic',
                accepted_fit=None, confirmed_task_fit=None)
            tasks.assert_not_called()
            self.assertEqual(actual, expected)
            self.assertEqual(actual, [('Match is based mostly on generic AI-work terms', 10)])


class AdditionalProjectionTests(unittest.TestCase):
    def test_fit_profile_predicates_refresh_between_evaluations(self):
        @memo.evaluation_scope
        def evaluate(profile):
            first = (fit_evidence._profile_requests_general_ai_work(profile),
                     fit_evidence._profile_has_software_evidence(profile))
            self.assertEqual(first, (
                fit_evidence._profile_requests_general_ai_work(profile),
                fit_evidence._profile_has_software_evidence(profile)))
            return first

        profile = {'summary': 'AI evaluation', 'degrees_or_domains': ['Software Engineering'],
                   'skills': ['Python']}
        self.assertEqual(evaluate(profile), (True, True))
        profile.update(summary='Sales', degrees_or_domains=['Sales'], skills=['Communication'])
        self.assertEqual(evaluate(profile), (False, False))
        self.assertIsNone(memo._profile.get())

    def test_evergreen_normalization_reuses_bounded_immutable_results(self):
        evergreen.normalize.cache_clear()
        self.addCleanup(evergreen.normalize.cache_clear)
        cases = [(None, ''), ('', ''), (' CAFÉ/Software–Engineering ', 'cafe software engineering'),
                 ('C++ / C#', 'c c'), ('São Paulo', 'sao paulo')]
        for original, expected in cases:
            self.assertEqual(evergreen.normalize(original), expected)
            self.assertEqual(evergreen.normalize(original), expected)
        self.assertEqual(evergreen.normalize.cache_info().hits, len(cases))
        for index in range(16400):
            evergreen.normalize(f'bounded unique source label {index}')
        self.assertLessEqual(evergreen.normalize.cache_info().currsize, 16384)
        self.assertEqual(evergreen.normalize(' CAFÉ/Software–Engineering '), 'cafe software engineering')


if __name__ == '__main__':
    unittest.main()
