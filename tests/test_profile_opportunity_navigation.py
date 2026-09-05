"""Existing confirmation workflow; all accounts and writes are disposable."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from tests.test_candidate_condition_comparisons import prepared, profile
from tests import test_persistent_profile_corrections as correction_tests
from wahojobs.profile_opportunity_navigation import (
    safe_opportunity_return, render_profile_update, correction_entry_navigation,
)


RETURN = '/job/opportunity-3809?variant=11242'


class ProfileOpportunityNavigationTests(unittest.TestCase):
    def test_safe_exact_variant_return_allowlist(self):
        self.assertEqual(safe_opportunity_return(RETURN), RETURN)
        self.assertEqual(safe_opportunity_return(RETURN+'&run='+'a'*24), RETURN+'&run='+'a'*24)
        for value in (None, 'https://evil.test/', '//evil.test/', '/\\evil.test/', '/action',
                      '/account/profile', '/job/opportunity-3809', RETURN+'&variant=1',
                      RETURN+'&return_to=/jobs', RETURN+'#x', RETURN+'%0d%0aLocation:x',
                      '/job/opportunity-3809?variant=0', '/job/opportunity-3809?variant=1&run=bad'):
            with self.subTest(value=value): self.assertIsNone(safe_opportunity_return(value))

    def test_only_missing_supported_personal_fields_get_optional_action(self):
        html = render_profile_update(prepared(),RETURN)
        self.assertEqual(html.count('>Update profile</a>'),1)
        self.assertIn('focus=software_tools',html)
        self.assertIn('(optional)',html)
        # The benchmark's known education and unknown hourly capacity have no
        # personal field that this editor can use to settle the question.
        self.assertEqual(render_profile_update(prepared(identity=11271),RETURN),'')
        p=profile();p['education']={}
        self.assertIn('focus=education',render_profile_update(prepared('**Required**\nPhD in Biology',p),RETURN))
        for text in ('**Required**\nWorking proficiency in Python or R',
                     '**Required**\nPhD in Chemistry or a closely related field',
                     '**Engagement**\nCommitment: 20+ hours/week'):
            self.assertEqual(render_profile_update(prepared(text),RETURN),'')
        self.assertEqual(render_profile_update(None,RETURN),'')

    def test_unsupported_focus_and_external_destinations_are_rejected(self):
        for focus in ('availability','country_eligibility','currency','source','education&admin=1'):
            with self.assertRaises(ValueError):
                correction_entry_navigation('/account/profile?'+urlencode({'correction':'start','return_to':RETURN,'focus':focus}))
        with self.assertRaises(ValueError):
            correction_entry_navigation('/account/profile?correction=start&return_to=https://evil.test/')


class ExistingProfileReturnWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f=correction_tests.PersistentProfileCorrectionTests()
        self.f.setUp();self.addCleanup(self.f.tearDown)
        self.browser=self.f._build_browser()

    def get(self,target,session=None):
        return self.browser.handle('GET',target,self.f._browser_headers(session or self.f.session))

    def count(self):
        connection=self.f._connection()
        try:return connection.execute('SELECT count(*) FROM product_profile_revisions').fetchone()[0]
        finally:connection.close()

    def start(self,focus='software_tools'):
        entry='/account/profile?'+urlencode({'correction':'start','return_to':RETURN,'focus':focus})
        start=self.get(entry);self.assertEqual(start.status,200)
        self.assertIn('Cancel and return',start.body.decode())
        form=self.f._form(start,'intent','return_to')
        created,_=self.f._post_form(self.browser,form['action'],form['fields'])
        self.assertEqual(created.status,303)
        target=self.f._response_header(created,'Location')
        review=self.get(target)
        edit=next(href for href in self.f._markup(review).links if 'correction=edit' in href)
        return target,self.get(edit)

    def offer(self,changes=()):
        _,edit=self.start();self.assertEqual(edit.status,200)
        form=self.f._form(edit,'edit_run_id')
        fields=self.f._set_form_field(form['fields'],'credentials_confirmed','1')
        for name,value in changes:fields=self.f._set_form_field(fields,name,value)
        redraft,_=self.f._post_form(self.browser,form['action'],fields)
        self.assertEqual(redraft.status,303)
        reviewed=self.get(self.f._response_header(redraft,'Location'))
        confirm=self.f._form(reviewed,'draft')
        absent,_=self.f._post_form(self.browser,confirm['action'],confirm['fields'])
        self.assertEqual(absent.status,400)
        fields=self.f._set_form_field(confirm['fields'],'confirmed','1')
        response,_=self.f._post_form(self.browser,confirm['action'],fields)
        self.assertEqual(response.status,200)
        return response,self.f._form(response,'artifact','csrf','return_to')

    def test_start_edit_and_cancel_do_not_save_or_prefill_job_answers(self):
        before=self.count();target,edit=self.start()
        self.assertEqual(self.count(),before)
        self.assertIn('<details class="review-more" open>',edit.body.decode())
        self.assertRegex(edit.body.decode(),r'id="software_tools"[^>]*autofocus')
        self.assertIn(RETURN.replace('&','&amp;'),edit.body.decode())
        self.assertNotIn('20+ hours',edit.body.decode())
        # Cancel is a plain local GET link; no profile mutation endpoint.
        self.assertIn(RETURN,self.f._markup(edit).links)
        self.assertEqual(self.count(),before)

    def test_confirm_then_apply_creates_one_revision_and_returns_exact_variant(self):
        before=self.count()
        original=self.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        response,form=self.offer([('software_tools','R, Docker')])
        self.assertEqual(self.count(),before)
        applied,_=self.f._post_form(self.browser,form['action'],form['fields'])
        self.assertEqual(applied.status,303)
        self.assertEqual(self.f._response_header(applied,'Location'),RETURN)
        self.assertEqual(self.count(),before+1)
        saved=self.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        self.assertEqual(saved['education'],original['education'])
        packet=prepared(p=saved)
        option=next(r for r in packet['comparisons'] if 'accepted tool option' in r['message'])
        self.assertIn('tool mention: R',option['supported_parts'])
        self.assertEqual(option['status'],'not_established')
        self.assertIn('requested proficiency',option['message'])
        self.assertTrue(any(ref.get('explicit') for fact in option['profile_facts'] for ref in fact['sources']))
        self.assertEqual(packet['geography'],prepared(p=original)['geography'])
        replay,_=self.f._post_form(self.browser,form['action'],form['fields'])
        self.assertEqual(replay.status,303)
        self.assertEqual(self.count(),before+1)

    def test_removing_tool_fact_restores_unknown_not_contradiction(self):
        for value in ('R',''):
            _,form=self.offer([('software_tools',value)])
            response,_=self.f._post_form(self.browser,form['action'],form['fields'])
            self.assertEqual(response.status,303)
        saved=self.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        rows=prepared(p=saved)['comparisons']
        option=next(r for r in rows if r['kind']=='tools')
        self.assertEqual(option['status'],'not_established')
        self.assertNotIn('tool mention: R',option['supported_parts'])

    def test_education_focus_does_not_invent_structured_degree_pairing(self):
        _,edit=self.start('education')
        self.assertRegex(edit.body.decode(),r'id="degrees"[^>]*autofocus')
        form=self.f._form(edit,'edit_run_id');fields=[item for item in form['fields'] if item[0] != 'no_degree']
        for name,value in [('degrees','PhD in Molecular Biology'),('education_fields','Molecular Biology'),
                           ('education_level','doctorate'),('education_status','completed'),('hard_constraints',''),('credentials_confirmed','1')]:
            fields=self.f._set_form_field(fields,name,value)
        redraft,_=self.f._post_form(self.browser,form['action'],fields);self.assertEqual(redraft.status,303)
        reviewed=self.get(self.f._response_header(redraft,'Location'))
        form=self.f._form(reviewed,'draft');fields=self.f._set_form_field(form['fields'],'confirmed','1')
        offer,_=self.f._post_form(self.browser,form['action'],fields);form=self.f._form(offer,'artifact')
        applied,_=self.f._post_form(self.browser,form['action'],form['fields']);self.assertEqual(applied.status,303)
        saved=self.f._current().trusted_dict(include_structured_profile=True)['structured_profile']
        self.assertIn('PhD in Molecular Biology',saved['education']['degrees'])
        self.assertNotIn('entries',saved['education'])
        row=next(r for r in prepared(identity=11271,p=saved)['comparisons'] if r['kind']=='education')
        self.assertEqual(row['status'],'not_established')

    def test_cancel_after_confirmation_does_not_apply(self):
        before=self.count();response,_=self.offer([('software_tools','R')])
        self.assertIn(RETURN,self.f._markup(response).links)
        self.assertEqual(self.count(),before)

    def test_foreign_session_cannot_read_draft_or_apply_offer(self):
        target,_=self.start();other=self.f._new_session('92')
        self.assertIn(self.get(target,other).status,(404,410))
        _,form=self.offer([('software_tools','R')]);before=self.count()
        denied,_=self.f._post_form(self.browser,form['action'],form['fields'],session=other)
        self.assertIn(denied.status,(403,404,410));self.assertEqual(self.count(),before)

    def test_bad_apply_return_is_rejected_before_profile_write(self):
        _,form=self.offer([('software_tools','R')]);before=self.count()
        fields=self.f._set_form_field(form['fields'],'return_to','https://evil.test/')
        denied,_=self.f._post_form(self.browser,form['action'],fields)
        self.assertEqual(denied.status,400);self.assertEqual(self.count(),before)

    def test_old_run_reads_confirmed_revision_and_stale_draft_is_rejected(self):
        self.f._seed_inventory()
        self.browser=self.f._build_browser(with_matches=True)
        matches=self.browser._matches_integration
        # Enable the existing owner-bound in-memory run path; tracking is not
        # involved in this test and no matching write is permitted.
        matches._write_connection_provider=lambda: self.fail('unexpected match write')
        with patch.object(type(matches),'_load_pipeline_records',return_value=[]):
            first=self.get('/find-matches');self.assertEqual(first.status,200)
            old=next(reversed(matches._registry._runs.values()))
            stale_draft,_=self.start()
            _,form=self.offer([('software_tools','R')])
            response,_=self.f._post_form(self.browser,form['action'],form['fields'])
            self.assertEqual(response.status,303)
            self.assertEqual(self.get(stale_draft).status,409)
            old_url='/find-matches?run='+old.match_run_id
            reused=self.get(old_url);self.assertEqual(reused.status,200)
            latest=next(reversed(matches._registry._runs.values()))
            self.assertNotEqual(latest.match_run_id,old.match_run_id)
            self.assertNotEqual(latest.recommendation_context['_authenticated_reuse']['inputs'],
                                old.recommendation_context['_authenticated_reuse']['inputs'])
            fresh=self.get('/find-matches');self.assertEqual(fresh.status,200)
            current=next(reversed(matches._registry._runs.values()))
            self.assertEqual(current.recommendation_context['matches'],latest.recommendation_context['matches'])


if __name__=='__main__':unittest.main()
