"""Beginner policy through authenticated list/detail; synthetic contrasts only.

The preserved Generalist body is unchanged. Additional bodies below are labelled
contract controls and never added to the measured 16-source inventory.
"""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_accepted_task_matching as support
from tests.test_confirmed_activity_matching import candidate, v2
from wahojobs import authenticated_profile_matches as browser
from wahojobs.profiles.canonical import field_sources_for_profile, canonical_to_matcher_profile
from wahojobs.matching.beginner_access import confirmed_interests


ENTRY = ('About the role\n\nThis is an entry-level role.\n\nResponsibilities\n\n'
         'Review and evaluate AI-generated content.\n\nRequirements\n\n'
         'Follow written project guidelines.')
PRESERVED = Path(__file__).parent/'fixtures/source_requirement_fidelity/accepted-generalist.txt'


class BeginnerAccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.base = support.AcceptedTaskMatchingTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.role('Generalist', 'Remote')
        self.install()

    def install(self, *, activities=(), interests=('AI evaluation',), no_history=False, **sections):
        c = candidate(activities)
        c['preferences']['target_opportunity_types'] = list(interests)
        for language in c['languages']:
            language.update(proficiency='fluent', proficiency_explicit=True)
        if no_history:
            c['constraints']['hard_constraints'] = ['no prior experience']
        for key, changes in sections.items():
            c[key].update(changes)
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(c)
        return c

    def current(self, body=ENTRY):
        self.base.source(body)
        page, run, context = self.base.current()
        return page, run, context, self.base.match(context)

    def shown(self, context):
        return browser._recommendation_presentation_matches(context)

    def detail(self, run, match):
        from wahojobs import authenticated_variant_details as details
        with patch.object(details, 'prepare_variant_notice', wraps=details.prepare_variant_notice) as notice:
            page = self.base.f.get(details.variant_detail_url(match, run_id=run.match_run_id))
        self.assertEqual(page.status, 200)
        return notice.call_args.args[0]['_authenticated_local_checks']['match']

    def test_preserved_source_admits_experienced_unknown_history_and_explicit_no_history(self):
        body = PRESERVED.read_text(encoding='utf-8')
        profiles = []
        for activities, no_history in ((['review written responses'], False), ([], False), ([], True)):
            with self.subTest(activities=activities, no_history=no_history):
                self.install(activities=activities, no_history=no_history)
                saved = deepcopy(self.base.f.profile)
                _, run, context, match = self.current(body)
                self.assertEqual([m['job_id'] for m in self.shown(context)], [7003])
                fit = match['accepted_task_fit']
                self.assertEqual(fit['basis'], 'beginner_interest')
                self.assertEqual(bool(fit.get('related_activity_fit')), bool(activities))
                self.assertTrue(fit['interest_links'])
                self.assertTrue(all(f['path'].startswith('preferences.target_opportunity_types[') for f in fit['profile_facts']))
                self.assertTrue(all(r['source_kind']=='user_confirmation' for f in fit['profile_facts'] for r in f['provenance']))
                self.assertEqual(self.detail(run, match)['accepted_task_fit'], fit)
                comparisons = match['source_qualification_comparisons']
                self.assertEqual(len(comparisons), 9)
                self.assertEqual(sum(r['status']=='unresolved' and r['modality']=='unspecified' for r in comparisons), 5)
                self.assertEqual(len(match['non_decisive_source_questions']), 4)
                self.assertTrue(match['conditional_task_fit'])  # English writing remains unassessed.
                self.assertEqual(self.base.f.profile, saved)
                self.assertIsNone(saved['experience']['total_years'])
                profiles.append(saved)
        self.assertNotEqual(profiles[1]['constraints']['hard_constraints'], profiles[2]['constraints']['hard_constraints'])
        self.assertEqual(profiles[1]['experience'], profiles[2]['experience'])

    def test_different_complete_beginner_wording_and_duties_use_shared_route(self):
        for proof in ('Open to beginners.', 'This role is open to first-time workers.',
                      'No specialized background is required — just curiosity and a willingness to learn.'):
            with self.subTest(proof=proof):
                _, run, context, match = self.current(ENTRY.replace('This is an entry-level role.', proof))
                self.assertEqual([m['job_id'] for m in self.shown(context)], [7003])
                self.assertEqual(self.detail(run, match)['accepted_task_fit']['basis'], 'beginner_interest')
                self.assertEqual(match['source_qualification_comparisons'][0]['status'], 'unresolved')
                self.assertTrue(match['primary_recommendation_eligible'])

    def test_interest_must_align_with_actual_accepted_duty(self):
        for interests in ([], ['remote'], ['customer support'], ['AI coding evaluation'], ['data annotation'], ['anything']):
            with self.subTest(interests=interests):
                self.install(interests=interests)
                _, run, context, match = self.current()
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))
                self.assertIsNone(self.detail(run, match)['accepted_task_fit'])
        self.install(interests=['data annotation'])
        _, _, context, match = self.current(ENTRY.replace('Review and evaluate AI-generated content.', 'Label images for AI training.'))
        self.assertTrue(self.shown(context))
        self.assertEqual(match['accepted_task_fit']['interest_links'][0]['task_family'], 'data_annotation')

    def test_source_title_or_ai_only_waiver_does_not_prove_beginner_access(self):
        self.base.role('Entry-level Generalist')
        for proof in ('No prior AI experience required.', 'Our other program is open to beginners.',
                      'This role is open to beginners who are experienced biologists.',
                      'No specialized background is required; for licensed physicians only.'):
            with self.subTest(proof=proof):
                _, _, context, match = self.current(ENTRY.replace('This is an entry-level role.', proof))
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))

    def test_required_history_is_not_replaced_by_interest_or_ai_experience_waiver(self):
        for requirement in ('Previous customer service experience required.', 'Two years of work experience required.',
                            '5 years of relevant professional experience in customer support required.',
                            'Previous AI annotation work is required.', 'Experience with Python required.',
                            'Previous employment is required.', 'Applicants need a history of paid employment.'):
            with self.subTest(requirement=requirement):
                _, run, context, match = self.current(ENTRY+'\n\n'+requirement+'\n\nNo prior AI experience required.')
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))
                self.assertIsNone(self.detail(run, match)['accepted_task_fit'])

    def test_preferred_history_does_not_become_mandatory(self):
        _, _, context, match = self.current(ENTRY+'\n\nPreferred Qualifications\n\nPrevious customer service experience preferred.')
        self.assertTrue(self.shown(context))
        self.assertEqual(match['accepted_task_fit']['basis'], 'beginner_interest')
        self.assertTrue(any(r['modality']=='preferred' and r['status']!='supported' for r in match['source_qualification_comparisons']))
        for condition in ('No previous employment is required.', 'Previous employment is not required.',
                          'Paid employment begins after onboarding.'):
            with self.subTest(condition=condition):
                _, _, context, match = self.current(ENTRY+'\n\n'+condition)
                self.assertTrue(match['accepted_task_fit'])
                self.assertTrue(self.shown(context))

    def test_candidate_directed_prior_work_under_about_cannot_hide_behind_entry_statement(self):
        for requirement in ('You will need two years of customer-support experience.',
                            'Applicants need previous customer service experience.',
                            'Candidates should have previous work experience.'):
            with self.subTest(requirement=requirement):
                body = ENTRY.replace('This is an entry-level role.', 'This is an entry-level role.\n\n'+requirement)
                _, run, context, match = self.current(body)
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))
                self.assertIsNone(self.detail(run, match)['accepted_task_fit'])

    def test_candidate_directed_specialist_credentials_under_about_remain_mandatory(self):
        for requirement in ('You will need a medical license.', 'Applicants need a PhD in Biology.'):
            for activities in ([], ['review written responses']):
                with self.subTest(requirement=requirement, activities=activities):
                    self.install(activities=activities)
                    body = ENTRY.replace('This is an entry-level role.', 'This is an entry-level role.\n\n'+requirement)
                    _, run, context, match = self.current(body)
                    self.assertIsNone(match['accepted_task_fit'])
                    self.assertFalse(self.shown(context))
                    self.assertIsNone(self.detail(run, match)['accepted_task_fit'])

    def test_source_grounded_beginner_relevance_is_not_rejected_as_title_only_generic_evidence(self):
        self.base.role('AI Content Reviewer', 'Remote')
        with patch('wahojobs.matching.beginner_access.confirmed_interests', return_value=[]):
            _, _, prior_context, prior = self.current()
        self.assertFalse(self.shown(prior_context))
        self.assertEqual(prior['score_components']['quality_gate_penalty'], 10)
        _, _, context, match = self.current()
        self.assertEqual(match['accepted_task_fit']['basis'], 'beginner_interest')
        self.assertTrue(self.shown(context))
        self.assertEqual(match['score_components']['quality_gate_penalty'], 0)
        self.assertEqual(match['score_components']['profile_signal_score'], prior['score_components']['profile_signal_score'])
        self.assertFalse(self.base.f.profile['experience']['specialties'])
        # Record this synthetic mechanism contrast separately from the intact
        # original-16 comparison. The existing penalty is accurately classified;
        # no ranking weight or cap changes and no AI-history signal is awarded.
        import json, os
        if os.environ.get('WAHOJOBS_CLIENT_EVIDENCE'):
            path = Path(os.environ['WAHOJOBS_CLIENT_EVIDENCE'])/'beginner-quality-gate.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as handle:
                json.dump(dict(authority='Synthetic additional contract fixture; task-interest projection disabled only in before counterfactual.',
                    before=dict(score=prior['score'], components=prior['score_components'], shown=[]),
                    after=dict(score=match['score'], components=match['score_components'], shown=[m['job_id'] for m in self.shown(context)])), handle, indent=2)
        self.install(interests=[])
        _, _, context, match = self.current()
        self.assertIsNone(match['accepted_task_fit'])
        self.assertFalse(self.shown(context))
        self.assertGreater(match['score_components']['quality_gate_penalty'], 0)

    def test_specialist_credentials_and_ambiguous_background_stay_protected(self):
        for requirement in ('Licensed physician required.', 'PhD in Biology required.',
                            'Hands-on molecular biology experience.', 'Prior professional experience required.'):
            with self.subTest(requirement=requirement):
                _, _, context, match = self.current(ENTRY+'\n\n'+requirement+'\n\nNo prior AI experience required.')
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))
        self.base.role('Biology Expert')
        _, _, context, match = self.current()
        self.assertFalse(self.shown(context))
        self.assertNotEqual(match['affirmative_fit_status'], 'supported')

    def test_degree_unknown_and_explicit_absence_keep_existing_independent_outcomes(self):
        body = ENTRY+"\n\nBachelor's in Humanities."
        _, _, context, match = self.current(body)
        self.assertTrue(self.shown(context))
        self.assertEqual(next(r for r in match['source_qualification_comparisons'] if r['kind']=='education')['status'], 'not_established')
        self.install(education={'education_level':'no_degree'})
        _, run, context, match = self.current(body)
        self.assertFalse(self.shown(context))
        self.assertEqual(match['affirmative_fit_status'], 'conflicting')
        self.assertEqual(self.detail(run, match)['affirmative_fit_status'], 'conflicting')

    def test_country_language_and_availability_conflicts_are_not_waived(self):
        for condition in ('Must be based in Canada', 'Commitment: 20 hours/week'):
            with self.subTest(condition=condition):
                self.install(preferences={'availability':'unavailable'} if 'Commitment' in condition else {})
                _, run, context, match = self.current(ENTRY+'\n\n'+condition)
                self.assertTrue(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))
                self.assertFalse(self.detail(run, match)['primary_recommendation_eligible'])

    def test_source_language_missing_and_explicit_below_required_level_are_distinct(self):
        body = ENTRY+'\n\nNative French required'
        _, _, context, match = self.current(body)
        self.assertTrue(match['accepted_task_fit'])
        self.assertTrue(self.shown(context))
        self.assertTrue(match['conditional_task_fit'])
        self.assertFalse(any(r['status']=='contradicted' for r in match['source_task_fit']['conditions']))
        c = self.install()
        french = deepcopy(c['languages'][0]); french.update(language='French', proficiency='basic', proficiency_explicit=True)
        c['languages'].append(french)
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(c)
        _, run, context, match = self.current(body)
        self.assertFalse(self.shown(context))
        # This explicit level conflict was rejected by the earlier accepted
        # eligibility comparator, before late qualification review was reached.
        self.assertTrue(any(r['status']=='contradicted' for r in match['source_language_checks']))
        self.assertEqual(match['primary_admission_source'], 'accepted_source_eligibility')
        self.assertIn('mandatory_language_proficiency_conflict', match['actionability_cap_reasons'])
        self.assertNotIn('source_qualification_comparisons', match)
        self.assertFalse(self.detail(run, match)['primary_recommendation_eligible'])

    def test_confirmed_firm_workload_limit_still_executes_after_interest_relevance(self):
        from tests.test_profile_preference_model import with_preference_model
        from wahojobs.profiles.preference_model import empty_profile_preferences_v2
        model = empty_profile_preferences_v2(); model['workloads'] = ['part_time']
        self.install(constraints={'hard_constraints':['part-time only']})
        self.base.f.profile = with_preference_model(self.base.f.profile, model)
        self.base.f.update_inventory("UPDATE jobs SET commitment='Full-time' WHERE id=7003")
        _, _, context, match = self.current()
        self.assertTrue(match['accepted_task_fit'])
        self.assertFalse(self.shown(context))
        evaluated = next(r for r in context['_typed_preference_enforcement']['evaluations'] if r['opportunity_reference']=='canonical:7002')
        self.assertEqual(evaluated['admission']['status'], 'exclude')
        outcome = next(r for r in evaluated['outcomes'] if r['criterion_id']=='preferences.workloads')
        self.assertEqual((outcome['criterion_class'], outcome['outcome']), ('strict_preference','fail'))

    def test_interest_path_does_not_change_existing_scores_or_create_ai_history(self):
        for activities in ([], ['review written responses'], ['Model output evaluation']):
            with self.subTest(activities=activities):
                c = self.install(activities=activities)
                projected = canonical_to_matcher_profile(c, include_task_evidence=True)
                if not activities:
                    self.assertNotIn('confirmed_ai_work_evidence', projected)
                    self.assertNotIn('confirmed_transferable_activity_evidence', projected)
                with patch('wahojobs.matching.beginner_access.confirmed_interests', return_value=[]):
                    _, _, _, before = self.current()
                _, _, _, after = self.current()
                self.assertEqual(after['score_components'], before['score_components'])
                self.assertEqual(after['score'], before['score'])
                self.assertEqual(after['accepted_task_fit']['basis'], 'beginner_interest')

    def test_stale_or_unconfirmed_projected_interest_cannot_survive_durable_binding(self):
        c = self.install()
        projected = confirmed_interests(c)
        for change in ('text', 'provenance', 'external_import'):
            with self.subTest(change=change):
                self.install(interests=['customer support'] if change=='text' else ['AI evaluation'])
                if change in ('provenance', 'external_import'):
                    for ref in self.base.f.profile['provenance']['field_sources']:
                        if ref['field_path']=='preferences.target_opportunity_types[0]':
                            ref['source_kind'] = 'external_import' if change=='external_import' else 'parsed_free_text'
                with patch('wahojobs.matching.beginner_access.confirmed_interests', return_value=projected):
                    _, run, context, match = self.current()
                    local = self.detail(run, match)
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))
                self.assertIsNone(local['accepted_task_fit'])
                self.assertFalse(local['primary_recommendation_eligible'])

    def test_foreign_source_cannot_supply_beginner_scope_or_task_fit(self):
        self.base.source(ENTRY, source_url='https://example.test/another-posting')
        _, _, context = self.base.current()
        self.assertIsNone(self.base.match(context)['accepted_task_fit'])
        self.assertFalse(self.shown(context))


