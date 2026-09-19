"""Anonymous server regressions supplement the real browser transfer checks."""
import json
import unittest

from tests import test_persistent_profile_corrections as support
from scripts.local_product_app import MatchRunRegistry
from wahojobs.persistent_profile_corrections import PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS
from wahojobs.profiles.correction_editor import changed_profile_sections


class ProfileReviewTransferTests(unittest.TestCase):
    def setUp(self):
        self.f = support.PersistentProfileCorrectionTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.browser = self.f._build_browser(correction_registry=MatchRunRegistry(
            absolute_ttl_seconds=PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
            _retention_clock=lambda: self.f.registry_time,
        ))

    def current(self):
        return self.f._current().trusted_dict(include_structured_profile=True)['structured_profile']

    def get(self, target):
        return self.browser.handle('GET', target, self.f._browser_headers(self.f.session))

    def review(self, changes=None):
        page = self.get(self.f._start_browser_correction(self.browser))
        if changes:
            edit = next(link for link in self.f._markup(page).links if 'correction=edit' in link)
            form = self.f._form(self.get(edit), 'edit_run_id')
            fields = self.f._set_form_field(form['fields'], 'credentials_confirmed', '1')
            for key, value in changes.items():
                fields = self.f._set_form_field(fields, key, value)
            result, _ = self.f._post_form(self.browser, form['action'], fields)
            self.assertEqual(result.status, 303)
            page = self.get(self.f._response_header(result, 'Location'))
        return page, (self.f._form(page, 'draft', 'review_token') if changes else None)

    def apply(self, form, **kwargs):
        fields = self.f._set_form_field(form['fields'], 'confirmed', '1')
        fields = self.f._set_form_field(fields, 'apply_now', '1')
        return self.f._post_form(self.browser, form['action'], fields, **kwargs)[0]

    def test_one_apply_persists_exact_sealed_proposal_and_reports_changes(self):
        before = self.current()
        counts = self.f._profile_counts()
        page, form = self.review(dict(country='Brazil', city='Example City',
            recent_roles=json.dumps(['Evaluator, Example Employer']),
            specialties='Model output evaluation, Image review, Annotation',
            job_titles='', software_tools='R'))
        run = self.browser._correction_registry.peek(dict(form['fields'])['draft'])
        expected = run.recommendation_context['correction_preparation'].profile_for_browser()
        self.assertEqual(self.current(), before)
        self.assertIn(b'Changes awaiting confirmation', page.body)
        result = self.apply(form)
        self.assertEqual(result.status, 200)
        self.assertIn(b'Profile changes saved', result.body)
        self.assertIn(b'Example City', result.body)
        self.assertIn(b'Model output evaluation', result.body)
        actual = self.current()
        self.assertFalse(changed_profile_sections(expected, actual))
        self.assertEqual(actual['experience'].get('job_titles', []), [])
        self.assertEqual(actual['credentials'], before['credentials'])
        self.assertEqual(self.f._profile_counts()[1], counts[1] + 1)
        replay = self.apply(form)
        self.assertIn(replay.status, (200, 409, 410))
        self.assertEqual(self.f._profile_counts()[1], counts[1] + 1)

    def test_true_noop_is_explicit_and_does_not_create_a_revision(self):
        before = self.current()
        counts = self.f._profile_counts()
        page, form = self.review()
        self.assertIn(b'No changes to save', page.body)
        self.assertNotIn(b'>Save changes</button>', page.body)
        self.assertFalse(self.f._markup(page).forms)
        self.assertEqual(self.f._profile_counts(), counts)
        self.assertEqual(self.current(), before)

    def test_one_apply_still_requires_explicit_confirmation(self):
        _, form = self.review(dict(city='Example City'))
        before = self.f._profile_counts()
        fields = self.f._set_form_field(form['fields'], 'apply_now', '1')
        fields = self.f._set_form_field(fields, 'confirmed', None)
        result, _ = self.f._post_form(self.browser, form['action'], fields)
        self.assertEqual(result.status, 400)
        self.assertEqual(self.f._profile_counts(), before)

    def test_expired_and_other_owner_proposals_are_rejected(self):
        _, form = self.review(dict(city='Example City'))
        counts = self.f._profile_counts()
        self.assertIn(self.apply(form, session=self.f._new_session('92')).status, (403, 404, 410))
        self.f.registry_time += PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS + 1
        self.assertEqual(self.apply(form).status, 410)
        self.assertEqual(self.f._profile_counts(), counts)

    def test_old_proposal_cannot_overwrite_new_revision(self):
        _, old = self.review(dict(city='Old Proposal'))
        _, new = self.review(dict(city='New Proposal'))
        self.assertEqual(self.apply(new).status, 200)
        counts = self.f._profile_counts()
        self.assertEqual(self.apply(old).status, 409)
        self.assertEqual(self.current()['location']['city'], 'New Proposal')
        self.assertEqual(self.f._profile_counts(), counts)

if __name__ == '__main__':
    unittest.main()
