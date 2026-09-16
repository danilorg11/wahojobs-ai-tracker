"""Recommendation materiality is not qualification certification."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from tests import test_accepted_task_matching as support
from tests.test_confirmed_activity_matching import candidate, v2
from wahojobs import authenticated_profile_matches as browser
from wahojobs.matching.recommendation_policy import condition_materiality


class RecommendationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.base = support.AcceptedTaskMatchingTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.role('AI Generalist', 'Remote - Brazil')

    def current(self, condition, heading='Requirements'):
        self.base.source('Scope of Work\n\nEvaluate AI outputs.\n\n'+heading+'\n\n'+condition)
        page, run, context = self.base.current()
        return page, run, context, self.base.match(context)

    def test_complete_ordinary_qualities_do_not_require_profile_certification(self):
        profile = deepcopy(self.base.f.profile)
        for quote in ('Strong attention to detail',
                      'Strong attention to detail with a systematic, thorough approach to tasks',
                      'Self-motivated and reliable when working independently',
                      'You must be reliable and patient.',
                      'Attention to detail is required.',
                      'Able to follow structured guidelines and apply them consistently',
                      'Follow written project guidelines.',
                      'Comfortable evaluating a broad variety of topics and content formats'):
            with self.subTest(quote=quote):
                _, _, context, match = self.current(quote)
                self.assertEqual([m['job_id'] for m in browser._primary_presentation_matches(context)], [7003])
                self.assertEqual([m['job_id'] for m in browser._recommendation_presentation_matches(context)], [7003])
                row = match['source_qualification_comparisons'][0]
                self.assertEqual(row['status'], 'unresolved')
                self.assertEqual(row['kind'], 'unassessed')
                self.assertEqual(match['non_decisive_source_questions'][0]['materiality']['basis'], 'bounded_behavior_clause')
                self.assertEqual(self.base.f.profile, profile)
                self.assertNotIn(quote, match['affirmative_fit']['satisfied_groups'])

    def test_mixed_language_credentials_history_tools_and_schedule_remain_material(self):
        for quote in ('Strong attention to detail and a medical license',
                      'Reliable when working independently as a licensed physician',
                      'Reliable with five years of experience',
                      'Clear written communication skills in English',
                      'Follow detailed legal compliance guidelines',
                      'Patient and proficient in Python',
                      'Strong attention to detail; PhD required',
                      'Reliable and available for 40 hours/week',
                      'Attention to detail is required for licensed physicians only',
                      'Strong attention to detail unless you hold a PhD'):
            with self.subTest(quote=quote):
                _, _, context, match = self.current(quote)
                self.assertNotIn('non_decisive_source_questions', match)
                self.assertFalse(browser._primary_presentation_matches(context))

    def test_behavior_does_not_create_task_support_or_specialist_history(self):
        self.base.f.profile = v2(candidate(['Interested in model output evaluation']))
        _, _, context, match = self.current('Strong attention to detail')
        self.assertFalse(browser._recommendation_presentation_matches(context))
        self.assertIsNone(match['accepted_task_fit'])
        self.assertNotIn('non_decisive_source_questions', match)

    def test_missing_degree_and_explicit_negative_keep_distinct_material_outcomes(self):
        _, _, context, match = self.current("Strong attention to detail\n\nBachelor's in Biology.")
        self.assertTrue(browser._recommendation_presentation_matches(context))
        self.assertFalse(browser._primary_presentation_matches(context))
        row = next(r for r in match['source_qualification_comparisons'] if r['kind']=='education')
        self.assertEqual(row['status'], 'not_established')
        p = candidate(['Model output evaluation'])
        p['education']['education_level'] = 'no_degree'
        from wahojobs.profiles.canonical import field_sources_for_profile
        p['provenance']['field_sources'] = field_sources_for_profile(p, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(p)
        _, _, context, match = self.current("Strong attention to detail\n\nBachelor's in Biology.")
        self.assertEqual(match['affirmative_fit_status'], 'conflicting')
        self.assertFalse(browser._recommendation_presentation_matches(context))

    def test_generic_behavior_cannot_override_explicit_language_or_geography_conflict(self):
        p = candidate(['Model output evaluation'])
        p['languages'].append(dict(language='French', proficiency='basic', proficiency_explicit=True,
                                   locale='', confidence='high'))
        from wahojobs.profiles.canonical import field_sources_for_profile
        p['provenance']['field_sources'] = field_sources_for_profile(p, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(p)
        _, _, context, match = self.current('Strong attention to detail\n\nNative French required')
        self.assertFalse(browser._recommendation_presentation_matches(context))
        self.assertTrue(any(r['status']=='contradicted' for r in match['source_language_checks']))
        self.base.role('AI Generalist', 'Remote - United States only')
        _, _, context, match = self.current('Strong attention to detail')
        self.assertEqual(match['location_eligibility_status'], 'incompatible')
        self.assertFalse(browser._recommendation_presentation_matches(context))

    def test_materiality_is_bound_to_exact_clause_and_does_not_mutate_comparisons(self):
        _, _, context, match = self.current('Strong attention to detail')
        packet = context['_card_evidence'][7003]
        row = packet['comparisons'][0]
        original = deepcopy((row, packet))
        self.assertFalse(condition_materiality(row, packet)['admission_decisive'])
        for field, value in (('job_id', 7777), ('external_id', 'foreign'), ('url', 'https://other.example.test'),
                             ('source_hash', 'other'), ('quote', 'Reliable'), ('line', 99),
                             ('block_reference', 'other'), ('heading', 'other')):
            with self.subTest(field=field):
                changed = deepcopy(row); changed['source'][field] = value
                self.assertTrue(condition_materiality(changed, packet)['admission_decisive'])
        for change in ({'kind':'education'}, {'kind':'professional_background'}, {'kind':'language'},
                       {'kind':'workload'}, {'status':'contradicted'}, {'modality':'conflicting'},
                       {'message':'A material condition is not established.'}):
            self.assertTrue(condition_materiality(dict(row, **change), packet)['admission_decisive'])
        self.assertEqual((row, packet), original)

    def test_source_change_recomputes_behavior_without_inheriting_old_exemption(self):
        _, old, before, prior = self.current('Strong attention to detail')
        self.assertTrue(browser._primary_presentation_matches(before))
        self.base.source("Scope of Work\n\nEvaluate AI outputs.\n\nRequirements\n\nBachelor's in Biology.")
        _, _, after = self.base.current('/find-matches?run='+old.match_run_id)
        current = self.base.match(after)
        self.assertFalse(browser._primary_presentation_matches(after))
        self.assertTrue(browser._recommendation_presentation_matches(after))
        self.assertNotIn('non_decisive_source_questions', current)
        self.assertEqual(prior['score_components'], current['score_components'])

    def test_combined_list_keeps_one_cap_and_existing_order_without_category_bonus(self):
        _, _, context, match = self.current('Strong attention to detail')
        primary, conditional = [], []
        for i in range(15):
            value = dict(match, job_id=8000+i, canonical_opportunity_id=9000+i,
                         score=100-i, ranking_score=100-i, preview_section='also_worth_reviewing',
                         presentation_source_section='also_worth_reviewing',
                         presentation_data_status='recently_verified')
            (conditional if i % 2 == 0 else primary).append(value)
        # A stale high-scoring row cannot jump over current sources. A duplicate
        # from another route must not occupy another slot or change identity.
        primary += [dict(conditional[0]), dict(primary[0], job_id=9998,
                    canonical_opportunity_id=9999, score=999, ranking_score=999,
                    presentation_data_status='recently_cached')]
        with patch.object(browser, '_primary_presentation_matches', return_value=primary), \
             patch.object(browser, '_conditional_presentation_matches', return_value=conditional):
            result = browser._recommendation_presentation_matches(context)
        self.assertEqual([m['job_id'] for m in result], list(range(8000,8010)))
        self.assertEqual([m['presentation_rank'] for m in result], list(range(1,11)))
        self.assertTrue(all(m['presentation_source_section']=='also_worth_reviewing' for m in result))

    def test_detail_membership_uses_combined_cap_not_internal_route_membership(self):
        from wahojobs.authenticated_variant_details import find_presented_variant
        _, _, context, match = self.current('Strong attention to detail')
        primary, conditional = [], []
        for i in range(12):
            value = dict(match, job_id=8000+i, canonical_opportunity_id=9000+i,
                         score=100-i, ranking_score=100-i, preview_section='also_worth_reviewing',
                         presentation_source_section='also_worth_reviewing',
                         presentation_data_status='recently_verified',
                         conditional_task_fit=i % 2 == 0, primary_recommendation_eligible=i % 2 != 0)
            (conditional if i % 2 == 0 else primary).append(value)
        with patch.object(browser, '_primary_presentation_matches', return_value=primary), \
             patch.object(browser, '_conditional_presentation_matches', return_value=conditional):
            self.assertEqual(find_presented_variant(context, 9008, 8008)['_detail_recommendation_section'], 'conditional')
            self.assertEqual(find_presented_variant(context, 9009, 8009)['_detail_recommendation_section'], 'main')
            self.assertIsNone(find_presented_variant(context, 9010, 8010))
            self.assertIsNone(find_presented_variant(context, 9011, 8011))
            self.assertIsNone(find_presented_variant(context, 9008, 8009))
