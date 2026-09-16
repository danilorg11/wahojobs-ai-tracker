"""Real shipped DOM/FormData, service-bound synthetic state; no HTTP listener."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests import test_candidate_correction_editor as fixture


class CorrectionEditorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CandidateCorrectionEditorTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def client(self, response, scenario, **options):
        node = os.environ.get('WAHOJOBS_CLIENT_NODE') or shutil.which('node')
        self.assertTrue(node, 'Node 22+ and the existing client_dom dependencies are required.')
        script = Path(__file__).with_name('correction_editor_lifecycle.cjs')
        env = dict(os.environ)
        env.setdefault('NODE_PATH', str(script.parent / 'client_dom' / 'node_modules'))
        result = subprocess.run([node, '--preserve-symlinks', '--preserve-symlinks-main',
            str(script), 'https://localhost:1', 'editor-lifecycle'],
            input=json.dumps(dict(html=response.body.decode(), scenario=scenario, **options)),
            capture_output=True, text=True, encoding='utf-8', timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = json.loads(result.stdout)
        result['fields'] = [tuple(pair) for pair in result['fields']]
        return result

    def test_done_reopen_formdata_review_and_confirmed_language_values_agree(self):
        _, response = self.f.editor()
        before = self.f.f._profile_counts()
        result = self.client(response, 'languages')
        posted, _ = self.f.f._post_form(self.f.browser, result['action'], result['fields'])
        self.assertEqual(posted.status, 303)
        review = self.f.get(self.f.f._response_header(posted, 'Location'))
        self.assertEqual(self.f.f._profile_counts(), before)
        confirm = self.f.f._form(review, 'draft', 'review_token')
        draft = self.f.browser._correction_registry.peek(dict(confirm['fields'])['draft'])
        self.assertEqual({v['language']:v['proficiency'] for v in draft.canonical_profile['languages']},
                         {'Portuguese':'native', 'English':'fluent'})
        fields = self.f.f._set_form_field(confirm['fields'], 'confirmed', '1')
        offer, _ = self.f.f._post_form(self.f.browser, confirm['action'], fields)
        self.assertEqual(offer.status, 200)
        apply = self.f.f._form(offer, 'artifact', 'csrf')
        applied, _ = self.f.f._post_form(self.f.browser, apply['action'], apply['fields'])
        self.assertEqual(applied.status, 303)
        current = self.f.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        self.assertEqual({v['language']:v['proficiency'] for v in current['languages']},
                         {'Portuguese':'native', 'English':'fluent'})
        sources = {v['field_path']: v for v in current['provenance']['field_sources']}
        for index in range(2):
            source = sources[f'languages[{index}].proficiency']
            self.assertTrue(source['explicit'])
            self.assertEqual(source['source_kind'], 'user_correction')

    def test_invalid_submission_retains_language_summary_controls_and_authority(self):
        _, response = self.f.editor()
        before = self.f.f._profile_counts()
        result = self.client(response, 'languages', invalidCountry=True)
        rejected, _ = self.f.f._post_form(self.f.browser, result['action'], result['fields'])
        self.assertEqual(rejected.status, 400)
        self.client(rejected, 'restored')
        self.assertEqual(self.f.f._profile_counts(), before)

    def test_repeated_fields_have_correct_labels_progressive_add_remove_undo_and_no_empty_facts(self):
        _, response = self.f.long_editor()
        self.client(response, 'rows')

    def test_item_detail_cancel_remove_undo_and_clear_keep_one_identity(self):
        _, response = self.f.editor()
        self.client(response, 'item')

    def duration_editor(self):
        from wahojobs.profiles.canonical_v2 import _material_field_paths
        from tests.test_professional_background_components import confirmed
        p = self.f.f._fixture_v2()
        p['experience']['total_years'] = 9
        p['experience']['years_by_domain'] = [dict(domain='customer support', years=2), dict(domain='writing', years=1.5)]
        p['provenance']['field_sources'] = []
        for path in _material_field_paths(p):
            confirmed(p, path)
        p['provenance']['field_sources'].sort(key=lambda row: (row['field_path'].casefold(), row['field_path']))
        self.f.f._install_current_v2(p, idempotency_key='synthetic-domain-duration-editor')
        return self.f.editor()[1]

    def apply_client(self, result):
        posted, _ = self.f.f._post_form(self.f.browser, result['action'], result['fields'])
        self.assertEqual(posted.status, 303)
        review = self.f.get(self.f.f._response_header(posted, 'Location'))
        confirm = self.f.f._form(review, 'draft', 'review_token')
        confirmed, _ = self.f.f._post_form(self.f.browser, confirm['action'],
            self.f.f._set_form_field(confirm['fields'], 'confirmed', '1'))
        self.assertEqual(confirmed.status, 200)
        apply = self.f.f._form(confirmed, 'artifact', 'csrf')
        result, _ = self.f.f._post_form(self.f.browser, apply['action'], apply['fields'])
        self.assertEqual(result.status, 303)
        return self.f.f._current().trusted_dict(include_structured_profile=True)['structured_profile']

    def test_scoped_duration_edit_reaches_review_confirmation_and_keeps_total_and_fractional_fact(self):
        result = self.client(self.duration_editor(), 'duration')
        current = self.apply_client(result)
        self.assertEqual(current['experience']['years_by_domain'],
                         [dict(domain='customer support', years=3), dict(domain='writing', years=1.5)])
        self.assertEqual(current['experience']['total_years'], 9)

    def test_explicit_duration_removal_survives_final_confirmation_without_clearing_total(self):
        result = self.client(self.duration_editor(), 'duration', clearDuration=True)
        current = self.apply_client(result)
        self.assertEqual(current['experience']['years_by_domain'], [dict(domain='writing', years=1.5)])
        self.assertEqual(current['experience']['total_years'], 9)

    def test_blank_duration_is_recoverable_and_never_becomes_zero(self):
        result = self.client(self.duration_editor(), 'duration', blankDuration=True)
        before = self.f.f._profile_counts()
        rejected, _ = self.f.f._post_form(self.f.browser, result['action'], result['fields'])
        self.assertEqual(rejected.status, 400)
        self.assertIn(b'Blank values are not saved as zero', rejected.body)
        self.assertEqual(self.f.f._profile_counts(), before)

    def test_older_form_without_duration_field_preserves_scoped_facts(self):
        response = self.duration_editor()
        form = self.f.f._form(response, 'edit_run_id')
        fields = [(name, value) for name, value in form['fields'] if name != 'domain_years_review']
        fields = self.f.f._set_form_field(fields, 'credentials_confirmed', '1')
        posted, _ = self.f.f._post_form(self.f.browser, form['action'], fields)
        self.assertEqual(posted.status, 303)
        review = self.f.get(self.f.f._response_header(posted, 'Location'))
        self.assertIn(b'No profile details have changed', review.body)
        current = self.f.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        self.assertEqual(current['experience']['years_by_domain'],
                         [dict(domain='customer support', years=2), dict(domain='writing', years=1.5)])

    def test_new_domain_is_rejected_at_actual_form_boundary_without_profile_mutation(self):
        response = self.duration_editor()
        form = self.f.f._form(response, 'edit_run_id')
        raw = json.dumps(dict(version=1, entries=[dict(domain='clinical psychology', years='8')]))
        fields = self.f.f._set_form_field(form['fields'], 'domain_years_review', raw)
        fields = self.f.f._set_form_field(fields, 'credentials_confirmed', '1')
        before = self.f.f._profile_counts()
        rejected, _ = self.f.f._post_form(self.f.browser, form['action'], fields)
        self.assertEqual(rejected.status, 400)
        self.assertIn(b"href='#domain-duration-editor'", rejected.body)
        self.assertEqual(self.f.f._profile_counts(), before)

    def test_pre_extension_retained_checkpoint_resumes_from_authoritative_duration_snapshot(self):
        from wahojobs import profile_correction_drafts as drafts
        self.duration_editor()
        service = self.f.f.service
        grant = self.f.f._grant()
        preparation = service.prepare_initial_review(grant)
        service.retain_review(grant, 'c' * 24, preparation)
        connection = self.f.f._connection()
        try:
            with drafts.store_connection(connection, write=True) as sidecar:
                original = drafts.load(sidecar, owner=service._retained_owner(grant), reference='c' * 24)
                payload = json.loads(json.dumps(original[3]))
                payload['updates'].pop('domain_years_review')
                drafts.save(sidecar, reference='l' * 24, owner=service._retained_owner(grant),
                    base_revision=original[1], base_hash=original[2], payload=payload,
                    created_at=self.f.f.now.isoformat())
                legacy = drafts.load(sidecar, owner=service._retained_owner(grant), reference='l' * 24)
            counts = self.f.f._profile_counts()
            resumed = service.retained_review(grant, 'l' * 24)
            self.assertEqual(resumed['state'], 'ready')
            self.assertEqual(resumed['proposed'], payload['proposed'])
            self.assertEqual(self.f.f._profile_counts(), counts)
            with drafts.store_connection(connection) as sidecar:
                self.assertEqual(drafts.load(sidecar, owner=service._retained_owner(grant), reference='l' * 24), legacy)
                self.assertEqual(drafts.load(sidecar, owner=service._retained_owner(grant), reference='c' * 24), original)
            renewed = self.f.f._grant(session=self.f.f._new_session('domain-renewed-session'))
            # A new session of the same owner can resume under its own grant.
            self.assertEqual(service.retained_review(renewed, 'l' * 24)['state'], 'ready')
        finally:
            connection.close()

    def test_manual_editor_defers_saved_state_to_its_autosave_status(self):
        from dataclasses import replace
        _, response = self.f.editor()
        response = replace(response, body=response.body.replace(b"id='profile-review-form'", b"id='profile-review-form' data-manual-draft"))
        self.client(response, 'manual-status')

    def test_independent_study_completion_is_visible_without_linking_qualifications(self):
        from wahojobs.profiles.correction_editor import summary_sections
        text = summary_sections(dict(education=dict(education_level='high_school', completion_status='completed',
            degrees=[], fields_or_domains=[], institutions=[])))
        self.assertIn('Education level: high school', text)
        self.assertIn('Study status: completed', text)


class DomainDurationReviewContractTests(unittest.TestCase):
    def test_omission_and_unchanged_fraction_preserve_values_and_changed_values_are_bound(self):
        from wahojobs.profiles.domain_duration_editor import form_value, reviewed
        existing = {'customer support': 2, 'writing': 1.5}
        for raw in (None, '', form_value(existing)):
            self.assertEqual(reviewed(raw, existing), existing)
        self.assertEqual(reviewed(form_value({'customer support': 0}), existing), {'customer support': 0})
        self.assertEqual(reviewed(form_value({}), existing), {})
        for rows in ([dict(domain='new specialty', years='2')],
                     [dict(domain='customer support', years=''),],
                     [dict(domain='customer support', years='2.5')],
                     [dict(domain='customer support', years='81')],
                     [dict(domain='customer support', years='-1')],
                     [dict(domain='customer support', years=True)],
                     [dict(domain='customer support', years='2'), dict(domain='customer support', years='3')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                reviewed(json.dumps(dict(version=1, entries=rows)), existing)
        with self.assertRaises(ValueError):
            reviewed('{"version":1,"entries":[],"entries":[]}', existing)


if __name__ == '__main__':
    unittest.main()
