"""Candidate cleanup regressions; every account and database is disposable."""
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_profile_review_transfer as transfer
from tests.test_persistent_profile_corrections import _logical_snapshot


class ProfileCleanupTests(unittest.TestCase):
    def setUp(self):
        self.t = transfer.ProfileReviewTransferTests()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.f, self.browser = self.t.f, self.t.browser

    def post(self, form):
        return self.f._post_form(self.browser, form['action'], form['fields'])[0]

    def editor(self):
        page = self.t.get('/account/profile?correction=start')
        self.assertEqual(page.status, 200)
        self.assertIn(b'Edit your profile', page.body)
        self.assertNotIn(b'Start a profile correction', page.body)
        return self.f._form(page, 'edit_run_id')

    def test_direct_entry_draft_resume_discard_and_confirmed_preservation(self):
        before = _logical_snapshot(self.f.path)
        self.assertNotIn(b'draft-notice', self.t.get('/account/profile').body)
        edit = self.editor()
        original_city = dict(edit['fields'])['city']
        edit['fields'] = self.f._set_form_field(edit['fields'], 'city', 'Synthetic unfinished city')
        self.assertEqual(self.post(edit).status, 303)
        page = self.t.get('/account/profile')
        self.assertIn(b'Continue editing', page.body)
        self.assertNotIn(b'Resume saved changes', page.body)
        resumed = self.f._form(self.t.get('/account/profile?correction=resume'), 'edit_run_id')
        self.assertEqual(dict(resumed['fields'])['city'], 'Synthetic unfinished city')
        discard = self.f._form(page, 'retained_draft')
        self.assertEqual(self.post(discard).status, 303)
        self.assertEqual(_logical_snapshot(self.f.path), before)
        self.assertNotIn(b'draft-notice', self.t.get('/account/profile').body)
        self.assertEqual(dict(self.editor()['fields'])['city'], original_city)
        self.assertEqual(self.post(resumed).status, 410)

    def test_review_discloses_location_constraints_and_credential_status(self):
        page, form = self.t.review({'geographic_restrictions': 'Synthetic location constraint', 'credential_status': 'in_progress'})
        self.assertIn(b'Synthetic location constraint', page.body)
        self.assertIn(b'Working toward a professional credential', page.body)
        self.assertIn(b'Added:', page.body)
        self.assertEqual(self.t.apply(form).status, 200)
        self.assertIn('Synthetic location constraint', self.t.current()['location']['geographic_work_restrictions'])

    def test_reviewed_year_changes_clear_linked_shadows_and_keep_independent_years(self):
        import json
        from wahojobs.profiles.canonical_v2 import _material_field_paths
        from tests.test_professional_background_components import confirmed
        profile = self.f._fixture_v2()
        profile['education']['graduation_years'] = [1998]
        profile['provenance']['field_sources'] = []
        for path in _material_field_paths(profile):
            confirmed(profile, path)
        profile['provenance']['field_sources'].sort(key=lambda row: (row['field_path'].casefold(), row['field_path']))
        self.f._install_current_v2(profile, idempotency_key='independent-study-year')
        entries = [
            dict(kind='phd', qualification='Biology', field='Biology', institution='U Penn', status='completed', completion_year=2020),
            dict(kind='bachelor', qualification='English', field='English', institution='Example School', status='completed', completion_year=2020),
        ]
        def save(expected_years, **changes):
            _, form = self.t.review(dict(education_entries=json.dumps(entries), **changes))
            self.assertEqual(self.t.apply(form).status, 200)
            current = self.t.current()['education']
            self.assertEqual(current['graduation_years'], expected_years)
            self.assertEqual({json.dumps(e, sort_keys=True) for e in current.get('entries', [])},
                             {json.dumps(e, sort_keys=True) for e in entries})
        save([1998, 2020], education_level='not_specified', education_status='unknown',
             no_degree=None, hard_constraints='', degrees='', education_fields='', institutions='')
        entries[0]['completion_year'] = 2021
        save([1998, 2020, 2021])
        entries.pop()
        save([1998, 2021])
        entries[0]['completion_year'] = None
        save([1998])
        entries.clear()
        save([1998])
        before = self.f._profile_counts()
        unchanged = self.editor()
        response = self.post(unchanged)
        review = self.t.get(self.f._response_header(response, 'Location'))
        self.assertIn(b'No changes to save', review.body)
        self.assertEqual(self.f._profile_counts(), before)

    def test_noop_revert_eclipses_pending_draft_and_never_adds_revision(self):
        before = _logical_snapshot(self.f.path)
        original_city = dict(self.editor()['fields'])['city']
        self.t.review({'city': 'Changed city'})
        resumed = self.f._form(self.t.get('/account/profile?correction=resume'), 'edit_run_id')
        resumed['fields'] = self.f._set_form_field(resumed['fields'], 'city', original_city)
        response = self.post(resumed)
        review = self.t.get(self.f._response_header(response, 'Location'))
        self.assertIn(b'No changes to save', review.body)
        self.assertEqual(self.f._markup(review).forms, [])
        self.assertNotIn(b'draft-notice', self.t.get('/account/profile').body)
        self.assertEqual(_logical_snapshot(self.f.path), before)

    def test_discard_invalidates_issued_artifact_and_keeps_confirmed_profile(self):
        _, form = self.t.review({'city': 'Must not be applied'})
        # Compatibility confirmation can issue an artifact without applying it.
        confirmed = self.post(form)
        offer = self.f._form(confirmed, 'artifact', 'csrf')
        before = _logical_snapshot(self.f.path)
        discard = self.f._form(self.t.get('/account/profile'), 'retained_draft')
        self.assertEqual(self.post(discard).status, 303)
        self.assertEqual(self.post(offer).status, 410)
        self.assertEqual(_logical_snapshot(self.f.path), before)

    def test_stale_discard_preserves_newer_draft_and_saved_proposal_notice_clears(self):
        self.t.review({'city': 'Earlier city'})
        stale = self.f._form(self.t.get('/account/profile'), 'retained_draft')
        _, latest = self.t.review({'city': 'Later city'})
        self.assertEqual(self.post(stale).status, 409)
        self.assertEqual(self.t.apply(latest).status, 200)
        self.assertNotIn(b'draft-notice', self.t.get('/account/profile').body)
        self.assertEqual(self.t.current()['location']['city'], 'Later city')


