"""One served-client chain: intake, recommendation, optional correction and tracking."""
import json
import unittest
from tests import test_private_beta_owner_correction
from tests.private_beta_matching_support import sources, CLOCK


def assert_application_destination(case, observation):
    """Inspect the real served DOM, separately from recommendation/profile evidence."""
    ui = observation['applicationUI']
    case.assertIn('application', ui['guidance'].lower())
    case.assertIn('website', ui['guidance'].lower())
    destination = ui['employer'] + '’s website' if ui['employer'] else 'the company’s website'
    case.assertIn(destination, ui['guidance'])
    case.assertNotIn('skills in your profile', ui['guidance'])
    case.assertNotIn('update your profile', ui['guidance'].lower())
    case.assertIn('Wahojobs', ui['personalizationHeading'])
    case.assertIn('recommendations', ui['personalizationHeading'].lower())
    case.assertTrue(ui['links'])
    for link in ui['links']:
        case.assertFalse(link['inApplication'])
        case.assertTrue(link['inPersonalization'])
        case.assertTrue(link['href'].startswith('/account/profile'))
        case.assertIn('Wahojobs', link['text'])
        case.assertIn('recommendations', link['text'].lower())
    case.assertTrue(ui['externalActions'])
    for action in ui['externalActions']:
        case.assertEqual(action['target'], '_blank')
        case.assertIn('noopener', action['rel'])
        case.assertFalse(action['href'].startswith('/account/profile'))
    for unsupported in ('cover letter', 'upload your', 'guaranteed interview', 'we submit'):
        case.assertNotIn(unsupported, ui['guidance'].lower())


class RecommendationClientTests(unittest.TestCase):
    journey=test_private_beta_owner_correction.PrivateBetaOwnerCorrectionTests.journey

    def test_recommendation_guidance_preference_correction_and_workflow_survive_real_http(self):
        result,persisted,returned,trace=self.journey(recommendations=True,label='recommendation-workflow')
        points={row['label']:row for row in result['observations']}
        self.assertEqual(trace['inventory'],sources())
        self.assertEqual(trace['clock'],CLOCK.isoformat())
        self.assertEqual(points['final-review-unconfirmed']['state']['profiles'],[])
        profiles=[json.loads(row['structured_profile_json']) for row in persisted['revisions']]
        self.assertEqual(len(profiles),2)
        self.assertEqual(profiles[0]['preferences']['preference_model']['workloads'],['part_time'])
        self.assertEqual(profiles[1]['preferences']['preference_model']['workloads'],['full_time'])
        self.assertTrue(profiles[0]['preferences']['remote'])
        self.assertFalse(profiles[1]['preferences']['remote'])
        for field in ('languages','education','experience','skills'):
            self.assertEqual(profiles[0][field],profiles[1][field])
        self.assertEqual(profiles[0]['experience']['years_by_domain'],[{'domain':'customer support','years':2}])
        self.assertIsNone(profiles[0]['experience']['total_years'])
        self.assertEqual(profiles[0]['preferences']['target_opportunity_types'],profiles[1]['preferences']['target_opportunity_types'])
        self.assertEqual({row['language']:row['proficiency'] for row in profiles[0]['languages']},
                         {'Portuguese':'native','English':'fluent'})
        text=points['recommendation-list']['pageText']
        self.assertIn('Your matches',text)
        self.assertNotIn('Possibilities with conditions',text)
        self.assertNotIn('No main recommendations',text)
        for label in ('recommendation-detail-before','recommendation-detail-after','recommendation-my-jobs'):
            page=points[label]['pageText']
            assert_application_destination(self, points[label])
            self.assertIn('Before you apply',page)
            self.assertIn('Brazil',page)
            self.assertNotIn('How your profile compares',page)
            self.assertNotIn('Comparison not established',page)
        self.assertIn('part-time',points['recommendation-detail-before']['pageText'].lower())
        self.assertIn('full-time',points['recommendation-detail-after']['pageText'].lower())
        self.assertEqual(len(persisted['items']),1)
        self.assertEqual(persisted['items'][0]['workflow_status'],'applied')
        self.assertTrue(persisted['items'][0]['reminder_at'])
        self.assertIn('recommendation-later-my-jobs',{row['label'] for row in returned['observations']})
        later = next(row for row in returned['observations'] if row['label']=='recommendation-later-my-jobs')
        assert_application_destination(self, later)
        self.assertTrue(any(request['status']==403 for request in result['requests']))
        self.assertTrue(any(request['status']==409 for request in result['requests']))
        self.assertTrue(all(row['recommendations']==[960012] for row in trace['rendered']))
        self.assertTrue(any(row.get('preferences',{}).get('evaluations') for row in trace['rendered']))
        self.assertEqual(result['clientErrors'],[])


if __name__=='__main__':unittest.main()
