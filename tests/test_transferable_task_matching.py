"""Owner-authorized transferable relevance: synthetic controls + preserved body."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_accepted_task_matching as accepted_support
from tests.test_confirmed_activity_matching import candidate, v2
from wahojobs import authenticated_profile_matches as browser
from wahojobs.profiles.canonical import canonical_to_matcher_profile, field_sources_for_profile
from wahojobs.matching.transferable_tasks import confirmed_activities, source_scope


ENTRY = ('About the role\n\nThis is an entry-level role.\n\nResponsibilities\n\n'
         'Review and evaluate AI-generated content.\n\nRequirements\n\n'
         'Follow written project guidelines.')


class TransferableTaskMatchingTests(unittest.TestCase):
    def setUp(self):
        self.base = accepted_support.AcceptedTaskMatchingTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.role('Generalist', 'Remote')
        self.install(['review written responses', 'check information against instructions'])

    def install(self, activities, **changes):
        c = candidate(activities)
        for language in c['languages']:
            language.update(proficiency='fluent', proficiency_explicit=True)
        for key, value in changes.items():
            c[key] = value
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(c)
        return c

    def current(self, body=ENTRY):
        self.base.source(body)
        page, run, context = self.base.current()
        return page, run, context, self.base.match(context)

    def shown(self, context):
        return browser._primary_presentation_matches(context) + browser._conditional_presentation_matches(context)

    def test_preserved_generalist_gets_related_activity_and_remaining_conditions_without_score_change(self):
        body = (Path(__file__).parent/'fixtures/source_requirement_fidelity/accepted-generalist.txt').read_text(encoding='utf-8')
        self.base.source(body)
        with patch('wahojobs.matching.transferable_tasks.confirmed_activities', return_value=[]):
            _, _, before = self.base.current()
        _, _, context = self.base.current()
        prior, match = self.base.match(before), self.base.match(context)
        self.assertEqual(match['score'], prior['score'])
        self.assertEqual(match['score_components'], prior['score_components'])
        self.assertEqual(browser._primary_presentation_matches(context), [])
        self.assertEqual([m['job_id'] for m in browser._conditional_presentation_matches(context)], [7003])
        fit = match['accepted_task_fit']
        self.assertEqual(fit['basis'], 'transferable_activity')
        self.assertEqual({f['text'] for f in fit['profile_facts']}, {'review written responses', 'check information against instructions'})
        self.assertTrue(fit['scope_evidence'])
        self.assertTrue(all(l['support_kind']=='transferable_activity' for l in fit['task_links']))
        self.assertTrue(all(ref['source_kind']=='user_confirmation' for f in fit['profile_facts'] for ref in f['provenance']))
        self.assertIn('Transferable activity for entry-level tasks', match['affirmative_fit']['satisfied_groups'])
        self.assertNotIn('AI evaluation or annotation tasks', match['affirmative_fit']['satisfied_groups'])
        self.assertTrue(match['source_qualification_comparisons'])

    def test_entry_source_without_ai_waiver_can_support_related_confirmed_activity(self):
        _, _, context, match = self.current()
        self.assertTrue(self.shown(context))
        self.assertEqual(match['accepted_task_fit']['basis'], 'transferable_activity')
        self.assertTrue(match['conditional_task_fit'])

    def test_no_ai_waiver_or_title_alone_cannot_prove_entry_scope(self):
        self.base.role('Entry-level AI Generalist')
        for body in ('Responsibilities\n\nReview AI-generated content.\n\nRequirements\n\nNo prior AI experience required.',
                     'About us\n\nOur entry-level employees review AI-generated content.'):
            with self.subTest(body=body):
                _, _, context, match = self.current(body)
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))

    def test_unrelated_interest_language_and_tool_mentions_are_not_activities(self):
        for activities in ([], ['organize spreadsheet records'], ['answer customer questions'],
                           ['Interested in reviewing responses'], ['No reviewing written responses'],
                           ['English fluency'], ['Python'], ['Alex reviews written responses'],
                           ['check the printer, then send information to IT']):
            with self.subTest(activities=activities):
                self.install(activities)
                _, _, context, match = self.current()
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))

    def test_unconfirmed_activity_and_interest_do_not_create_evidence_or_ai_signal(self):
        c = candidate(['review written responses'])
        original = deepcopy(c)
        projected = canonical_to_matcher_profile(c, include_task_evidence=True)
        self.assertTrue(projected['confirmed_transferable_activity_evidence'])
        self.assertNotIn('confirmed_ai_work_evidence', projected)
        self.assertEqual(len(projected['signals']), 1)
        self.assertEqual(c, original)
        c['provenance']['reviewed'] = False
        self.assertFalse(confirmed_activities(c))
        c['provenance']['reviewed'] = True
        c['provenance']['field_sources']['experience.specialties[0]']['explicit'] = False
        self.assertFalse(confirmed_activities(c))

    def test_entry_scope_cannot_waive_specialist_ai_or_tool_prerequisites(self):
        for condition in ('PhD in Biology required', 'Licensed physician required',
                          '5 years of relevant professional experience in customer support required',
                          'Prior AI evaluation experience required', 'Previous AI annotation work is required.',
                          'Experience with Python required'):
            with self.subTest(condition=condition):
                _, _, context, match = self.current(ENTRY + '\n\n' + condition)
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))

    def test_preferred_specialist_background_is_not_a_mandatory_requirement(self):
        for preferred in ('PhD in Biology preferred', 'Previous AI annotation work preferred'):
            with self.subTest(preferred=preferred):
                _, _, context, match = self.current(ENTRY+'\n\nPreferred Qualifications\n\n'+preferred)
                self.assertTrue(match['accepted_task_fit'])
                self.assertTrue(self.shown(context))

    def test_native_french_requirement_still_rejects_and_unknown_is_not_explicit_negative(self):
        self.base.role('French Generalist', 'Remote')
        _, _, context, match = self.current(ENTRY+'\n\nNative French required')
        self.assertTrue(match['accepted_task_fit'])
        self.assertFalse(self.shown(context))
        self.assertFalse(match['eligible_for_personalized'])
        self.assertEqual(match['source_language_checks'][0]['status'], 'unresolved')

    def test_explicit_foreign_country_restriction_still_rejects(self):
        _, _, context, match = self.current(ENTRY+'\n\nMust be based in Canada')
        self.assertTrue(match['accepted_task_fit'])
        self.assertFalse(self.shown(context))
        self.assertEqual(match['location_eligibility_status'], 'incompatible')

    def test_remote_does_not_establish_worldwide_permission(self):
        _, _, context, match = self.current()
        self.assertTrue(self.shown(context))
        self.assertEqual(match['location_eligibility_status'], 'unknown')

    def test_source_reference_mismatch_cannot_supply_transfer_fit(self):
        self.base.source(ENTRY, source_url='https://example.test/another-posting')
        _, _, context = self.base.current()
        self.assertIsNone(self.base.match(context)['accepted_task_fit'])
        self.assertFalse(self.shown(context))

    def test_exact_detail_reuses_the_same_derived_support_and_bound_source(self):
        from wahojobs import authenticated_variant_details as details
        from wahojobs.authenticated_variant_details import variant_detail_url
        _, run, context, match = self.current()
        with patch.object(details, 'prepare_variant_notice', wraps=details.prepare_variant_notice) as notice:
            page = self.base.f.get(variant_detail_url(match, run_id=run.match_run_id))
        self.assertEqual(page.status, 200)
        local = notice.call_args.args[0]['_authenticated_local_checks']['match']
        self.assertEqual(local['accepted_task_fit'], match['accepted_task_fit'])
        self.assertEqual(local['score'], match['score'])
        self.assertIn(b'This task overlap does not establish prior professional AI work', page.body)

    def test_about_role_prerequisite_is_not_hidden_by_positive_entry_scope(self):
        for scope in ('This is an entry-level role.\n\nLicensed physician required.',
                      'No specialized background or prior AI experience required. Licensed physician required.',
                      'This is an entry-level role for licensed physicians.',
                      'This is an entry-level role for experienced biologists.'):
            with self.subTest(scope=scope):
                body = ('About the role\n\n'+scope+'\n\nResponsibilities\n\nReview AI-generated content.\n\n'
                        'Requirements\n\nFollow project guidelines.')
                _, _, context, match = self.current(body)
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))

    def test_negated_historical_and_future_scope_are_not_current_entry_proof(self):
        for scope in ('This is no longer an entry-level role.', 'We hope to offer an entry-level role in future.',
                      'This is not an entry-level role.', 'We previously offered an entry-level role.',
                      'Our other team offers an entry-level role.',
                      'The company also advertises an entry-level role.'):
            with self.subTest(scope=scope):
                _, _, context, match = self.current(ENTRY.replace('This is an entry-level role.', scope))
                self.assertIsNone(match['accepted_task_fit'])
                self.assertFalse(self.shown(context))

    def test_all_scope_branches_preserve_restrictive_continuations(self):
        for start in ('This role is open to beginners', 'No specialized background is required',
                      'This is an entry-level role'):
            for tail in (' who are experienced biologists.', ' in our other program.',
                         ' for licensed physicians.', ' — for licensed physicians only.',
                         '; for licensed physicians only.', '. For licensed physicians only.',
                         ', but only for licensed physicians.', '\nfor licensed physicians only.',
                         '\n\nFor licensed physicians only.',
                         '\n\n- For licensed physicians only.', '\n\n1. For licensed physicians only.',
                         ' — just a medical license.', ' — just experience in molecular biology.'):
                with self.subTest(scope=start+tail):
                    _, _, context, match = self.current(ENTRY.replace('This is an entry-level role.', start+tail))
                    self.assertIsNone(match['accepted_task_fit'])
                    self.assertFalse(self.shown(context))
            for prefix in ('This role is for experienced biologists. ',
                           'This role is for licensed physicians.\n\n',
                           'This role is limited to experienced biologists.\n\n',
                           'This position is intended for licensed physicians.\n\n',
                           'This is a role for experienced biologists.\n\n',
                           'For licensed physicians only.\n\n',
                           'Only experienced biologists.\n\n',
                           '- For licensed physicians only.\n\n',
                           '1. For licensed physicians only.\n\n'):
                with self.subTest(scope=prefix+start):
                    _, _, context, match = self.current(ENTRY.replace('This is an entry-level role.', prefix+start+'.'))
                    self.assertIsNone(match['accepted_task_fit'])
                    self.assertFalse(self.shown(context))

    def test_complete_scope_statements_allow_only_bounded_generic_quality_continuations(self):
        for scope in ('Open to beginners.', 'This role is open to first-time workers.',
                      'No specialized background is required.',
                      'This is a remote, flexible role. Open to beginners.',
                      'This is a freelance contract opportunity. No specialized background is required.',
                      'No specialized background is required — just attention to detail and a willingness to learn.',
                      'This is an entry-level role — just curiosity, patience and clear communication.'):
            with self.subTest(scope=scope):
                _, _, context, match = self.current(ENTRY.replace('This is an entry-level role.', scope))
                self.assertTrue(match['accepted_task_fit'])
                self.assertTrue(self.shown(context))

    def test_missing_degree_is_conditional_but_explicit_negative_degree_rejects(self):
        body = ENTRY + "\n\nBachelor's in Humanities."
        _, _, context, match = self.current(body)
        self.assertTrue(self.shown(context))
        education = next(r for r in match['source_qualification_comparisons'] if r['kind']=='education')
        self.assertEqual(education['status'], 'not_established')
        c = candidate(['review written responses'])
        c['education']['education_level'] = 'no_degree'
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(c)
        _, _, context, match = self.current(body)
        self.assertFalse(self.shown(context))
        self.assertEqual(match['affirmative_fit_status'], 'conflicting')
        self.assertEqual(next(r for r in match['source_qualification_comparisons'] if r['kind']=='education')['status'], 'contradicted')

    def test_independent_language_level_conflict_is_not_transferable_task_support(self):
        c = candidate(['review written responses'])
        for language in c['languages']:
            if language['language']=='English':
                language.update(proficiency='basic', proficiency_explicit=True)
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(c)
        _, _, context, match = self.current(ENTRY+'\n\nNative English required')
        self.assertTrue(match['accepted_task_fit'])
        self.assertFalse(self.shown(context))
        self.assertEqual(match['source_language_checks'][0]['status'], 'contradicted')

    def test_workload_unknown_and_unspecified_conflict_remain_visible_but_required_conflict_rejects(self):
        body = ENTRY+'\n\nEngagement\n\nCommitment: 20 hours/week'
        _, _, context, match = self.current(body)
        self.assertTrue(self.shown(context))
        self.assertEqual(next(r for r in match['source_qualification_comparisons'] if r['kind']=='workload')['status'], 'not_established')
        c = candidate(['review written responses'])
        c['preferences']['availability'] = 'unavailable'
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(c)
        _, _, context, match = self.current(body)
        self.assertTrue(self.shown(context))
        workload = next(r for r in match['source_qualification_comparisons'] if r['kind']=='workload')
        self.assertEqual(workload['status'], 'contradicted')
        self.assertEqual(workload['modality'], 'unspecified')
        _, _, context, match = self.current(ENTRY+'\n\nCommitment: 20 hours/week')
        self.assertFalse(self.shown(context))
        self.assertEqual(next(r for r in match['source_qualification_comparisons'] if r['kind']=='workload')['status'], 'contradicted')

    def test_strict_compensation_preference_runs_after_transfer_relevance(self):
        from tests.test_profile_preference_model import with_preference_model
        from wahojobs.profiles.preference_model import empty_profile_preferences_v2
        preference = empty_profile_preferences_v2()
        preference['compensation_expectations'] = [dict(minimum_kind='strict', amount='100', currency='USD', period='hour')]
        self.base.f.profile = with_preference_model(self.base.f.profile, preference)
        from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2
        validate_canonical_profile_v2(self.base.f.profile)
        _, _, context, match = self.current(ENTRY+'\n\nEngagement\n\nPay: USD 25 per hour')
        self.assertTrue(match['accepted_task_fit'])
        self.assertFalse(self.shown(context))
        evaluation = next(r for r in context['_typed_preference_enforcement']['evaluations']
                          if r['opportunity_reference']=='canonical:7002')
        self.assertEqual(evaluation['admission']['status'], 'exclude')
        compensation = [r for r in evaluation['outcomes'] if r['criterion_id'].startswith('preferences.compensation')]
        self.assertTrue(compensation)
        self.assertTrue(all(r['outcome']=='unknown' for r in compensation))

    def test_confirmed_typed_workload_is_evaluated_independently_of_activity_support(self):
        from tests.test_profile_preference_model import with_preference_model
        from wahojobs.profiles.preference_model import empty_profile_preferences_v2
        preference = empty_profile_preferences_v2()
        preference['workloads'] = ['part_time']
        self.base.f.profile = with_preference_model(self.base.f.profile, preference)
        for commitment, shown, outcome in (('Part-time', True, 'pass'), ('Full-time', False, 'fail')):
            with self.subTest(commitment=commitment):
                self.base.f.update_inventory('UPDATE jobs SET commitment=? WHERE id=7003', (commitment,))
                _, _, context, match = self.current()
                self.assertTrue(match['accepted_task_fit'])
                self.assertEqual(bool(self.shown(context)), shown)
                evaluation = next(r for r in context['_typed_preference_enforcement']['evaluations']
                                  if r['opportunity_reference']=='canonical:7002')
                workload = next(r for r in evaluation['outcomes'] if r['criterion_id']=='preferences.workloads')
                self.assertEqual(workload['outcome'], outcome)
                self.assertEqual(evaluation['admission']['status'], 'keep' if shown else 'exclude')

    def test_specialist_title_and_no_ai_waiver_keep_existing_background_guardrails(self):
        self.base.role('Biology Expert', 'Remote')
        _, _, context, match = self.current(ENTRY+'\n\nNo prior AI experience required')
        self.assertFalse(self.shown(context))
        self.assertNotEqual(match['affirmative_fit_status'], 'supported')
        self.assertGreater(match['score_components']['quality_gate_penalty'], 0)

    def test_stale_projected_activity_cannot_survive_durable_profile_binding(self):
        projected = confirmed_activities(candidate(['review written responses']))
        for changed in ('text', 'provenance'):
            with self.subTest(changed=changed):
                self.install(['organize spreadsheet records' if changed=='text' else 'review written responses'])
                if changed=='provenance':
                    for ref in self.base.f.profile['provenance']['field_sources']:
                        if ref['field_path']=='experience.specialties[0]':
                            ref['source_kind'] = 'parsed_free_text'
                with patch('wahojobs.matching.transferable_tasks.confirmed_activities', return_value=projected):
                    _, _, context, match = self.current()
                self.assertFalse(self.shown(context))
                self.assertIsNone(match['accepted_task_fit'])

    def test_source_scope_does_not_supply_or_reassign_relevant_domain_duration(self):
        from wahojobs import authenticated_source_detail
        from wahojobs.authenticated_variant_details import variant_detail_url
        c = candidate(['review written responses'])
        c['experience']['years_by_domain'] = {'customer support': 2}
        c['experience']['total_years'] = 12
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.base.f.profile = v2(c)
        _, run, context, match = self.current(ENTRY+'\n\n5 years of relevant professional experience in customer support')
        self.assertFalse(self.shown(context))
        self.assertIsNone(match['accepted_task_fit'])
        recorded = []
        real_prepare = authenticated_source_detail.prepare_detail_display
        def prepare(*args, **kwargs):
            result = real_prepare(*args, **kwargs)
            recorded.append(result)
            return result
        with patch.object(authenticated_source_detail, 'prepare_detail_display', side_effect=prepare):
            page = self.base.f.get(variant_detail_url(match, run_id=run.match_run_id))
        self.assertEqual(page.status, 200)
        packet = recorded[-1]
        duration = next(r for r in packet['comparisons'] if r['kind']=='professional_background')
        self.assertEqual(duration['status'], 'contradicted')
        self.assertEqual(self.base.f.profile['experience']['total_years'], 12)
        self.assertEqual(self.base.f.profile['experience']['years_by_domain'], [{'domain':'customer support','years':2}])


if __name__ == '__main__':
    unittest.main()