class ProfilePresentationTests(unittest.TestCase):
    def test_selected_degree_type_is_displayed_without_inference_or_repetition(self):
        from wahojobs.candidate_readability import education_entry_title
        cases = [
            ('phd', 'Biology', 'Biology', 'PhD in Biology'),
            ('phd', 'PhD in Biology', 'Biology', 'PhD in Biology'),
            ('doctorate', '', 'Biology', 'Doctorate in Biology'),
            ('not_specified', 'Biology', 'Biology', 'Biology'),
            ('bachelor', 'Bachelor of Biology', 'Biology', 'Bachelor of Biology'),
            ('not_specified', 'Exchange study', 'Biology', 'Exchange study in Biology'),
        ]
        for kind, qualification, field, expected in cases:
            with self.subTest(kind=kind, qualification=qualification):
                self.assertEqual(education_entry_title(dict(kind=kind, qualification=qualification, field=field)), expected)
    def test_completed_study_change_is_visible_in_review(self):
        from copy import deepcopy
        from wahojobs.profiles.correction_editor import change_summary, summary_sections
        before = {'education': {'entries': [dict(kind='bachelor', qualification='Bachelor of Computer Science',
            field='Computer Science', institution='Example University', status='unknown', completion_year=None)]}}
        after = deepcopy(before)
        after['education']['entries'][0]['status'] = 'completed'
        self.assertIn('Completed', summary_sections(after))
        self.assertIn('Completed', change_summary(before, after))
        self.assertEqual(summary_sections(after).count('Computer Science'), 1)

    def test_identical_interest_lists_display_once_without_changing_storage(self):
        from copy import deepcopy
        from wahojobs.profiles.preference_model import empty_profile_preferences_v2
        from wahojobs.profiles.preference_presentation import preference_summary
        preferences = dict(remote=True, preference_model=empty_profile_preferences_v2(),
            target_opportunity_types=['Content review'], preferred_task_types=['Content review'])
        before = deepcopy(preferences)
        rows = preference_summary(preferences)
        self.assertEqual(sum('Content review' in row for row in rows), 1)
        self.assertFalse(any('Remote' in row for row in rows))
        self.assertEqual(preferences, before)


class StatusCorrectionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_pipeline_actions import PipelineActionTests
        self.f = PipelineActionTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_restore_keeps_reminder_visibility_audit_and_replay(self):
        from wahojobs import pipeline_reconciliation
        f = self.f
        saved = f.create()
        applied = f.act(saved, 'applied')
        reminder = f.act(applied, 'remind_later', reminder_at='2026-10-01T00:00:00+00:00')
        hidden = f.act(reminder, 'not_interested')
        key = f.key('correction')
        corrected = f.act(hidden, 'undo_applied', idempotency_key=key)
        self.assertEqual(corrected.state['workflow_status'], 'saved')
        self.assertEqual(corrected.state['visibility'], 'hidden')
        self.assertEqual(corrected.state['reminder_at'], reminder.state['reminder_at'])
        self.assertEqual(corrected.transition['correction_of_transition_id'], applied.transition['transition_id'])
        counts = f.counts()
        self.assertTrue(f.act(hidden, 'undo_applied', idempotency_key=key).replayed)
        self.assertEqual(f.counts(), counts)
        self.assertTrue(pipeline_reconciliation.reconcile_pipeline_state(f.conn)['fully_reconciled'])

    def test_unknown_previous_progress_is_restored_without_inventing_saved(self):
        from wahojobs import pipeline_actions as actions
        f = self.f
        item = f.insert_unknown(visibility='visible', reminder_at='2026-10-01T00:00:00+00:00')
        applied = actions.perform_pipeline_action(f.conn, action='applied', owner_profile_id='profile-a',
            idempotency_key=f.key('unknown'), expected_version=1, match_run_id='fixture', pipeline_item_id=item)
        corrected = f.act(applied, 'correct_applied')
        self.assertIsNone(corrected.state['workflow_status'])
        self.assertEqual(corrected.state['workflow_status_provenance'], 'unknown_legacy')
        self.assertEqual(corrected.state['reminder_at'], '2026-10-01T00:00:00+00:00')

    def test_ownership_stale_and_atomic_failure(self):
        import sqlite3
        from wahojobs import pipeline_state as state
        f = self.f
        applied = f.create('applied')
        before = f.counts()
        with self.assertRaises(state.OwnershipError):
            f.act(applied, 'undo_applied', owner_profile_id='profile-b')
        with self.assertRaises(state.StaleStateVersion):
            f.act(applied, 'undo_applied', expected_version=1)
        f.conn.execute("CREATE TRIGGER fail_correction BEFORE UPDATE ON user_pipeline_items BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            f.act(applied, 'undo_applied')
        self.assertEqual(f.counts(), before)
        self.assertEqual(state.get_current_state(f.conn, applied.pipeline_item['pipeline_item_id'], 'profile-a'), applied.state)

    def test_concurrent_duplicate_and_stale_requests_have_one_effect(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from wahojobs import pipeline_actions as actions, pipeline_state as state
        f = self.f
        applied = f.create('applied')
        before = f.counts()
        barrier = Barrier(2)
        def attempt(key):
            connection = f.connect()
            try:
                barrier.wait(timeout=5)
                result = actions.perform_pipeline_action(connection, action='undo_applied',
                    pipeline_item_id=applied.pipeline_item['pipeline_item_id'], owner_profile_id='profile-a',
                    expected_version=applied.state['version'], idempotency_key=key, match_run_id='fixture')
                return 'replayed' if result.replayed else 'corrected'
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, ['cleanup-concurrent-same-key'] * 2)), ['corrected', 'replayed'])
        self.assertEqual(f.counts()[2], before[2] + 1)
        with self.assertRaises(state.StaleStateVersion):
            f.act(applied, 'undo_applied', idempotency_key='cleanup-different-stale-key')


class CleanupHTTPClientTests(unittest.TestCase):
    def test_education_removal_and_degree_through_native_forms_https_and_persistence(self):
        from tests.candidate_decision_support import decision_state, observe, verified_https_request
        from tests.candidate_continuity_support import running_process
        from tests.test_candidate_continuity_client import run_client
        script = Path(__file__).with_name('candidate_decision_client.cjs')
        with decision_state(typed_preferences=True) as state, patch('tests.test_candidate_continuity_client.https_request', verified_https_request):
            with running_process(state):
                self.assertTrue(run_client(state, 'education-cleanup', script=script, observe=observe))
    def test_native_forms_shipped_script_http_and_persistence(self):
        from tests.candidate_decision_support import decision_state, observe, verified_https_request
        from tests.candidate_continuity_support import running_process
        from tests.test_candidate_continuity_client import run_client
        script = Path(__file__).with_name('candidate_decision_client.cjs')
        with decision_state() as state, patch('tests.test_candidate_continuity_client.https_request', verified_https_request):
            with running_process(state):
                result = run_client(state, 'ux-cleanup', script=script, observe=observe)
                self.assertTrue(result)


if __name__ == '__main__':
    unittest.main()
