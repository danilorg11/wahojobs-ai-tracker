"""Confirmed tasks feed existing signals, without manufacturing specialties."""
from copy import deepcopy
import json
import unittest
from unittest import mock

from scripts.profile_match_digest import AI_EVALUATION_SIGNAL
from tests import test_source_task_fit as task_support
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs import authenticated_profile_matches as browser
from wahojobs.profiles.canonical import (
    canonical_to_matcher_profile, matcher_profile_to_canonical, field_sources_for_profile,
)
from wahojobs.profiles.canonical_v2 import convert_v1_to_v2, project_v2_to_matcher_v1


def candidate(activities=(), skills=()):
    raw = dict(profile_id='synthetic-task-worker', display_name='Synthetic Task Worker',
        summary='', languages=['English', 'Portuguese'], country='Brazil',
        skills=list(skills), degrees_or_domains=[], work_preferences=[], constraints=[],
        target_opportunity_types=[], notes='', avoid_keywords=[],
        signals=[('Declared language capability', ['language', 'linguistic', 'english', 'portuguese'], 7)])
    c = matcher_profile_to_canonical(raw)
    c['experience']['specialties'] = list(activities)
    c['provenance']['reviewed'] = True
    c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
    return c


def v2(c):
    return convert_v1_to_v2(json.loads(json.dumps(c)), persistent_profile_id='prf_' + '0' * 31 + '1',
                            source_ordinal_resolver=lambda *_: [1])


class ConfirmedActivityMatchingTests(unittest.TestCase):
    def test_declared_activities_reuse_exact_existing_signal_without_profile_mutation(self):
        c = candidate(['Model output evaluation', 'Data annotation', 'Rubric development'])
        before = deepcopy(c)
        m = canonical_to_matcher_profile(c)
        self.assertEqual(m['signals'][-1], AI_EVALUATION_SIGNAL)
        self.assertEqual(len(m['signals']), 2)
        self.assertEqual(c, before)
        self.assertEqual(canonical_to_matcher_profile(c)['signals'], m['signals'])
        self.assertEqual(m['specialties'], c['experience']['specialties'])

    def test_language_interest_titles_and_negation_do_not_create_task_experience(self):
        for text in ('Interested in AI evaluation', 'No AI evaluation experience',
                     'Want to learn data annotation', 'Clinical evaluation', 'Portuguese fluency'):
            with self.subTest(text=text):
                self.assertEqual(len(canonical_to_matcher_profile(candidate([text]))['signals']), 1)
        c = candidate()
        c['experience']['job_titles'] = ['AI Trainer']
        c['experience']['recent_roles'] = ['AI Trainer at Example Company']
        c['preferences']['target_opportunity_types'] = ['AI evaluation']
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.assertEqual(len(canonical_to_matcher_profile(c)['signals']), 1)

    def test_skill_statement_does_not_become_proficiency_or_other_profession(self):
        c = candidate(skills=['AI evaluation'])
        m = canonical_to_matcher_profile(c)
        self.assertEqual(len(m['signals']), 2)
        self.assertEqual(m['skills'], ['AI evaluation'])
        self.assertFalse(m['degrees_or_domains'])
        self.assertNotIn('translation', str(m['signals']))
        self.assertNotIn('coding', str(m['signals']))
        self.assertEqual(c['experience']['total_years'], None)

    def test_existing_task_signal_is_not_counted_twice(self):
        c = candidate(['Data annotation'])
        c['derived_matcher_signals']['signals'].append(dict(
            reason='data_annotation_signal', keywords=['annotation'], points=7,
            evidence=[], confidence='high'))
        self.assertEqual(len(canonical_to_matcher_profile(c)['signals']), 2)

    def test_unconfirmed_legacy_profile_is_unchanged(self):
        c = candidate(['Data annotation'])
        c['provenance']['reviewed'] = False
        self.assertEqual(len(canonical_to_matcher_profile(c)['signals']), 1)

    def test_specialist_linguistics_needs_related_background_and_preferred_degree_not_required(self):
        c = candidate(['AI evaluation', 'Data annotation', 'Video review'])
        source = task_support.SOURCES[0]
        m = task_support.match(source)
        result = task_support.apply_source_task_fit(m, source, v2(c))
        self.assertEqual(result['affirmative_fit_status'], 'uncertain')
        self.assertFalse(result['conditional_task_fit'])
        self.assertEqual(result['preview_section'], 'explore_only')
        self.assertFalse(result['affirmative_fit']['conflicting_requirements'])
        self.assertNotIn('degree', str(result['affirmative_fit']['missing_requirements']).lower())
        self.assertFalse(result['source_task_fit']['profile_facts'])

    def test_explicit_incompatibility_is_not_overridden_by_task_signal(self):
        f = SyntheticMatcherFixture()
        self.addCleanup(f.close)
        f.update_inventory("UPDATE jobs SET title='Portuguese AI Evaluator - Data Annotation',department='',expertise='',commitment='',location='Remote - United States only'")
        f.update_inventory("UPDATE canonical_opportunities SET canonical_title='Portuguese AI Evaluator - Data Annotation',source_category=''")
        f.profile = v2(candidate(['AI evaluation']))
        self.assertEqual(f.get().status, 200)
        context = f.last_run().recommendation_context
        self.assertEqual(browser._primary_presentation_matches(context), [])
        self.assertEqual(browser._conditional_presentation_matches(context), [])
        assessed = [r for rows in context['matches'].values() for r in rows]
        self.assertTrue(assessed)
        for result in assessed:
            self.assertEqual(result['location_eligibility_status'], 'incompatible')

    def test_authenticated_reuse_respects_added_and_removed_activity(self):
        original = browser.local_product.secrets.token_urlsafe
        action_keys = mock.patch.object(browser.local_product.secrets, 'token_urlsafe',
            side_effect=lambda n: 'synthetic-action-key' if n == 24 else original(n))
        action_keys.start()
        self.addCleanup(action_keys.stop)
        f = SyntheticMatcherFixture()
        self.addCleanup(f.close)
        f.update_inventory("UPDATE jobs SET title='Portuguese AI Evaluator - Data Annotation',department='',expertise='',commitment='',location='Remote - Brazil'")
        f.update_inventory("UPDATE canonical_opportunities SET canonical_title='Portuguese AI Evaluator - Data Annotation',source_category=''")
        f.profile = v2(candidate())
        before = f.get()
        self.assertEqual(before.status, 200)
        old = f.last_run()
        self.assertEqual(browser._primary_presentation_matches(old.recommendation_context), [])
        f.profile = v2(candidate(['AI evaluation', 'Data annotation']))
        result = f.get('/find-matches?run=' + old.match_run_id)
        self.assertEqual(result.status, 200)
        new = f.last_run()
        main = browser._primary_presentation_matches(new.recommendation_context)
        self.assertTrue(main)
        self.assertEqual(main[0]['score_components']['profile_signal_score'], 15)
        current_url = '/find-matches?run=' + new.match_run_id
        self.assertEqual(f.get(current_url).body, result.body)
        f.profile = v2(candidate())
        self.assertEqual(f.get(current_url).status, 200)
        self.assertEqual(browser._primary_presentation_matches(f.last_run().recommendation_context), [])
