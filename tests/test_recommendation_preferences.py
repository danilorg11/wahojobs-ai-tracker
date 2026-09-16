"""Confirmed manual wishes reach matching; firm workload limits stay explicit."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from scripts import local_product_app as app
from tests.test_confirmed_activity_matching import candidate, v2
from tests.test_typed_match_criteria import opportunity_for_dimension
from wahojobs import authenticated_profile_matches as browser
from wahojobs.persistent_profile_creation import prepare_reviewed_profile_source_bundle
from wahojobs.profiles.canonical import field_sources_for_profile
from wahojobs.profiles.canonical_v2 import (
    add_reviewed_legacy_preference_model, add_user_confirmed_preference_model_v2,
    CanonicalProfileV2Error, validate_canonical_profile_v2,
)
from wahojobs.profiles.preference_model import (
    effective_preference_authority, explicit_hard_workload, confirmed_hard_workload,
    ProfilePreferenceModelError,
)
from wahojobs.profiles.preference_presentation import (
    with_reviewed_preferences, preference_summary, candidate_workload_context,
)
from wahojobs.matching.typed_criteria import (
    match_criteria_v1_from_profile, evaluate_match_criteria_shadow,
    evaluate_primary_preference_admission_v1,
)


def legacy_profile(*, workload='part-time', hard=(), interests=()):
    c = candidate(['review written responses', 'check information against instructions'])
    c['preferences']['employment_types'] = [workload] if workload else []
    c['preferences']['work_preferences'] = [workload] if workload else []
    c['preferences']['target_opportunity_types'] = list(interests)
    c['constraints']['hard_constraints'] = list(hard)
    c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
    return v2(c)


def attached(profile):
    return add_reviewed_legacy_preference_model(profile, source_ordinal_resolver=lambda *_: [1])


def evaluate(profile, *, value='full_time', known=True):
    criteria = match_criteria_v1_from_profile(profile)
    outcome = evaluate_match_criteria_shadow(criteria, opportunity_for_dimension('workload', value, known=known))
    return criteria, outcome.outcomes, evaluate_primary_preference_admission_v1(criteria, outcome.outcomes)


class RecommendationPreferenceTests(unittest.TestCase):
    def test_sealed_manual_confirmation_attaches_reviewed_workload_with_complete_source_binding(self):
        raw = 'I live in Brazil and speak English. I prefer part-time work.'
        draft = app.normalize_identity_free_profile_input(raw, 'short_paragraph', allow_fallbacks=False)
        for workload in ('part-time', 'full-time'):
            with self.subTest(workload=workload):
                fields = app.profile_review_form_fields(draft, 'run', 'R' * 43)
                fields.update(employment_types=workload, work_preferences=workload)
                updates = app.profile_review_updates_from_form({k: [v] for k, v in fields.items()},
                    app.profile_review_language_slots(draft))
                reviewed = app.apply_identity_free_profile_review(draft, updates)
                bundle = prepare_reviewed_profile_source_bundle(reviewed, raw, updates,
                    datetime(2026, 9, 15, tzinfo=timezone.utc))
                result = bundle.build_canonical_v2('prf_' + '0' * 31 + '1')
                model = result['preferences']['preference_model']
                self.assertIn(workload.replace('-', '_'), model['workloads'])
                ref = next(r for r in result['provenance']['field_sources']
                           if r['field_path'] == 'preferences.preference_model.workloads[0]')
                original_refs = [r for r in result['provenance']['field_sources']
                                 if r['field_path'].startswith('preferences.employment_types[')]
                self.assertEqual(ref['source_kind'], original_refs[0]['source_kind'])
                self.assertEqual(ref['source_ordinals'], original_refs[0]['source_ordinals'])
                self.assertTrue(ref['explicit'])
                validate_canonical_profile_v2(result)
                self.assertNotIn('preference_model', draft.to_mapping()['preferences'])

    def test_manual_model_preserves_legacy_free_text_and_strict_writer_mirror_guard(self):
        profile = legacy_profile(interests=['language evaluation', 'a specific personal interest'])
        before = deepcopy(profile)
        result = attached(profile)
        self.assertEqual(profile, before)
        self.assertEqual({k: v for k, v in result['preferences'].items() if k != 'preference_model'}, before['preferences'])
        self.assertEqual(result['preferences']['preference_model']['workloads'], ['part_time'])
        self.assertEqual(result['preferences']['preference_model']['employment_relationships'], [])
        self.assertEqual(result['preferences']['availability'], 'unknown')
        self.assertIn('a specific personal interest', ' '.join(preference_summary(result['preferences'])))
        with self.assertRaisesRegex(CanonicalProfileV2Error, 'preference_legacy_projection_mismatch'):
            add_user_confirmed_preference_model_v2(before, result['preferences']['preference_model'],
                                                   source_ordinal_resolver=lambda *_: [1])

    def test_old_confirmed_workload_reaches_same_consumer_without_profile_mutation(self):
        profile = legacy_profile(interests=['unmapped interest'])
        before = deepcopy(profile)
        model, origin = effective_preference_authority(profile)
        self.assertEqual(origin, 'confirmed_legacy_workload')
        self.assertEqual(model['workloads'], ['part_time'])
        self.assertEqual(model['job_interests'], [])
        self.assertTrue(browser._has_authoritative_preference_model(profile))
        criteria, outcomes, admission = evaluate(profile)
        self.assertEqual(criteria.source_status, 'present')
        self.assertEqual(outcomes[0].outcome, 'fail')
        self.assertEqual(admission.status, 'keep')
        self.assertEqual(profile, before)
        self.assertNotIn('preference_model', profile['preferences'])

    def test_unreviewed_or_unconfirmed_legacy_value_cannot_become_read_authority(self):
        for case in ('reviewed', 'explicit', 'source_kind'):
            profile = legacy_profile()
            if case == 'reviewed':
                profile['provenance']['reviewed'] = False
            else:
                for ref in profile['provenance']['field_sources']:
                    if ref['field_path'].startswith('preferences.'):
                        ref[case] = False if case == 'explicit' else 'parsed_free_text'
            self.assertEqual(effective_preference_authority(profile), (None, 'absent'))
            self.assertEqual(match_criteria_v1_from_profile(profile).source_status, 'absent')

    def test_soft_difference_is_evaluated_not_excluded_and_unknown_hours_stay_unknown(self):
        profile = attached(legacy_profile())
        for value, known, expected in [('full_time', True, 'fail'), ('part_time', True, 'pass'), ('part_time', False, 'unknown')]:
            criteria, outcomes, admission = evaluate(profile, value=value, known=known)
            self.assertEqual(outcomes[0].outcome, expected)
            self.assertEqual(criteria.soft_preference_criteria[0].dimension, 'workload')
            self.assertEqual(admission.status, 'keep')
            self.assertEqual(admission.exclusion_criterion_ids, ())
        guidance = candidate_workload_context(profile, {}, {})
        self.assertIn('part-time', guidance['guidance'])
        self.assertNotIn('hours', guidance['guidance'])
        self.assertEqual(guidance['outcome'], None)

    def test_confirmed_firm_limit_requires_positive_workload_evidence(self):
        profile = attached(legacy_profile(hard=['part-time only']))
        for value, known, expected in [('full_time', True, 'exclude'), ('part_time', False, 'exclude'), ('part_time', True, 'keep')]:
            criteria, outcomes, admission = evaluate(profile, value=value, known=known)
            self.assertEqual(admission.status, expected)
            self.assertEqual(criteria.strict_preference_criteria[0].criterion_id, 'preferences.workloads')
            self.assertEqual(criteria.soft_preference_criteria, ())
            state = candidate_workload_context(profile, {}, {'_workload_preference_outcomes': [o.as_dict() for o in outcomes]})
            self.assertEqual(state['state'], 'supported' if expected == 'keep' else 'hard_conflict' if known else 'hard_unresolved')

    def test_firm_limit_recognizes_exact_confirmed_forms_only(self):
        for text in ('I prefer part-time work', 'I cannot work full-time', 'my friend wants part-time only', 'not part-time only'):
            profile = attached(legacy_profile(hard=[text]))
            self.assertEqual(confirmed_hard_workload(profile), ())
            self.assertEqual(evaluate(profile)[2].status, 'keep')
        profile = legacy_profile(hard=['part-time only'])
        for ref in profile['provenance']['field_sources']:
            if ref['field_path'].startswith('constraints.hard_constraints['):
                ref['explicit'] = False
        self.assertEqual(confirmed_hard_workload(profile), ())
        self.assertEqual(explicit_hard_workload(['ONLY PART-TIME WORK']), ('part_time',))
        with self.assertRaisesRegex(ProfilePreferenceModelError, 'conflicting_hard_workload_constraints'):
            explicit_hard_workload(['part-time only', 'full-time only'])

    def test_other_strict_preferences_are_not_relaxed_by_soft_workload_policy(self):
        profile = attached(legacy_profile())
        model = deepcopy(profile['preferences']['preference_model'])
        model['compensation_expectations'] = [dict(minimum_kind='strict', amount='15', currency='USD', period='hour')]
        profile = with_reviewed_preferences(profile, model)
        criteria, outcomes, admission = evaluate(profile)
        self.assertEqual(admission.status, 'exclude')
        self.assertNotIn('preferences.workloads', admission.exclusion_criterion_ids)
        self.assertTrue(any('compensation' in value for value in admission.exclusion_criterion_ids))

    def test_typed_change_preserves_independent_interests_constraints_and_provenance(self):
        profile = attached(legacy_profile(interests=['language evaluation', 'my exact personal interest']))
        before = deepcopy(profile)
        model = deepcopy(profile['preferences']['preference_model'])
        model['workloads'] = ['full_time']
        result = with_reviewed_preferences(profile, model)
        self.assertEqual(result['preferences']['employment_types'], ['full-time'])
        self.assertEqual(result['preferences']['work_preferences'], ['full-time'])
        self.assertEqual(result['preferences']['target_opportunity_types'], before['preferences']['target_opportunity_types'])
        self.assertEqual(result['constraints'], before['constraints'])
        prior_refs = [r for r in before['provenance']['field_sources'] if 'target_opportunity_types' in r['field_path']]
        self.assertEqual(prior_refs, [r for r in result['provenance']['field_sources'] if 'target_opportunity_types' in r['field_path']])
        self.assertEqual(profile, before)
        self.assertIn('full-time', candidate_workload_context(result, {}, {})['guidance'])

    def test_existing_legacy_workload_shadow_follows_explicit_typed_change(self):
        profile = legacy_profile()
        profile['preferences']['availability'] = 'part-time'
        profile = attached(validate_canonical_profile_v2(profile))
        model = deepcopy(profile['preferences']['preference_model'])
        model['workloads'] = ['full_time']
        result = with_reviewed_preferences(profile, model)
        self.assertEqual(result['preferences']['availability'], 'full-time')
        self.assertNotIn('part-time', ' '.join(preference_summary(result['preferences'])))

    def test_guidance_severity_comes_from_actual_evaluated_outcome(self):
        profile = attached(legacy_profile())
        _, outcomes, _ = evaluate(profile)
        state = candidate_workload_context(profile, {}, {'_workload_preference_outcomes': [o.as_dict() for o in outcomes]})
        self.assertEqual(state['state'], 'soft_difference')
        self.assertEqual(state['outcome'], 'fail')
        self.assertIn('this posting lists full-time', state['guidance'])
        self.assertNotIn('contractor', state['guidance'])
        partial = {'preferences': {'workloads': ['part_time']}, 'experience': {}}
        self.assertIsNone(candidate_workload_context(partial, {}, {})['guidance'])

    def test_actual_authenticated_consumer_records_legacy_origin_and_keeps_soft_mismatch(self):
        from tests.test_accepted_task_matching import AcceptedTaskMatchingTests
        from tests.test_transferable_task_matching import ENTRY
        base = AcceptedTaskMatchingTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        base.role('Generalist', 'Remote')
        base.source(ENTRY)
        base.f.update_inventory("UPDATE jobs SET commitment='Full-time'")
        base.f.profile = legacy_profile()
        original = deepcopy(base.f.profile)
        _, _, context = base.current()
        diagnostic = context['_typed_preference_enforcement']
        self.assertEqual(diagnostic['preference_authority_origin'], 'confirmed_legacy_workload')
        match = base.match(context)
        self.assertEqual(match['_workload_preference_outcomes'][0]['outcome'], 'fail')
        shown = browser._primary_presentation_matches(context) + browser._conditional_presentation_matches(context)
        self.assertIn(match['job_id'], [m['job_id'] for m in shown])
        self.assertEqual(base.f.profile, original)

    def test_rendered_remote_control_remains_draft_then_confirms_independently_of_typed_model(self):
        from tests import test_private_beta_preferences as support
        helper = support.PrivateBetaPreferenceTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        before = helper.t.current()
        self.assertFalse(before['preferences']['remote'])
        for value in ('1', None):
            page, form = helper.editor()
            self.assertIn(b'type="checkbox" name="remote"', page.body)
            self.assertNotIn(b"type='hidden' name='remote'", page.body)
            unchanged = helper.t.current()
            response = helper.submit(form, {'remote': value})
            self.assertEqual(response.status, 303)
            review = helper.t.get(helper.f._response_header(response, 'Location'))
            self.assertEqual(helper.t.current(), unchanged)
            self.assertEqual(helper.t.apply(helper.f._form(review, 'draft', 'review_token')).status, 200)
            after = helper.t.current()
            self.assertEqual(after['preferences']['remote'], value == '1')
            self.assertEqual('remote' in after['preferences']['work_preferences'], value == '1')
            self.assertEqual(after['preferences']['preference_model'], before['preferences']['preference_model'])
            ref = next(r for r in after['provenance']['field_sources'] if r['field_path']=='preferences.remote')
            self.assertEqual(ref['source_kind'], 'user_correction')
            self.assertTrue(ref['explicit'])

    def test_manual_remote_control_and_typed_workload_focus_are_visible_controls(self):
        from wahojobs.profiles.correction_editor import render_editor
        from wahojobs.profiles.canonical_v2 import project_v2_to_review_v1
        from wahojobs.persistent_profiles import IdentityFreeCanonicalProfileV1
        profile = attached(legacy_profile())
        canonical = IdentityFreeCanonicalProfileV1.from_mapping(project_v2_to_review_v1(profile))
        fields = app.profile_review_form_fields(canonical, 'run', 'R' * 43)
        kwargs = dict(action='/find-matches', back_url='/find-matches',
                      education=canonical['education'], form_defaults=fields, focus='preferences')
        manual = render_editor(app, canonical, 'run', 'R' * 43, manual_draft=True, **kwargs)
        self.assertIn('type="checkbox" name="remote"', manual)
        typed = render_editor(app, canonical, 'run', 'R' * 43,
                              preference_model=profile['preferences']['preference_model'], **kwargs)
        self.assertIn("data-focus='beta_preference_workloads'", typed)
        self.assertIn("type='checkbox' name='beta_preference_workloads'", typed)
        self.assertNotIn("<select id='availability'", typed)

    def legacy_correction_fixture(self):
        from tests.test_candidate_correction_editor import CandidateCorrectionEditorTests
        helper = CandidateCorrectionEditorTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        profile = legacy_profile(interests=['my exact personal interest'])
        profile['identity']['profile_id'] = helper.f.created.profile_id
        helper.f._install_current_v2(profile, idempotency_key='reviewed-legacy-preference-fixture')
        return helper

    def test_legacy_noop_and_unrelated_correction_preserve_preference_authority_and_history(self):
        from contextlib import closing
        helper = self.legacy_correction_fixture()
        current = lambda: helper.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        before = current()
        counts = helper.f._profile_counts()
        with closing(helper.f._connection()) as connection:
            history = dict(connection.execute('SELECT revision_id, structured_profile_json FROM product_profile_revisions'))
        _, page = helper.editor()
        form = helper.f._form(page, 'edit_run_id')
        fields = helper.f._set_form_field(form['fields'], 'credentials_confirmed', '1')
        response, _ = helper.f._post_form(helper.browser, form['action'], fields)
        self.assertEqual(response.status, 303)
        review = helper.get(helper.f._response_header(response, 'Location'))
        self.assertIn(b'No profile details have changed', review.body)
        self.assertEqual(current(), before)
        self.assertEqual(helper.f._profile_counts(), counts)

        offer = helper.f._browser_apply_offer(helper.browser, changes=(('city', 'Recife'),))
        self.assertEqual(current(), before)
        response, _ = helper.f._post_form(helper.browser, offer['action'], offer['fields'])
        self.assertEqual(response.status, 303)
        after = current()
        self.assertEqual(after['location']['city'], 'Recife')
        self.assertEqual(after['preferences'], before['preferences'])
        self.assertNotIn('preference_model', after['preferences'])
        refs = lambda p: [r for r in p['provenance']['field_sources'] if r['field_path'].startswith('preferences.')]
        self.assertEqual(refs(after), refs(before))
        self.assertEqual(effective_preference_authority(after)[1], 'confirmed_legacy_workload')
        self.assertEqual(helper.f._profile_counts()[1], counts[1] + 1)
        with closing(helper.f._connection()) as connection:
            retained = dict(connection.execute('SELECT revision_id, structured_profile_json FROM product_profile_revisions'))
        self.assertEqual({key: retained[key] for key in history}, history)

    def test_explicit_legacy_preference_edit_attaches_typed_model_only_after_confirmation(self):
        helper = self.legacy_correction_fixture()
        current = lambda: helper.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        before = current()
        counts = helper.f._profile_counts()
        _, page = helper.editor()
        form = helper.f._form(page, 'edit_run_id')
        fields = helper.f._set_form_field(form['fields'], 'credentials_confirmed', '1')
        self.assertIn('employment_types', dict(fields))
        self.assertNotIn('work_preferences', dict(fields))
        fields = helper.f._set_form_field(fields, 'employment_types', 'full-time')
        # The legacy work_preferences mirror is server-derived, not a generated
        # browser control. Supplying it is still rejected by the closed form.
        rejected, _ = helper.f._post_form(helper.browser, form['action'],
            helper.f._set_form_field(fields, 'work_preferences', 'full-time'))
        self.assertEqual(rejected.status, 400)
        self.assertEqual(current(), before)
        self.assertEqual(helper.f._profile_counts(), counts)
        response, _ = helper.f._post_form(helper.browser, form['action'], fields)
        self.assertEqual(response.status, 303, 'The generated workload control must reach review')
        review = helper.get(helper.f._response_header(response, 'Location'))
        self.assertIn(b'Changes awaiting confirmation', review.body)
        self.assertIn(b'Full-time', review.body)
        self.assertEqual(current(), before)
        self.assertEqual(helper.f._profile_counts(), counts)
        confirm = helper.f._form(review, 'draft', 'review_token')
        response, _ = helper.f._post_form(helper.browser, confirm['action'],
            helper.f._set_form_field(confirm['fields'], 'confirmed', '1'))
        self.assertEqual(response.status, 200)
        self.assertEqual(current(), before)
        offer = helper.f._form(response, 'artifact', 'csrf')
        response, _ = helper.f._post_form(helper.browser, offer['action'], offer['fields'])
        self.assertEqual(response.status, 303)
        after = current()
        self.assertEqual(after['preferences']['preference_model']['workloads'], ['full_time'])
        self.assertEqual(after['preferences']['target_opportunity_types'], before['preferences']['target_opportunity_types'])
        self.assertEqual(after['experience'], before['experience'])
        ref = next(r for r in after['provenance']['field_sources']
                   if r['field_path'] == 'preferences.preference_model.workloads[0]')
        self.assertEqual(ref['source_kind'], 'user_correction')
        self.assertEqual(ref['source_ordinals'], [2])
        self.assertTrue(ref['explicit'])


if __name__ == '__main__':
    unittest.main()
