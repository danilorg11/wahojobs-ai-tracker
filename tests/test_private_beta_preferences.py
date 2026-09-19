"""Offline rendered-form / owner-bound persistence regressions for beta UX."""
from copy import deepcopy
import unittest
from types import SimpleNamespace

from tests import test_persistent_profile_corrections as support
from tests.test_canonical_profile_v2 import load_cases, ordinal_resolver
from tests import test_profile_review_transfer as transfer
from wahojobs.profiles.canonical import field_sources_for_profile
from wahojobs.profiles.canonical_v2 import convert_v1_to_v2, add_user_confirmed_preference_model_v2
from wahojobs.profiles.preference_model import empty_profile_preferences_v2, preference_model_to_legacy_preferences
from wahojobs.profiles.preference_presentation import preference_summary
from wahojobs.persistent_profiles_application import _build_profile_view


class PrivateBetaPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.t = transfer.ProfileReviewTransferTests()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.f = self.t.f
        model = empty_profile_preferences_v2()
        model['workloads'] = ['part_time']
        model['accepted_phone_voice_modes'] = ['non_phone']
        model['schedule']['working_days'] = ['weekdays']
        model['compensation_expectations'] = [dict(minimum_kind='strict', amount='15', currency='USD', period='hour')]
        v1 = deepcopy(load_cases()[0]['expected_canonical_profile'])
        v1['preferences'] = preference_model_to_legacy_preferences(model)
        v1['provenance']['field_sources'] = field_sources_for_profile(v1, 'user_confirmation', explicit=True)
        base = convert_v1_to_v2(v1, persistent_profile_id=self.f.created.profile_id, source_ordinal_resolver=ordinal_resolver)
        typed = add_user_confirmed_preference_model_v2(base, model, source_ordinal_resolver=ordinal_resolver)
        self.f._install_current_v2(typed, idempotency_key='beta-preferences-fixture')

    def editor(self):
        page, _ = self.t.review()
        target = next(link for link in self.f._markup(page).links if 'correction=edit' in link)
        page = self.t.get(target)
        return page, self.f._form(page, 'edit_run_id')

    def submit(self, form, changes):
        fields = self.f._set_form_field(form['fields'], 'credentials_confirmed', '1')
        for key, value in changes.items():
            fields = self.f._set_form_field(fields, key, value)
        return self.f._post_form(self.t.browser, form['action'], fields)[0]

    def test_summary_shows_authoritative_preferences_pay_and_matching_limits(self):
        current = self.t.current()
        before = deepcopy(current)
        rows = preference_summary(current['preferences'])
        self.assertTrue(any('Part-time' in row for row in rows))
        self.assertIn('Strict minimum: USD 15/hour', rows)
        view = _build_profile_view(SimpleNamespace(lifecycle_status='active', revision_number=2, updated_at='2026-09-15T00:00:00Z'), current)
        self.assertIn('Strict minimum: USD 15/hour', next(g.values for g in view.field_groups if g.label == 'Work preferences'))
        page, _ = self.editor()
        self.assertIn(b'Advertised pay is not guaranteed earnings', page.body)
        self.assertIn(b"type='hidden' name='phone_preference'", page.body)
        self.assertEqual(current, before)

    def test_summary_distinguishes_self_reported_eligibility_domain_years_and_unfinished_study(self):
        from wahojobs.profiles.correction_editor import summary_sections
        current = self.t.current()
        current['location']['work_authorization'] = 'unknown'
        current['location']['eligible_countries'] = ['Brazil']
        current['experience']['total_years'] = 9
        current['experience']['years_by_domain'] = [dict(domain='Biology', years=2)]
        current['education']['entries'] = [dict(kind='doctorate', qualification='PhD', field='Biology',
            institution='Synthetic university', status='in_progress', completion_year=None)]
        view = _build_profile_view(SimpleNamespace(lifecycle_status='active', revision_number=2, updated_at='2026-09-15T00:00:00Z'), current)
        values = ' '.join(v for group in view.field_groups for v in group.values)
        self.assertIn('Eligible country (self-reported): Brazil', values)
        self.assertNotIn('Permission to work:', values)
        self.assertIn('Total experience: 9 years', values)
        self.assertIn('Experience in Biology: 2 years', values)
        self.assertIn('In progress', values)
        review = summary_sections(current)
        self.assertIn('Total career experience: 9 years', review)
        self.assertIn('Experience in Biology: 2 years', review)
        self.assertIn('In progress', review)

    def test_pay_change_preserves_supported_independent_remote_and_start_preferences(self):
        from wahojobs.profiles.preference_presentation import with_reviewed_preferences
        from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2
        current = self.t.current()
        current['preferences']['remote'] = True
        current['preferences']['availability'] = 'immediate'
        current = validate_canonical_profile_v2(current)
        model = deepcopy(current['preferences']['preference_model'])
        model['compensation_expectations'][0]['amount'] = '18'
        after = with_reviewed_preferences(current, model)
        self.assertTrue(after['preferences']['remote'])
        self.assertEqual(after['preferences']['availability'], 'immediate')
        self.assertIn('remote', after['preferences']['work_preferences'])
        self.assertEqual(after['preferences']['preference_model']['compensation_expectations'][0]['amount'], '18')
        self.assertNotIn('Remote work preferred', preference_summary(after['preferences']))
        self.assertIn('Workload or start preference: immediate', preference_summary(after['preferences']))

    def test_rendered_typed_preferences_remain_draft_then_apply_exactly_once(self):
        before = self.t.current()
        _, form = self.editor()
        result = self.submit(form, {'beta_pay_0_amount':'18', 'beta_preference_workloads':'full_time'})
        self.assertEqual(result.status, 303)
        page = self.t.get(self.f._response_header(result, 'Location'))
        self.assertIn(b'Strict minimum: USD 18/hour', page.body)
        self.assertEqual(self.t.current(), before)
        confirm = self.f._form(page, 'draft', 'review_token')
        self.assertEqual(self.t.apply(confirm).status, 200)
        after = self.t.current()
        model = after['preferences']['preference_model']
        self.assertEqual(model['workloads'], ['full_time'])
        self.assertEqual(model['compensation_expectations'][0]['amount'], '18')
        self.assertEqual({k: after['preferences'][k] for k in preference_model_to_legacy_preferences(model)}, preference_model_to_legacy_preferences(model))
        self.assertEqual(after['identity'], before['identity'])
        self.assertEqual(after['languages'], before['languages'])
        count = self.f._profile_counts()
        self.t.apply(confirm)
        self.assertEqual(self.f._profile_counts(), count)

    def test_invalid_pay_keeps_draft_editable_and_does_not_write(self):
        _, form = self.editor()
        before = self.f._profile_counts()
        result = self.submit(form, {'beta_pay_0_amount':'invalid', 'city':'Retained Example City'})
        self.assertEqual(result.status, 400)
        self.assertIn(b'Review your work preferences', result.body)
        self.assertIn(b'Retained Example City', result.body)
        self.assertIn(b"value='invalid'", result.body)
        self.assertEqual(self.f._profile_counts(), before)

    def test_empty_choice_clears_dimension_and_other_preferences_survive(self):
        _, form = self.editor()
        result = self.submit(form, {'beta_preference_workloads':None, 'beta_pay_0_amount':'', 'beta_pay_0_currency':''})
        self.assertEqual(result.status, 303)
        page = self.t.get(self.f._response_header(result, 'Location'))
        self.assertEqual(self.t.apply(self.f._form(page, 'draft', 'review_token')).status, 200)
        model = self.t.current()['preferences']['preference_model']
        self.assertEqual(model['workloads'], [])
        self.assertEqual(model['compensation_expectations'], [])
        self.assertEqual(model['accepted_phone_voice_modes'], ['non_phone'])
        self.assertEqual(model['schedule']['working_days'], ['weekdays'])

    def test_legacy_shadow_cannot_override_typed_model(self):
        _, form = self.editor()
        result = self.submit(form, {'phone_preference':'phone preferred', 'availability':'full-time'})
        self.assertEqual(result.status, 303)
        page = self.t.get(self.f._response_header(result, 'Location'))
        self.assertIn(b'No changes to save', page.body)
        self.assertFalse(self.f._markup(page).forms)
        self.assertEqual(self.t.current()['preferences']['preference_model']['accepted_phone_voice_modes'], ['non_phone'])

    def test_duplicate_or_unknown_preference_is_rejected_without_write(self):
        _, form = self.editor()
        before = self.f._profile_counts()
        for extra in [('beta_preference_workloads', 'part_time'), ('beta_preference_secret', '1')]:
            fields = self.f._set_form_field(form['fields'], 'credentials_confirmed', '1') + [extra]
            result = self.f._post_form(self.t.browser, form['action'], fields)[0]
            self.assertEqual(result.status, 400)
        self.assertEqual(self.f._profile_counts(), before)

    def test_pay_only_proposal_survives_another_edit_and_expired_review(self):
        from wahojobs.persistent_profile_corrections import PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS as TTL
        _, form = self.editor()
        result = self.submit(form, {'beta_pay_0_amount':'19'})
        self.assertEqual(result.status, 303)
        page = self.t.get(self.f._response_header(result, 'Location'))
        edit = next(link for link in self.f._markup(page).links if 'correction=edit' in link)
        form = self.f._form(self.t.get(edit), 'edit_run_id')
        result = self.submit(form, {'city':'Later draft city'})
        self.assertEqual(result.status, 303)
        self.f.registry_time += TTL + 1
        resume = self.t.get('/account/profile?correction=resume')
        form = self.f._form(resume, 'edit_run_id')
        result = self.f._post_form(self.t.browser, form['action'], form['fields'])[0]
        self.assertEqual(result.status, 303)
        page = self.t.get(self.f._response_header(result, 'Location'))
        self.assertIn(b'Strict minimum: USD 19/hour', page.body)
        self.assertEqual(self.t.apply(self.f._form(page, 'draft', 'review_token')).status, 200)
        current = self.t.current()
        self.assertEqual(current['preferences']['preference_model']['compensation_expectations'][0]['amount'], '19')
        self.assertEqual(current['location']['city'], 'Later draft city')


if __name__ == '__main__':
    unittest.main()