class BeginnerHistoryNormalizationTests(unittest.TestCase):
    def profile(self, text):
        from scripts.local_product_app import normalize_identity_free_profile_input
        return normalize_identity_free_profile_input(text, 'short_paragraph', allow_fallbacks=False).to_mapping()

    def test_explicit_personal_no_work_maps_existing_reviewable_control_without_years_inference(self):
        for sentence in ('I have no prior work experience.', 'I have no previous work experience.', 'I have never worked.', 'I have not worked before.'):
            with self.subTest(sentence=sentence):
                p = self.profile(sentence+' I want AI evaluation work.')
                self.assertEqual(p['constraints']['hard_constraints'], ['no prior experience'])
                self.assertIsNone(p['experience']['total_years'])
                self.assertEqual(p['experience']['years_by_domain'], {})
                self.assertEqual(p['experience']['specialties'], [])
                self.assertEqual(p['preferences']['target_opportunity_types'], ['AI evaluation'])
                self.assertIs(p['provenance']['field_sources']['constraints.hard_constraints[0]']['explicit'], False)

    def test_unknown_first_job_alone_third_party_domain_limited_and_quoted_history_stay_distinct(self):
        for text in ('I want AI evaluation work.', 'I am looking for my first job.',
                     'My friend has no prior work experience.', 'The applicant says I have no prior work experience.',
                     'I do not have no prior work experience.', 'I have no prior work experience in AI.',
                     'I have no prior work experience, but I have volunteered.',
                     '"I have no prior work experience."', 'I will say I have no prior work experience.',
                     'My friend said: "I moved recently. I have no prior work experience. I prefer remote work."',
                     'The applicant said: \u201cI moved recently. I have no prior work experience.\u201d',
                     "My friend said: 'I moved recently. I have no prior work experience. I prefer remote work.'",
                     'I have never worked "as a physician".',
                     'I have no prior work experience "in AI".',
                     'I have no prior work experience \u201cin AI.\u201d'):
            with self.subTest(text=text):
                self.assertNotIn('no prior experience', self.profile(text)['constraints']['hard_constraints'])
        self.assertIn('no prior experience', self.profile(
            'My friend said: "I have worked before." I have no prior work experience.')['constraints']['hard_constraints'])
