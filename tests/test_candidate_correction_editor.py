"""Anonymous long-profile correction fixtures; no personal recovery inputs."""
import json
import unittest

from tests import test_persistent_profile_corrections as support
from wahojobs.profiles.correction_editor import EDITOR_SCRIPT, EDITOR_SHA256


def long_education():
    return [dict(kind='bachelor', qualification='Bachelor of Business Administration',
                 field='Business Administration', institution='Example University', status='unknown', completion_year=None)] + [
        dict(kind='not_specified', qualification='Additional study', field=f'Study topic {i}',
             institution='', status='unknown', completion_year=None) for i in range(1, 11)]


class CandidateCorrectionEditorTests(unittest.TestCase):
    def setUp(self):
        self.f = support.PersistentProfileCorrectionTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.browser = self.f._build_browser()

    def get(self, target):
        return self.browser.handle('GET', target, self.f._browser_headers(self.f.session))

    def editor(self, changes=None):
        target = self.f._start_browser_correction(self.browser)
        review = self.get(target)
        edit = next(x for x in self.f._markup(review).links if 'correction=edit' in x)
        if changes:
            form = self.f._form(self.get(edit), 'edit_run_id')
            fields = self.f._set_form_field(form['fields'], 'credentials_confirmed', '1')
            for key, value in changes.items(): fields = self.f._set_form_field(fields, key, value)
            result, _ = self.f._post_form(self.browser, form['action'], fields)
            self.assertEqual(result.status, 303)
            target = self.f._response_header(result, 'Location')
            review = self.get(target)
            edit = next(x for x in self.f._markup(review).links if 'correction=edit' in x)
        return edit, self.get(edit)

    def long_editor(self):
        return self.editor(dict(education_entries=json.dumps(long_education()),
            education_level='not_specified', degrees='', institutions='', education_fields='',
            education_status='unknown', no_degree=None, hard_constraints='',
            recent_roles=json.dumps(['Reviewer, Example Company, remote']),
            specialties='Image review, Quality checks'))

    def test_long_profile_is_compact_and_keeps_associations_and_full_form(self):
        _, response = self.long_editor()
        html = response.body.decode()
        self.assertIn('Additional studies', html)
        self.assertIn("href='#review-actions'", html)
        self.assertIn('Review changes', html)
        for unwanted in ('Needs your input', '>Industries<', '>Professional domains<'):
            self.assertNotIn(unwanted, html)
        self.assertEqual(html.count('type="checkbox" name="remote"'), 1)
        self.assertNotIn("type='hidden' name='remote'", html)
        self.assertIn('I prefer remote work', html)
        fields = dict(self.f._form(response, 'edit_run_id')['fields'])
        self.assertEqual(len(json.loads(fields['education_entries'])), 11)
        self.assertIn('Example University', fields['education_entries'])
        self.assertIn("type='hidden' name='professional_domains'", html)
        self.assertIn(EDITOR_SCRIPT, html)
        self.assertIn(EDITOR_SHA256, self.f._response_header(response, 'Content-Security-Policy'))

    def test_untouched_and_collapsed_fields_roundtrip_without_revision_write(self):
        _, response = self.long_editor()
        form = self.f._form(response, 'edit_run_id')
        before = self.f._profile_counts()
        fields = self.f._set_form_field(form['fields'], 'credentials_confirmed', '1')
        result, _ = self.f._post_form(self.browser, form['action'], fields)
        self.assertEqual(result.status, 303)
        review = self.get(self.f._response_header(result, 'Location'))
        edit = next(x for x in self.f._markup(review).links if 'correction=edit' in x)
        actual = dict(self.f._form(self.get(edit), 'edit_run_id')['fields'])
        expected = dict(form['fields'])
        for key in expected.keys() - {'edit_run_id', 'review_token'}:
            self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(self.f._profile_counts(), before)

    def test_missing_hidden_required_field_rejects_without_clearing_profile(self):
        _, response = self.editor()
        form = self.f._form(response, 'edit_run_id')
        before = self.f._profile_counts()
        fields = self.f._set_form_field(form['fields'], 'professional_domains', None)
        fields = self.f._set_form_field(fields, 'credentials_confirmed', '1')
        response, _ = self.f._post_form(self.browser, form['action'], fields)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.f._profile_counts(), before)

    def test_country_error_targets_editor_and_retains_other_edits(self):
        _, response = self.long_editor()
        form = self.f._form(response, 'edit_run_id')
        fields = form['fields']
        for key,value in dict(country='Unrecognized place',city='Edited test city',credentials_confirmed='1').items():
            fields = self.f._set_form_field(fields,key,value)
        before = self.f._profile_counts()
        response,_ = self.f._post_form(self.browser,form['action'],fields)
        self.assertEqual(response.status,400)
        self.assertIn(b"href='#country'",response.body)
        self.assertIn(b'Enter a country name',response.body)
        self.assertIn(b'Edited test city',response.body)
        self.assertEqual(self.f._profile_counts(),before)
        restored = dict(self.f._form(response,'edit_run_id')['fields'])
        self.assertEqual(len(json.loads(restored['education_entries'])),11)

    def test_overlong_skill_remains_editable_with_item_feedback(self):
        _, response = self.editor()
        form = self.f._form(response,'edit_run_id')
        fields = self.f._set_form_field(form['fields'],'software_tools','x'*129)
        fields = self.f._set_form_field(fields,'credentials_confirmed','1')
        before = self.f._profile_counts()
        response,_ = self.f._post_form(self.browser,form['action'],fields)
        self.assertEqual(response.status,400)
        self.assertIn(b'Software and tools, item 1',response.body)
        self.assertIn(('x'*129).encode(),response.body)
        self.assertEqual(self.f._profile_counts(),before)

    def test_duplicate_study_feedback_retains_rows_without_json_editor(self):
        _, response = self.long_editor()
        form = self.f._form(response, 'edit_run_id')
        entries = long_education()
        fields = self.f._set_form_field(form['fields'], 'education_entries', json.dumps(entries + [entries[-1]]))
        fields = self.f._set_form_field(fields, 'credentials_confirmed', '1')
        before = self.f._profile_counts()
        result, _ = self.f._post_form(self.browser, form['action'], fields)
        self.assertEqual(result.status, 400)
        self.assertIn(b'empty or duplicate entries', result.body)
        self.assertIn(b'data-education-editor', result.body)
        self.assertNotIn(b"textarea id='education_entries'", result.body)
        actual = dict(self.f._form(result, 'edit_run_id')['fields'])
        self.assertEqual(len(json.loads(actual['education_entries'])), 12)
        self.assertEqual(self.f._profile_counts(), before)


if __name__ == '__main__': unittest.main()
