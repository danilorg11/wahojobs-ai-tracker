"""Anonymous optional self-report and owner-bound confirmation regressions."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from tests.test_confirmed_activity_matching import candidate, v2
from tests import test_profile_review_transfer as transfer
from tests import test_profile_correction_resume as resume_support
from tests.test_candidate_condition_comparisons import compared, prepared
from wahojobs.candidate_condition_comparisons import compare_conditions
from wahojobs.profiles.item_experience import canonical_items, with_reviewed_items, PREFIX
from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2, project_v2_to_matcher_v1


def item(label='R', field='skills', **changes):
    result = dict(item_id='a'*32, field=field, label=label, contexts=['projects'],
                  autonomy='guided', months=6, basis='self_reported')
    result.update(changes)
    return result


def base():
    c = candidate(skills=['R', 'Python'], activities=['Image annotation', 'Model output evaluation'])
    c['experience']['total_years'] = 20
    from wahojobs.profiles.canonical import field_sources_for_profile
    c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
    return v2(c)


def enriched(p=None, records=None):
    p = p or base()
    return with_reviewed_items(p, json.dumps(records if records is not None else [item()]), p)


def compare(text, p):
    packet = prepared(text, p)
    return compare_conditions(packet, p, include_item_experience=True)[0]


class ItemExperienceContractTests(unittest.TestCase):
    def test_old_profile_unknown_and_unchanged_scoring_projection(self):
        p = base(); before = deepcopy(p); new = enriched(p)
        self.assertEqual(validate_canonical_profile_v2(p), p)
        self.assertEqual(p, before)
        self.assertNotIn('item_details', p['experience'])
        self.assertEqual(project_v2_to_matcher_v1(p, matcher_profile_id='synthetic'),
                         project_v2_to_matcher_v1(new, matcher_profile_id='synthetic'))
        self.assertEqual((new['experience']['total_years'], new['experience']['item_details'][0]['months']), (20, 6))
        unknown = enriched(p, [item(contexts=[], autonomy='unknown', months=None)])
        self.assertEqual(compare('**Required**\nExperience with R', unknown)['status'], 'not_established')
        self.assertEqual(with_reviewed_items(p, '[]', p), p)

    def test_contract_limits_and_unknown_are_authoritative(self):
        for changes in (dict(months=-1), dict(months=961), dict(months=2.5), dict(months=True),
                        dict(contexts=['professional','professional']), dict(autonomy='expert'),
                        dict(basis='verified'), dict(field='languages'), dict(item_id='other'),
                        dict(label='x'*129)):
            with self.subTest(changes=changes), self.assertRaises(ValueError): canonical_items([item(**changes)])
        self.assertEqual(canonical_items([item(months=960)])[0]['months'], 960)
        with self.assertRaises(ValueError): enriched(records=[item(label='Unlisted')])

    def test_required_preferred_or_and_stronger_uncertainty(self):
        p = enriched()
        for heading in ('Required','Preferred'):
            row = compare(f'**{heading}**\nExperience with Python or R', p)
            self.assertEqual(row['status'], 'supported')
            self.assertEqual(row['modality'], heading.lower())
            self.assertIn('own assessment', row['message'])
            self.assertTrue(any(f['field_path'].startswith(PREFIX) for f in row['profile_facts']))
            self.assertEqual(row['source']['job_id'], 11242)
        self.assertEqual(compare('**Required**\nExperience with Python and R', p)['status'], 'not_established')
        self.assertEqual(compare('**Required**\nWorking proficiency in Python or R', p)['status'], 'not_established')
        self.assertEqual(compare('**Required**\nAdvanced proficiency in R', p)['status'], 'unresolved')
        self.assertEqual(compare('**Required**\n3 years professional experience with R', p)['status'], 'unresolved')
        independent = enriched(records=[item(autonomy='independent')])
        self.assertEqual(compare('**Required**\nWorking proficiency in R', independent)['status'], 'supported')
        for quote in ('Working proficiency in R for scientific computing',
                      'Experience with R — advanced statistical modeling required'):
            self.assertEqual(compare('**Required**\n'+quote, independent)['status'], 'not_established')
        professional = enriched(records=[item(contexts=['professional'], autonomy='unknown', months=120)])
        self.assertEqual(compare('**Required**\nWorking proficiency in R', professional)['status'], 'not_established')

    def test_self_report_is_not_external_verification_or_an_admission_bonus(self):
        p = enriched()
        self.assertEqual(compared('**Required**\nExperience with R', p)[0]['status'], 'not_established')
        for ref in p['provenance']['field_sources']:
            if ref['field_path'].startswith(PREFIX): ref['source_kind'] = 'external_import'
        self.assertEqual(compare('**Required**\nExperience with R', p)['status'], 'not_established')
        self.assertEqual(compare('**Required**\nExperience with Docker', enriched())['status'], 'not_established')

    def test_conflicting_source_and_negative_candidate_evidence_stay_unresolved(self):
        p = enriched()
        self.assertEqual(compare('**Required**\nExperience with R preferred', p)['status'], 'unresolved')
        p['constraints']['hard_constraints'] = ['No experience with R']
        p['provenance']['field_sources'].append(dict(field_path='constraints.hard_constraints[0]', explicit=True))
        self.assertEqual(compare('**Required**\nExperience with R', p)['status'], 'unresolved')


class ItemExperienceWorkflowTests(transfer.ProfileReviewTransferTests):
    # Reuse the complete authenticated confirmation regressions with this field.
    def test_edit_review_apply_rename_delete_and_unrelated_details(self):
        page, form = self.review(dict(software_tools='R, Python', specialties='Image annotation',
            item_experience=json.dumps([item(field='software_tools'), item('Image annotation','specialties',item_id='b'*32)])))
        self.assertIn(b'about 6 months', page.body)
        self.assertIn(b'self-reported', page.body)
        before = self.current()
        self.assertNotIn('item_details', before['experience'])
        self.assertEqual(self.apply(form).status, 200)
        saved = self.current()
        self.assertEqual(len(saved['experience']['item_details']), 2)
        self.assertEqual(compare('**Required**\nExperience with R', saved)['status'], 'supported')
        self.assertEqual(compare('**Required**\nWorking proficiency in R', saved)['status'], 'not_established')
        renamed = deepcopy(saved['experience']['item_details']); renamed[0]['label'] = 'R programming'
        page, form = self.review(dict(software_tools='R programming, Python', item_experience=json.dumps(renamed)))
        self.assertEqual(self.apply(form).status, 200)
        self.assertEqual(self.current()['experience']['item_details'][0]['item_id'], 'a'*32)
        page, form = self.review(dict(software_tools='Python', item_experience=json.dumps(renamed[1:])))
        self.assertEqual(self.apply(form).status, 200)
        self.assertEqual(self.current()['experience']['item_details'], renamed[1:])
        self.assertEqual(self.current()['languages'], before['languages'])

    def test_editor_cancel_omitted_field_and_unchanged_detail_roundtrip(self):
        page, form = self.review(dict(software_tools='R', item_experience=json.dumps([item(field='software_tools')])))
        self.assertEqual(self.apply(form).status, 200)
        before = self.current()
        page, form = self.review(dict(city='Example City'))
        self.assertEqual(self.current(), before)  # Review / Cancel does not save.
        self.assertEqual(self.apply(form).status, 200)
        self.assertEqual(self.current()['experience']['item_details'], before['experience']['item_details'])

    def test_identity_cannot_be_reattached_to_another_present_item(self):
        p = enriched()
        with self.assertRaises(ValueError):
            with_reviewed_items(p, json.dumps([item(label='Python')]), p)
        with self.assertRaises(ValueError):
            with_reviewed_items(p, json.dumps([item(field='specialties', label='Image annotation')]), p)


class ItemExperienceReuseTests(unittest.TestCase):
    def test_current_profile_invalidates_old_run_without_membership_or_score_change(self):
        from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
        from wahojobs import authenticated_profile_matches as browser
        fixture = SyntheticMatcherFixture(); self.addCleanup(fixture.close)
        self.assertEqual(fixture.get().status, 200)
        run = fixture.last_run(); original = run.recommendation_context
        label = fixture.profile['skills']['normalized'][0]
        fixture.profile = enriched(fixture.profile, [item(label)])
        with patch.object(browser.profile_preview, 'build_preview_context_from_canonical_rows',
                          wraps=browser.profile_preview.build_preview_context_from_canonical_rows) as compute:
            self.assertEqual(fixture.get('/find-matches?run='+run.match_run_id).status, 200)
            self.assertEqual(compute.call_count, 1)
        new = fixture.last_run().recommendation_context
        fingerprint = lambda c: [(m['job_id'], m['score']) for m in browser._primary_presentation_matches(c)]
        self.assertEqual(fingerprint(original), fingerprint(new))
        self.assertNotEqual(original['_authenticated_reuse']['inputs'], new['_authenticated_reuse']['inputs'])


class ItemExperienceResumeTests(resume_support.ProfileCorrectionResumeTests):
    def proposal(self):
        page, form = self.t.review(dict(country='Brazil', city='Example City',
            software_tools='R', job_titles='', item_experience=json.dumps([item(field='software_tools')])) )
        ref = dict(form['fields'])['draft']
        prepared = self.browser._correction_registry.peek(ref).recommendation_context['correction_preparation']
        return page, form, prepared.profile_for_browser()

    def test_pre_extension_retained_updates_still_resume_without_rewriting_storage(self):
        import sqlite3, hashlib
        from contextlib import closing
        from wahojobs.profile_correction_drafts import encode
        self.t.review(dict(city='Example City'))
        path=self.f.path.with_name(self.f.path.name+'.correction-drafts.sqlite3')
        with closing(sqlite3.connect(path)) as c, c:
            for ref, raw in c.execute('SELECT draft_reference,payload_json FROM product_profile_correction_drafts').fetchall():
                value=json.loads(raw);value['updates'].pop('item_experience')
                raw=encode(value)
                c.execute('UPDATE product_profile_correction_drafts SET payload_json=?,payload_sha256=? WHERE draft_reference=?',
                          (raw,hashlib.sha256(raw.encode()).hexdigest(),ref))
        before=path.read_bytes()
        self.assertEqual(self.landing().status,200)
        self.assertEqual(path.read_bytes(),before)


if __name__ == '__main__': unittest.main()
