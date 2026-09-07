"""Expired corrections restart through ordinary authentication, not token renewal."""
import unittest

from tests import test_persistent_profile_corrections as support
from scripts.local_product_app import MatchRunRegistry
from wahojobs.persistent_profile_corrections import PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS


class ProfileCorrectionRestartTests(unittest.TestCase):
    def setUp(self):
        self.f = support.PersistentProfileCorrectionTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.registry = MatchRunRegistry(absolute_ttl_seconds=PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
                                        _retention_clock=lambda: self.f.registry_time)
        self.browser = self.f._build_browser(correction_registry=self.registry)

    def get(self, url, session=None):
        return self.browser.handle('GET', url, self.f._browser_headers(session or self.f.session))

    def start(self):
        target = self.f._start_browser_correction(self.browser)
        review = self.get(target)
        edit = next(h for h in self.f._markup(review).links if 'correction=edit' in h)
        return target, edit, review

    def test_account_entry_and_expiry_restart_are_token_free_and_fresh(self):
        account = self.get('/account/profile')
        entry = '/account/profile?correction=start'
        self.assertIn(entry, self.f._markup(account).links)
        original, edit, review = self.start()
        before = self.f._profile_counts()
        confirm = self.f._form(review, 'draft', 'review_token')
        self.f.registry_time += PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS + 1
        expired = self.get(edit)
        self.assertEqual(expired.status, 410)
        self.assertIn(entry, self.f._markup(expired).links)
        self.assertIn(b'Start a new profile review', expired.body)
        self.assertNotIn(edit.encode(), expired.body)
        fields = self.f._set_form_field(confirm['fields'], 'confirmed', '1')
        denied, _ = self.f._post_form(self.browser, confirm['action'], fields)
        self.assertEqual(denied.status, 410)
        fresh, new_edit, _ = self.start()
        self.assertNotEqual(fresh, original)
        self.assertEqual(self.get(new_edit).status, 200)
        self.assertEqual(self.get(edit).status, 410)
        self.assertEqual(self.f._profile_counts(), before)

    def test_retained_form_changes_are_revalidated_in_new_unconfirmed_draft(self):
        _, old_edit, _ = self.start()
        old_form = self.f._form(self.get(old_edit), 'edit_run_id')
        retained = {'city': 'Synthetic Review City', 'software_tools': 'R, Docker'}
        self.f.registry_time += PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS + 1
        _, new_edit, _ = self.start()
        new_form = self.f._form(self.get(new_edit), 'edit_run_id')
        self.assertNotEqual(dict(old_form['fields'])['review_token'], dict(new_form['fields'])['review_token'])
        before = self.f._profile_counts()
        fields = self.f._set_form_field(new_form['fields'], 'credentials_confirmed', '1')
        for name, value in retained.items():
            fields = self.f._set_form_field(fields, name, value)
        response, _ = self.f._post_form(self.browser, new_form['action'], fields)
        self.assertEqual(response.status, 303)
        review = self.get(self.f._response_header(response, 'Location'))
        for value in retained.values():
            for term in value.split(', '): self.assertIn(term.encode(), review.body)
        self.assertEqual(self.f._profile_counts(), before)
        invalid = self.f._set_form_field(fields, 'software_tools', 'x' * 129)
        rejected, _ = self.f._post_form(self.browser, new_form['action'], invalid)
        self.assertEqual(rejected.status, 400)
        self.assertEqual(self.f._profile_counts(), before)

    def test_restart_binds_current_revision_and_rejects_stale_reference(self):
        _, old_edit, _ = self.start()
        grant = self.f._grant()
        offer, *_ = self.f._issue(grant, city='Confirmed Synthetic City')
        self.assertEqual(self.f._consume(grant, offer).state, 'corrected')
        stale = self.get(old_edit)
        self.assertEqual(stale.status, 409)
        self.assertIn('/account/profile?correction=start', self.f._markup(stale).links)
        before = self.f._profile_counts()
        _, new_edit, _ = self.start()
        self.assertIn(b'Confirmed Synthetic City', self.get(new_edit).body)
        self.assertEqual(self.f._profile_counts(), before)

    def test_restart_link_grants_no_access_to_other_owner_draft(self):
        _, edit, _ = self.start()
        outsider = self.f._new_session('92')
        denied = self.get(edit, outsider)
        self.assertIn(denied.status, (403, 404, 410))
        if denied.status == 410:
            self.assertIn('/account/profile?correction=start', self.f._markup(denied).links)
        self.assertNotIn(edit.encode(), denied.body)
        self.assertEqual(self.get(edit).status, 200)


if __name__ == '__main__':
    unittest.main()
