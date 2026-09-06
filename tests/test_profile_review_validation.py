from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from tests import test_profile_intake_final_save as support
from wahojobs.profile_intake.runtime import hydrate_profile_intake_checkpoint, review_collection_entries
from wahojobs.profiles.canonical_v2 import MAX_DYNAMIC_LABEL_LENGTH


COMBINED_SKILL = ('Translation; editing; proofreading; content quality review; Microsoft Word; '
                  'CAT tools; spreadsheets; Portuguese-English translation and editorial quality')


class _ResidenceAdapter(support._FinalSaveAdapter):
    def extract(self, evidence):
        result = super().extract(evidence)
        return replace(result, facts=tuple(
            replace(f, field_path='location.residence', value='I reside in Brazil.')
            if f.field_path == 'location.city' else f for f in result.facts
        ))


class ProfileReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = support.ProfileIntakeFinalSaveTests()
        self.c.setUp()

    def tearDown(self):
        self.c.tearDown()

    def start(self, residence=False):
        if residence:
            self.c.integration.close()
            self.c.integration = self.c._build(_ResidenceAdapter(
                self.c.path, (self.c.read_provider, self.c.write_provider)))
        self.ref = self.c._reference(self.c._upload())
        self.review = self.c.integration._processing.vault.get(self.ref, self.c._grant()).review
        self.skill = next(i for i, f in enumerate(self.review.facts) if f.field_path == 'skills.normalized')

    def submit(self, **kwargs):
        response = self.c._post_review(self.ref, self.c._review_body(self.ref, action='save', **kwargs))
        location = dict(response.headers).get('Location', '')
        if 'check=1' in location:
            self.assertEqual(response.status, 303)
            from io import BytesIO
            page = self.c.integration.handle('GET', location, self.c._headers(), BytesIO())
            self.assertEqual(page.status, 200)
            return page
        return response

    def assert_uncommitted(self):
        with self.c._database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM product_profiles').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM product_profile_revisions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM ai_profile_import_entitlements').fetchone()[0], 'reserved')
            return hydrate_profile_intake_checkpoint(db.execute(
                'SELECT review_payload_json FROM ai_profile_intake_checkpoints').fetchone()[0])

    def test_canonical_length_boundary_is_accepted(self):
        self.assertEqual(MAX_DYNAMIC_LABEL_LENGTH, 128)
        self.start()
        self.assertEqual(self.submit(changes={self.skill: 'x' * 128}).status, 303)

    def test_one_over_canonical_limit_targets_exact_item_and_preserves_it(self):
        self.start()
        response = self.submit(changes={self.skill: 'x' * 129})
        self.assertEqual(response.status, 200)
        self.assertIn(b'data-review-error-target=\'review-collection-skills-0-value\'', response.body)
        self.assertIn(b'128 characters maximum', response.body)
        self.assertIn(('x' * 129).encode(), response.body)
        saved = self.assert_uncommitted()
        self.assertEqual(saved.facts[self.skill].value, 'x' * 129)

    def test_combined_entry_preserves_omissions_edits_and_resumes_before_correction(self):
        self.assertEqual(len(COMBINED_SKILL), 153)
        self.start()
        city = next(i for i, f in enumerate(self.review.facts) if f.field_path == 'location.city')
        response = self.submit(changes={self.skill: COMBINED_SKILL, city: ''},
                               preference_overrides={'missing_soft_preferences': 'Prefer 20 hours, availability not confirmed'})
        self.assertEqual(response.status, 200)
        self.assertIn(COMBINED_SKILL.encode(), response.body)
        self.assertIn(b'Add another skill', response.body)
        self.assertNotIn(b'canonical_profile', response.body)
        saved = self.assert_uncommitted()
        self.assertEqual(saved.facts[city].decision, 'remove')
        self.assertEqual(saved.facts[self.skill].value, COMBINED_SKILL)
        self.assertIn('20 hours', dict(saved.user_inputs)['soft_preferences'])
        from wahojobs.profile_intake.browser import _review_page
        snapshot = self.c.integration._processing.vault.get(self.ref, self.c._grant())
        page = _review_page(self.ref, replace(snapshot, review=saved), self.c.session['csrf_secret'], save_enabled=True)
        self.assertIn(COMBINED_SKILL, page)
        body = self.c._review_body(self.ref, action='save', changes={self.skill: 'Translation', city: ''})
        self.assertEqual(self.c._post_review(self.ref, body).status, 303)
        self.assertEqual(self.c._post_review(self.ref, body).status, 303)
        with self.c._database() as db:
            rows = db.execute('SELECT structured_profile_json FROM product_profile_revisions').fetchall()
            self.assertEqual(len(rows), 1)
            profile = json.loads(rows[0][0])
            self.assertEqual(profile['location']['city'], '')
            self.assertEqual(profile['skills']['normalized'], ['Translation'])
            self.assertNotIn('proficiency', profile['skills']['entries'][0])

    def test_server_preflight_uses_actual_commit_builder_before_atomic_save(self):
        self.start()
        from wahojobs import ai_profile_import as core
        with patch.object(core, '_confirmed_profile_v2', wraps=core._confirmed_profile_v2) as build, \
             patch.object(type(self.c.integration._processing._durable), 'commit') as commit:
            self.assertEqual(self.submit(changes={self.skill: COMBINED_SKILL}).status, 200)
            self.assertEqual(build.call_count, 2)  # POST and redirected read-only GET
            commit.assert_not_called()
        self.assert_uncommitted()

    def test_prose_residence_has_specific_feedback_and_accepts_existing_country_code(self):
        self.start(residence=True)
        index = next(i for i, f in enumerate(self.review.facts) if f.field_path == 'location.residence')
        response = self.submit()
        self.assertEqual(response.status, 200)
        self.assertIn(f'data-review-error-target=\'review-fact-{index}-value\''.encode(), response.body)
        self.assertIn(b'one country name or two-letter code', response.body)
        self.assertIn(b'list=\'profile-country-options\'', response.body)
        saved = self.assert_uncommitted()
        self.assertEqual(saved.facts[index].value, 'I reside in Brazil.')
        self.assertEqual(self.submit(changes={index: 'BR'}).status, 303)
        with self.c._database() as db:
            profile = json.loads(db.execute('SELECT structured_profile_json FROM product_profile_revisions').fetchone()[0])
            self.assertEqual(profile['location']['residence'], 'Brazil')

    def test_ambiguous_residence_remains_in_draft_without_inference(self):
        self.start(residence=True)
        index = next(i for i, f in enumerate(self.review.facts) if f.field_path == 'location.residence')
        response = self.submit(changes={index: 'Brazil or Portugal'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.assert_uncommitted().facts[index].value, 'Brazil or Portugal')

    def test_missing_optional_location_is_not_made_mandatory(self):
        self.start(residence=True)
        index = next(i for i, f in enumerate(self.review.facts) if f.field_path == 'location.residence')
        self.assertEqual(self.submit(changes={index: ''}).status, 303)

    def test_feedback_get_is_owned_read_only_and_clears_after_valid_edit(self):
        from io import BytesIO
        self.start()
        response = self.c._post_review(self.ref, self.c._review_body(
            self.ref, action='save', changes={self.skill: COMBINED_SKILL}))
        self.assertEqual(response.status, 303)
        location = dict(response.headers)['Location']
        self.assertIn('check=1', location)
        saved = self.assert_uncommitted()
        for _ in range(2):
            page = self.c.integration.handle('GET', location, self.c._headers(), BytesIO())
            self.assertEqual(page.status, 200)
            self.assertIn(COMBINED_SKILL.encode(), page.body)
            self.assertEqual(self.assert_uncommitted(), saved)
        other = dict(self.c.session, session_token='z' * 43)
        denied = self.c.integration.handle('GET', location, self.c._headers(session=other), BytesIO())
        self.assertNotIn(COMBINED_SKILL.encode(), denied.body)
        self.assertEqual(self.c._post_review(self.ref, self.c._review_body(
            self.ref, action='autosave', changes={self.skill: 'Translation'})).status, 204)
        page = self.c.integration.handle('GET', location, self.c._headers(), BytesIO())
        self.assertNotIn(b"id='profile-field-error'", page.body)
        self.assert_uncommitted()

    def test_removed_overlong_item_does_not_trigger_a_validation_error(self):
        self.start()
        self.assertEqual(self.submit(changes={self.skill: COMBINED_SKILL}).status, 200)
        self.assertEqual(self.submit(changes={self.skill: ''}).status, 303)
        with self.c._database() as db:
            profile = json.loads(db.execute('SELECT structured_profile_json FROM product_profile_revisions').fetchone()[0])
            self.assertEqual(profile['skills']['normalized'], [])

    def test_invalid_review_cannot_be_saved_by_another_owner(self):
        self.start()
        body = self.c._review_body(self.ref, action='save', changes={self.skill: COMBINED_SKILL})
        other = dict(self.c.session, session_token='z' * 43)
        response = self.c._post_review(self.ref, body, session=other)
        self.assertNotEqual(response.status, 200)
        saved = self.assert_uncommitted()
        self.assertEqual(saved.facts[self.skill].value, 'Python')


if __name__ == '__main__':
    unittest.main()
