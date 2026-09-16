"""Entry-level policy through the actual served client and confirmed revision.

The same sixteen historical/synthetic sources and clock remain intact. These
checks do not seed candidate qualifications or manufacture successful responses.
"""
from html import unescape
import json
import unittest

from tests.private_beta_demo_support import BACKGROUND
from tests.private_beta_matching_support import sources, CLOCK
from tests import test_private_beta_owner_correction as owner_correction


class TransferableTaskClientTests(unittest.TestCase):
    def journey(self, **kwargs):
        return owner_correction.PrivateBetaOwnerCorrectionTests.journey(self, **kwargs)

    def test_confirmed_non_ai_activities_reach_conditional_list_and_exact_detail(self):
        result, persisted, returned, trace = self.journey(label='transferable-positive')
        self.assertEqual(trace['inventory'], sources())
        self.assertEqual(trace['clock'], CLOCK.isoformat())
        self.assertEqual(len(persisted['revisions']), 1)
        profile = json.loads(persisted['revisions'][0]['structured_profile_json'])
        self.assertEqual(profile['experience']['years_by_domain'],
                         [{'domain': 'customer support', 'years': 2}])
        self.assertIsNone(profile['experience']['total_years'])
        self.assertEqual(profile['experience']['job_titles'], [])
        self.assertEqual(profile['experience']['recent_roles'], [])
        self.assertNotIn('confirmed_transferable_activity_evidence', profile)
        self.assertNotIn('confirmed_ai_work_evidence', profile)
        self.assertEqual({r['language']: r['proficiency'] for r in profile['languages']},
                         {'Portuguese': 'native', 'English': 'fluent'})
        points = {r['label']: r for r in result['observations']}
        self.assertEqual(points['final-review-unconfirmed']['state']['profiles'], [])
        for rendered in trace['rendered']:
            self.assertEqual(rendered['inventory_count'], 16)
            self.assertEqual(rendered['main'], [])
            self.assertEqual(rendered['conditional'], [960012])
            selected = [m for rows in rendered['matches'].values() for m in rows]
            match = next(m for m in selected if m['job_id'] == 960012)
            fit = match['accepted_task_fit']
            self.assertEqual(fit['basis'], 'transferable_activity')
            self.assertTrue(fit['scope_evidence'])
            self.assertTrue(fit['task_links'])
            self.assertTrue(match['source_qualification_comparisons'])
            self.assertTrue(match['conditional_task_fit'])
            self.assertFalse(match['primary_recommendation_eligible'])
            self.assertTrue(match['source_task_fit']['conditions'])
            for fact in fit['profile_facts']:
                self.assertIn(fact['text'], profile['experience']['specialties'])
                self.assertTrue(fact['path'].startswith('experience.specialties['))
            self.assertNotIn(960014, rendered['conditional'], 'Python skill is not professional practice')
            self.assertNotIn(960015, rendered['conditional'], 'AI waiver does not waive French')
        for label in ('confirmed-matches', 'generalist-exact-detail'):
            html = unescape(points[label]['pageText'])
            self.assertIn('Your confirmed activity', html)
            self.assertIn('review written responses', html)
            self.assertIn('does not establish prior professional AI work', html)
            self.assertNotIn('describes AI evaluation or annotation work', html)
        self.assertTrue(returned['observations'])
        self.assertEqual(result['clientErrors'], [])

    def test_same_interests_and_skills_without_performed_tasks_do_not_create_overlap(self):
        activities = ('I answer customer questions, review written responses, check information '
                      'against instructions, and organize spreadsheet records. ')
        self.assertIn(activities, BACKGROUND)
        _, persisted, _, trace = self.journey(
            background=BACKGROUND.replace(activities, ''), label='transferable-interest-only')
        profile = json.loads(persisted['revisions'][0]['structured_profile_json'])
        self.assertEqual(profile['experience']['specialties'], [])
        self.assertIn('python', profile['skills']['normalized'])
        self.assertIn('AI evaluation', profile['preferences']['target_opportunity_types'])
        self.assertEqual(trace['inventory'], sources())
        for rendered in trace['rendered']:
            self.assertEqual(rendered['main'], [])
            self.assertEqual(rendered['conditional'], [])
        for match in trace['evaluated']:
            self.assertNotEqual((match.get('accepted_task_fit') or {}).get('basis'),
                                'transferable_activity')


if __name__ == '__main__':
    unittest.main()
