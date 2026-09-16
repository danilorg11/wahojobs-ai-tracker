"""Owner narrative through shipped client, HTTPS, drafts, confirmation and Matches.

All identities/source acceptance times are controlled synthetic fixtures. Observers
delegate to real consumers and never create profile values or successful responses.
"""
from contextlib import contextmanager
from contextlib import closing
from copy import deepcopy
import json
import os
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.private_beta_demo_support import beta_state, beta_application, BACKGROUND
from tests.private_beta_matching_support import CLOCK, sources
from tests.test_first_time_candidate import client, observe
from tests.candidate_decision_support import verified_https_request
from wahojobs import authenticated_profile_matches as browser
from wahojobs import authenticated_source_detail


@contextmanager
def observe_matching():
    trace={'clock':CLOCK.isoformat(),'inventory':sources(),'projected':[],'evaluated':[],'rendered':[],'exact_details':[]}
    build=browser.profile_preview.build_preview_context_from_canonical_rows
    render=browser._render_match_results
    detail=authenticated_source_detail.prepare_detail_display
    def exact(job,profile):
        result=detail(job,profile)
        trace['exact_details'].append(deepcopy(result))
        return result
    def project(canonical, **kwargs):
        trace['projected'].append(deepcopy(canonical))
        prior=kwargs.get('evaluated_match_sink')
        def sink(match):
            trace['evaluated'].append(deepcopy(match))
            if prior is not None:prior(match)
        return build(canonical,**dict(kwargs,evaluated_match_sink=sink))
    def rendered(context, **kwargs):
        trace['rendered'].append(dict(inventory_count=kwargs['inventory_count'],
            main=[m['job_id'] for m in browser._primary_presentation_matches(context)],
            conditional=[m['job_id'] for m in browser._conditional_presentation_matches(context)],
            recommendations=[m['job_id'] for m in browser._recommendation_presentation_matches(context)],
            matches=deepcopy(context['matches']),hidden=context.get('_hidden_posting_ids'),
            preferences=deepcopy(context.get('_typed_preference_enforcement'))))
        return render(context,**kwargs)
    with patch.object(browser.profile_preview,'build_preview_context_from_canonical_rows',side_effect=project),patch.object(browser,'_render_match_results',side_effect=rendered),patch.object(authenticated_source_detail,'prepare_detail_display',side_effect=exact):
        yield trace


