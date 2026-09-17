"""Served forms, real verified HTTPS and durable beginner-interest projection."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from tests.private_beta_demo_support import beta_state, beta_application, BACKGROUND
from tests.private_beta_matching_support import CLOCK, sources
from tests.recommendation_demo_support import configure_samples
from tests.test_recommendation_demo import sample_client
from tests.test_first_time_candidate import observe
from tests.test_private_beta_owner_correction import observe_matching
from tests.test_recommendation_client import assert_application_destination
from tests.candidate_decision_support import verified_https_request


HISTORY = ('I have two years of customer support experience. '
           'I answer customer questions, review written responses, check information against instructions, '
           'and organize spreadsheet records. ')
UNKNOWN = BACKGROUND.replace(HISTORY, '')
FIRST_JOB = UNKNOWN.replace('My name is Alex. ',
    'My name is Alex. I am looking for my first job. I have no prior work experience. ')
CASES = {'experienced': BACKGROUND, 'history_unknown': UNKNOWN, 'first_job': FIRST_JOB}


def narrative_for(state, narrative):
    path = state.directory/'private-beta-demo.json'
    marker = json.loads(path.read_text())
    marker['recommendation_samples']['alex']['background'] = narrative
    path.write_text(json.dumps(marker))


class BeginnerAccessClientTests(unittest.TestCase):
    def test_original_inventory_paired_text_draft_confirmation_projection_and_return(self):
        results = []
        for label, narrative in CASES.items():
            with self.subTest(label=label), beta_state(now=CLOCK) as state:
                configure_samples(state)
                narrative_for(state, narrative)
                with patch('tests.test_candidate_continuity_client.https_request', verified_https_request), observe_matching() as trace:
                    with beta_application(state):
                        client = sample_client(state, 'alex')
                        before_return = observe(state)
                        returned = sample_client(state, 'alex', returning=True)
                persisted = observe(state)
                self.assertEqual(persisted['revisions'], before_return['revisions'])
                self.assertEqual(len(persisted['profiles']), 1)
                self.assertEqual(len(persisted['revisions']), 1)
                points = {p['label']: p for p in client['observations']}
                self.assertEqual(points['sample-final-review']['state']['profiles'], [])
                self.assertEqual(client['clientErrors'], [])
                self.assertEqual(returned['clientErrors'], [])
                canonical = json.loads(persisted['revisions'][0]['structured_profile_json'])
                original = [s for s in persisted['sources'] if s['source_type']=='confirmed_about_you_text']
                self.assertEqual(len(original), 1)
                self.assertEqual(original[0]['source_content'], narrative)
                self.assertEqual(original[0]['source_content_sha256'], hashlib.sha256(narrative.encode()).hexdigest())
                self.assertEqual(original[0]['revision_id'], persisted['revisions'][0]['revision_id'])
                self.assertEqual(original[0]['profile_id'], persisted['profiles'][0]['profile_id'])
                self.assertEqual(canonical['location']['country'], 'Brazil')
                self.assertEqual({v['language']:v['proficiency'] for v in canonical['languages']},
                                 {'Portuguese':'native', 'English':'fluent'})
                self.assertIsNone(canonical['experience']['total_years'])
                if label == 'experienced':
                    self.assertEqual(canonical['experience']['years_by_domain'], [{'domain':'customer support','years':2}])
                    self.assertTrue(canonical['experience']['specialties'])
                else:
                    for key in ('years_by_domain','job_titles','professional_domains','specialties','employment_history'):
                        self.assertFalse(canonical['experience'].get(key), key)
                negative = 'no prior experience' in canonical['constraints']['hard_constraints']
                self.assertEqual(negative, label == 'first_job')
                self.assertEqual(points['sample-text-draft']['form'].get('no_experience') == '1', negative)
                if negative:
                    self.assertIn('No prior work experience', points['sample-text-draft']['summaries']['section-experience'])
                    self.assertIn('No prior work experience', points['sample-final-review']['pageText'])
                    self.assertIn('No prior work experience', points['sample-profile']['pageText'])
                self.assertIn('no college degree', canonical['constraints']['hard_constraints'])
                self.assertEqual(canonical['preferences']['preference_model']['workloads'], ['part_time'])
                self.assertEqual(trace['inventory'], sources())
                self.assertTrue(trace['rendered'])
                self.assertTrue(all(r['inventory_count']==16 for r in trace['rendered']))
                self.assertTrue(all(r['recommendations']==[960012] for r in trace['rendered']))
                details = [p for p in client['observations'] if p['label'].startswith('sample-detail-')]
                self.assertEqual(len(details), 1)
                for detail in details:
                    assert_application_destination(self, detail)
                    self.assertIn('open to beginners', detail['pageText'])
                    self.assertIn('interest in “AI evaluation”', detail['pageText'])
                    if label != 'experienced':
                        self.assertIn('explain what interests you about the evaluation tasks', detail['applicationUI']['guidance'])
                        self.assertNotIn('describe your experience', detail['applicationUI']['guidance'])
                results.append(dict(label=label,narrative=narrative,confirmed=canonical,
                    initial_draft=points['sample-text-draft']['form'],final_review=points['sample-final-review']['pageText'],
                    details=details,confirmed_sources=original,trace=deepcopy(trace)))
        if os.environ.get('WAHOJOBS_CLIENT_EVIDENCE'):
            path = Path(os.environ['WAHOJOBS_CLIENT_EVIDENCE'])/'beginner-original16-contrast.json'
            self.assertFalse(path.exists(), 'Do not replace earlier contrast evidence')
            path.write_text(json.dumps(dict(clock=CLOCK.isoformat(),inventory=sources(),cases=results,
                complete=len(results)==len(CASES)),indent=2,default=str))

    def test_explicit_no_history_can_be_cleared_without_sticky_hidden_value_or_workflow_loss(self):
        with beta_state(now=CLOCK) as state:
            configure_samples(state)
            narrative_for(state, FIRST_JOB)
            with patch('tests.test_candidate_continuity_client.https_request', verified_https_request), beta_application(state):
                result = sample_client(state, 'alex', clear_no_experience=True)
                persisted = observe(state)
                returned = sample_client(state, 'alex', returning=True)
            self.assertEqual(result['clientErrors'], [])
            self.assertEqual(returned['clientErrors'], [])
            revisions = [json.loads(r['structured_profile_json']) for r in persisted['revisions']]
            self.assertEqual(len(revisions), 2)
            self.assertIn('no prior experience', revisions[0]['constraints']['hard_constraints'])
            self.assertNotIn('no prior experience', revisions[1]['constraints']['hard_constraints'])
            self.assertEqual(revisions[0]['experience'], revisions[1]['experience'])
            self.assertEqual(revisions[0]['languages'], revisions[1]['languages'])
            self.assertEqual(revisions[0]['preferences'], revisions[1]['preferences'])
            self.assertEqual(observe(state)['revisions'], persisted['revisions'])
            points = {p['label']:p for p in result['observations']}
            self.assertNotIn('No prior work experience', points['beginner-clear-review']['pageText'])
            self.assertIn('Applied', points['beginner-tracked-detail']['pageText'])
            self.assertIn('Reminder set', points['beginner-tracked-detail']['pageText'])
            self.assertEqual(len(persisted['items']), 1)
            self.assertEqual([row['action_name'] for row in persisted['transitions']],
                ['user_created', 'product_noop_save', 'product_remind_later', 'product_applied'])

    def test_additional_beginner_practice_owner_preserves_existing_profile_and_unused_fresh_entry(self):
        with beta_state(now=CLOCK) as state:
            configure_samples(state, include_beginner=True)
            with patch('tests.test_candidate_continuity_client.https_request', verified_https_request), beta_application(state):
                sample_client(state, 'alex')
                before = observe(state)
                result = sample_client(state, 'beginner')
                after = observe(state)
                for key in ('profiles','revisions','sources','items','transitions'):
                    for row in before[key]:
                        self.assertIn(row, after[key], key)
                self.assertEqual(len(after['profiles']), 2)
                self.assertEqual(len(after['revisions']), 2)
                self.assertEqual(result['clientErrors'], [])
                points = {p['label']:p for p in result['observations']}
                self.assertTrue(points['sample-matches']['matchCards'])
                self.assertEqual(points['sample-final-review']['state']['profiles'], before['profiles'])
                sample_client(state, 'beginner', returning=True, switch_sample='alex')
                self.assertEqual(observe(state)['revisions'], after['revisions'])
            with closing(sqlite3.connect(state.database_path)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_identities WHERE provider_subject='recommendation-practice-fresh'").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_identities WHERE provider_subject='recommendation-practice-beginner'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