class PrivateBetaOwnerCorrectionTests(unittest.TestCase):
    def journey(self, *, background=BACKGROUND, editors=False, durations=False, recommendations=False, label='owner'):
        def observe_draft(state):
            result=observe(state)
            sidecar=state.database_path.with_name(state.database_path.name+'.correction-drafts.sqlite3')
            result['manual_drafts']=[]
            if sidecar.exists():
                with closing(sqlite3.connect(sidecar.as_uri()+'?mode=ro',uri=True)) as c:
                    c.execute('PRAGMA query_only=ON')
                    if c.execute("SELECT 1 FROM sqlite_master WHERE name='manual_profile_drafts'").fetchone():
                        result['manual_drafts']=[json.loads(r[0]) for r in c.execute('SELECT payload_json FROM manual_profile_drafts')]
            return result
        with beta_state(now=CLOCK) as state, patch('tests.test_candidate_continuity_client.https_request',verified_https_request), patch('tests.test_first_time_candidate.observe',side_effect=observe_draft), observe_matching() as trace:
            marker=state.directory/'first-time-candidate.json'
            data=json.loads(marker.read_text());data.update(background=background,exercise_editors=editors,exercise_durations=durations,
                                                          exercise_recommendations=recommendations)
            marker.write_text(json.dumps(data))
            with beta_application(state): result=client(state,'owner-correction')
            persisted=observe(state)
            state.close_harnesses()
            with beta_application(state): returned=client(state,'owner-return')
            self.assertEqual(observe(state)['revisions'],persisted['revisions'])
        root=os.environ.get('WAHOJOBS_CLIENT_EVIDENCE')
        if root:
            p=Path(root)/(label+'-matching-trace.json')
            assert not p.exists()
            p.write_text(json.dumps(trace,indent=2,default=str),encoding='utf-8')
        return result,persisted,returned,trace

    def test_exact_narrative_client_to_confirmation_return_and_matching(self):
        result,persisted,returned,trace=self.journey()
        checkpoints={r['label']:r for r in result['observations']}
        initial=checkpoints['initial-narrative-draft']['form']
        def languages(form):
            return {form['language_'+str(i)]:form['language_proficiency_'+str(i)] for i in range(8) if form.get('language_'+str(i))}
        self.assertEqual(languages(initial),{'Portuguese':'native','English':'fluent'})
        self.assertIn('customer service',initial['professional_domains'].lower())
        self.assertEqual(initial['total_years'],'','Domain duration is not total career duration')
        for label in ('done-language-editing','reopened-language-editing','saved-draft-reloaded','validation-error-preserves-choices','stale-recovery'):
            with self.subTest(stage=label):
                checkpoint=checkpoints[label]
                self.assertEqual(languages(checkpoint['form']),{'Portuguese':'native','English':'fluent'})
                self.assertIn('Native',checkpoint['summaries']['section-languages'])
                self.assertIn('Fluent',checkpoint['summaries']['section-languages'])
        review=checkpoints['final-review-unconfirmed']
        self.assertEqual(review['state']['profiles'],[])
        self.assertRegex(review['pageText'],r'Portuguese.{0,60}[Nn]ative')
        self.assertRegex(review['pageText'],r'English.{0,60}[Ff]luent')
        profile=json.loads(persisted['revisions'][0]['structured_profile_json'])
        self.assertEqual(profile['identity']['display_name'],'Alex')
        self.assertEqual({v['language']:v['proficiency'] for v in profile['languages']},{'English':'fluent','Portuguese':'native'})
        self.assertEqual(profile['experience']['total_years'],None)
        self.assertEqual(profile['experience']['years_by_domain'],[{'domain':'customer support','years':2}])
        self.assertEqual(profile['experience']['job_titles'],[])
        self.assertEqual(profile['experience']['recent_roles'],[])
        self.assertEqual(set(profile['experience']['specialties']),{'answer customer questions','review written responses','check information against instructions','organize spreadsheet records'})
        self.assertEqual(profile['education']['education_level'],'high_school')
        self.assertEqual(profile['education']['completion_status'],'completed')
        self.assertIn('no college degree',profile['constraints']['hard_constraints'])
        self.assertTrue({'customer support','data entry','writing','attention to detail','python'}<=set(profile['skills']['normalized']))
        self.assertEqual(set(profile['preferences']['target_opportunity_types']),{'AI evaluation','data annotation','language review','customer support'})
        self.assertEqual(profile['preferences']['employment_types'],['part-time'])
        self.assertTrue(profile['preferences']['remote'])
        self.assertEqual(profile['location']['work_authorization'],'unknown')
        self.assertTrue(all(not v['locale'] for v in profile['languages']))
        initial_draft=checkpoints['initial-narrative-draft']['state']['manual_drafts'][0]['canonical']
        self.assertEqual(initial_draft['provenance']['original_text'],BACKGROUND)
        self.assertTrue(all(not f['explicit'] for f in initial_draft['provenance']['field_sources'].values()))
        self.assertEqual(len(persisted['revisions']),1)
        self.assertTrue(trace['projected'],'Normal authenticated canonical projection ran')
        for canonical in trace['projected']:
            self.assertEqual({v['language']:v['proficiency'] for v in canonical['languages']},{'English':'fluent','Portuguese':'native'})
            self.assertEqual(canonical['experience']['total_years'],None)
        self.assertEqual({m['job_id'] for m in trace['evaluated']},{s['job_id'] for s in sources()})
        self.assertTrue(all(r['inventory_count']==16 for r in trace['rendered']))
        self.assertGreaterEqual(len(trace['exact_details']),3)
        self.assertTrue(all(960015 not in r['main']+r['conditional'] for r in trace['rendered']))
        french=[m for m in trace['evaluated'] if m['job_id']==960015]
        self.assertTrue(all(not m['eligible_for_personalized'] for m in french))
        self.assertTrue(returned['observations'])
        self.assertEqual(result['clientErrors'],[])

    def test_unspecified_levels_and_missing_education_then_manual_choices_persist(self):
        result,persisted,_,trace=self.journey(background='I live in Brazil. My name is Sam. I speak Portuguese and English. My skills include writing.',label='unspecified')
        points={r['label']:r for r in result['observations']}
        initial=points['initial-narrative-draft']['form']
        self.assertEqual(initial['language_proficiency_0'],'unspecified')
        self.assertEqual(initial['language_proficiency_1'],'unspecified')
        self.assertNotIn('no_degree',initial)
        profile=json.loads(persisted['revisions'][0]['structured_profile_json'])
        self.assertEqual({v['language']:v['proficiency'] for v in profile['languages']},{'Portuguese':'native','English':'fluent'})
        self.assertEqual(profile['education']['education_level'],'not_specified')
        self.assertNotIn('no college degree',profile['constraints']['hard_constraints'])
        self.assertEqual(profile['experience']['years_by_domain'],[])
        self.assertTrue(trace['projected'])

    def test_repeated_field_actions_reach_http_without_empty_or_erased_facts(self):
        result,persisted,_,_=self.journey(editors=True,label='editors')
        points={r['label']:r for r in result['observations']}
        added=points['added-unspecified-language-saved']['state']['manual_drafts'][0]['canonical']
        french=next(v for v in added['languages'] if v['language']=='French')
        self.assertIn(french['proficiency'],('unspecified','unknown'))
        profile=json.loads(persisted['revisions'][0]['structured_profile_json'])
        self.assertEqual({v['language'] for v in profile['languages']},{'Portuguese','English'})
        self.assertEqual(profile['experience']['job_titles'],[])
        self.assertEqual(profile['experience']['recent_roles'],[])
        self.assertEqual(len(profile['experience']['specialties']),4)
        self.assertNotIn('Temporary synthetic edit',str(profile))

    def test_existing_domain_duration_can_be_corrected_and_explicitly_removed_via_http(self):
        result,persisted,_,trace=self.journey(durations=True,label='durations')
        points={r['label']:r for r in result['observations']}
        profiles=[json.loads(r['structured_profile_json']) for r in persisted['revisions']]
        self.assertEqual(len(profiles),3)
        self.assertEqual(profiles[0]['experience']['years_by_domain'],[{'domain':'customer support','years':2}])
        self.assertEqual(profiles[1]['experience']['years_by_domain'],[{'domain':'customer support','years':1}])
        self.assertEqual(profiles[2]['experience']['years_by_domain'],[])
        self.assertEqual(len(points['duration-change-review']['state']['revisions']),1)
        self.assertEqual(len(points['duration-remove-review']['state']['revisions']),2)
        for profile in profiles:
            self.assertIsNone(profile['experience']['total_years'])
            self.assertEqual(profile['languages'],profiles[0]['languages'])
            self.assertEqual(profile['skills'],profiles[0]['skills'])
        self.assertTrue(trace['projected'])


if __name__=='__main__':unittest.main()
