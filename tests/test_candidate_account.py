"""Candidate-only production composition with synthetic, offline provider identities."""
from datetime import datetime, timedelta
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from tests.test_public_job_page import seed_public_job, OBSERVED_AT
from tests.test_public_catalog_reader import BODY
from tests.workos_authkit_test_support import build_m008, connect, MutableClock, FakeWorkOSBoundary, CLIENT_ID
from wahojobs.candidate_account import CandidateAccountIntegration, SESSION, CSRF, LOGIN_CSRF, CONTEXT, ORIGIN, safe_return
from wahojobs.public_catalog_reader import PublicCatalogReader, preparation_metadata
from wahojobs import public_jobs_catalog as catalog
from wahojobs.trusted_login_completion import create_workos_authkit_trusted_login_completion_policy
from wahojobs.workos_authkit import WorkOSAuthKitConfiguration, WorkOSAuthKitGateway
from wahojobs.remote_beta import RemoteBetaIntegration


class CandidateAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'product.sqlite3'
        db = build_m008(self.path)
        seed_public_job(db)
        db.execute("UPDATE job_source_contents SET provider='acme-ai', body=?",(BODY,))
        db.commit()
        self.clock = MutableClock(datetime.fromisoformat(OBSERVED_AT))
        metadata,generation = preparation_metadata(db)
        self.reader = PublicCatalogReader(catalog.load_public_jobs(db,now=self.clock()),clock=self.clock,
            metadata=metadata,generation=generation,candidate_enabled=True)
        db.close()
        self.boundary = FakeWorkOSBoundary()
        self.integration = self.make_integration()
        self.addCleanup(lambda:self.integration.close())
        self.cookies = {}
        self.target = '/jobs/opportunity-9002?'+urlencode({'variant':'9003','return_to':'/jobs?location=Brazil&page=2'})

    def make_integration(self, client=CLIENT_ID):
        gateway = WorkOSAuthKitGateway(configuration=WorkOSAuthKitConfiguration(client_id=client,
            redirect_uri=ORIGIN+'/candidate/auth/callback',environment_namespace='production',public_candidate_registration=True),
            boundary=self.boundary,invitation_lookup_key=secrets.token_bytes(32),clock=self.clock)
        return CandidateAccountIntegration(connection_factory=lambda:connect(self.path),gateway=gateway,
            completion_policy=create_workos_authkit_trusted_login_completion_policy(environment_namespace='production',
                idle_ttl=timedelta(hours=1),absolute_ttl=timedelta(hours=8)),public_reader=self.reader,client_id=client,clock=self.clock)

    def request(self, method, target, form=None, *, cookies=None, extra=(), deliver=True):
        jar = self.cookies if cookies is None else cookies
        headers = [('Host','www.wahojobs.com'),('Cookie','; '.join(k+'='+v for k,v in jar.items()))]+list(extra)
        stream=None
        if method=='POST':
            raw=urlencode(form or {}).encode()
            headers += [('Origin',ORIGIN),('Content-Type','application/x-www-form-urlencoded'),('Content-Length',str(len(raw)))]
            stream=BytesIO(raw)
        response = self.integration.handle(method,target,tuple(headers),stream)
        result=(response.status,response.body,dict(response.headers))
        if deliver:
            for name,value in response.headers:
                if name=='Set-Cookie':
                    key,raw=value.split(';',1)[0].split('=',1)
                    if raw: jar[key]=raw
                    else: jar.pop(key,None)
            response.acknowledge_delivery()
        else:
            response.fail_delivery()
        return result

    def count(self, table):
        with closing(connect(self.path)) as db:
            if table=='match_runs' and not db.execute("SELECT 1 FROM sqlite_schema WHERE name='match_runs'").fetchone():
                return 0
            return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def login(self, *, cookies=None, subject=None):
        jar=self.cookies if cookies is None else cookies
        if subject: self.boundary.subject=subject
        status,_,_=self.request('GET','/candidate/login',cookies=jar)
        self.assertEqual(status,200)
        status,_,headers=self.request('POST','/candidate/auth/start',{'csrf':jar[LOGIN_CSRF]},cookies=jar)
        self.assertEqual(status,303)
        state=parse_qs(urlsplit(headers['Location']).query)['state'][0]
        callback='/candidate/auth/callback?'+urlencode({'code':'synthetic_auth_code_123','state':state})
        result=self.request('GET',callback,cookies=jar)
        self.assertEqual(result[0],303,result[1])
        return result,callback

    def states(self, *, cookies=None):
        status,body,headers=self.request('GET','/candidate/state?ids=9002',cookies=cookies)
        self.assertEqual(status,200,body)
        self.assertEqual(headers['Cache-Control'],'private, no-store')
        return json.loads(body)

    def action(self, name, *, version=None, key=None, variant=9003, cookies=None):
        jar=self.cookies if cookies is None else cookies
        state=self.states(cookies=jar)
        current=state['states'].get('9002',{})
        form=dict(csrf=state['csrf'],action=name,canonical='9002',variant=str(variant),
            version=str(current.get('version',0) if version is None else version),key=key or secrets.token_urlsafe(32),return_to=self.target)
        return self.request('POST','/candidate/intent',form,cookies=jar),form

    def test_anonymous_my_jobs_explains_benefit_without_profile_or_beta_access(self):
        status,_,headers=self.request('GET','/my-jobs?view=saved')
        self.assertEqual(status,303)
        self.assertIn('/candidate/login?',headers['Location'])
        self.assertEqual(self.count('users'),0)
        for path in ['/find-matches','/account/profile','/tracker','/user','/api/auth/signin','/candidate/find-matches']:
            self.assertEqual(self.request('GET',path)[0],404)

    def test_public_registration_and_exact_anonymous_intent_use_no_invitation_or_match(self):
        response,form=self.action('save')
        self.assertEqual(response[0],303)
        self.assertEqual(self.count('user_pipeline_items'),0)
        (status,_,headers),callback=self.login()
        self.assertEqual(headers['Location'],'/candidate/resume')
        self.assertEqual(self.count('account_invitations'),0)
        self.assertEqual(self.count('user_profiles'),0)
        self.assertEqual(self.request('GET','/candidate/resume')[0],200)
        self.assertEqual(self.count('user_pipeline_items'),0,'GET cannot perform tracking writes')
        status,_,headers=self.request('POST','/candidate/resume',{'csrf':self.cookies[CSRF]})
        self.assertEqual(status,303)
        self.assertEqual(headers['Location'],self.target)
        self.assertEqual(self.states()['states']['9002']['workflow_status'],'saved')
        self.assertEqual(self.count('user_profiles'),1)
        self.assertEqual(self.count('match_runs'),0)
        with closing(connect(self.path)) as db:
            row=db.execute('SELECT * FROM user_profiles').fetchone()
            self.assertEqual(row['display_name'],'Candidate')
            self.assertEqual(row['is_sample'],0)
            subject=db.execute('SELECT provider_subject FROM auth_identities').fetchone()[0]
            self.assertEqual(subject,'production:'+CLIENT_ID+':'+self.boundary.subject)
        self.assertEqual(self.request('POST','/candidate/resume',{'csrf':self.cookies[CSRF]})[0],409)
        self.assertEqual(self.request('GET',callback)[0],403)

    def test_saved_applied_correction_hidden_restoration_history_and_views(self):
        self.login()
        for action,wanted in [('save','saved'),('applied','applied'),('undo_applied','saved')]:
            response,_=self.action(action)
            self.assertEqual(response[0],303,response[1])
            self.assertEqual(self.states()['states']['9002']['workflow_status'],wanted)
        response,_=self.action('not_interested')
        self.assertEqual(response[0],303,response[1])
        self.assertEqual(self.states()['states']['9002']['visibility'],'hidden')
        status,body,_=self.request('GET','/my-jobs?view=hidden')
        self.assertEqual(status,200,body)
        self.assertIn(b'Show again',body)
        self.assertIn(b'History',body)
        self.assertNotIn(b'/find-matches',body)
        response,_=self.action('undo_discovery')
        self.assertEqual(response[0],303,response[1])
        self.assertEqual(self.states()['states']['9002']['workflow_status'],'saved')
        self.assertEqual(self.states()['states']['9002']['visibility'],'visible')
        for view in ['all','saved','in_progress','hidden']:
            self.assertEqual(self.request('GET','/my-jobs?view='+view)[0],200)
        self.assertEqual(self.count('user_pipeline_items'),1)
        self.assertGreater(self.count('user_pipeline_transitions'),3)

    def test_unsave_returns_to_browse_and_direct_applied_correction_is_not_fabricated_saved(self):
        self.login()
        for name,wanted in [('applied','applied'),('undo_applied','recommended'),('save','saved'),('unsave','recommended')]:
            reply,_=self.action(name)
            self.assertEqual(reply[0],303,reply[1])
            self.assertEqual(self.states()['states']['9002']['workflow_status'],wanted)

    def test_duplicate_retry_stale_tab_and_applied_save_cannot_overwrite_progress(self):
        self.login()
        response,form=self.action('save')
        self.assertEqual(response[0],303)
        before=self.states()['states']['9002']
        self.assertEqual(self.request('POST','/candidate/intent',form)[0],303)
        self.assertEqual(before,self.states()['states']['9002'])
        self.assertEqual(self.action('applied',version=0)[0][0],409)
        self.assertEqual(self.action('applied')[0][0],303)
        self.assertEqual(self.action('save')[0][0],409)
        self.assertEqual(self.states()['states']['9002']['workflow_status'],'applied')

    def test_two_candidates_same_email_never_link_and_foreign_fields_are_rejected(self):
        self.login()
        self.action('save')
        second={}
        self.login(cookies=second,subject='user_other0123456789abcd')
        self.assertEqual(self.states(cookies=second)['states'],{})
        self.assertEqual(self.count('users'),2)
        self.assertEqual(self.request('GET','/candidate/state?ids=9002',cookies=second,extra=(('X-Owner-Id','one'),))[0],400)
        _,form=self.action('applied',cookies=second)
        form['owner']='someone-else'
        self.assertEqual(self.request('POST','/candidate/action',form,cookies=second)[0],409)
        self.assertEqual(self.states()['states']['9002']['workflow_status'],'saved')
        self.assertEqual(self.states(cookies=second)['states']['9002']['workflow_status'],'applied')

    def test_candidate_and_employer_cookies_and_logout_are_scoped(self):
        self.cookies.update({'__Secure-next-auth.session-token':'employer-session','wahojobs_session':'beta-session'})
        self.login()
        self.action('save')
        csrf=self.cookies[CSRF]
        status,_,headers=self.request('POST','/candidate/logout',{'csrf':csrf})
        self.assertEqual(status,303)
        self.assertEqual(self.cookies,{'__Secure-next-auth.session-token':'employer-session','wahojobs_session':'beta-session'})
        self.assertEqual(self.states()['states'],{})
        self.login()
        self.assertEqual(self.states()['states']['9002']['workflow_status'],'saved')
        self.assertEqual(self.count('users'),1)

    def test_cancel_failed_and_expired_login_do_not_track(self):
        self.action('save')
        self.boundary.fail_exchange=True
        self.request('GET','/candidate/login')
        status,_,headers=self.request('POST','/candidate/auth/start',{'csrf':self.cookies[LOGIN_CSRF]})
        state=parse_qs(urlsplit(headers['Location']).query)['state'][0]
        callback='/candidate/auth/callback?'+urlencode({'code':'synthetic_auth_code_123','state':state})
        self.assertEqual(self.request('GET',callback)[0],403)
        self.assertEqual(self.count('user_pipeline_items'),0)
        self.clock.advance(timedelta(minutes=11))
        self.assertEqual(self.request('POST','/candidate/auth/start',{'csrf':self.cookies[LOGIN_CSRF]})[0],409)
        self.assertEqual(self.count('users'),0)

    def test_csrf_origin_methods_query_and_unverified_identity_fail_closed(self):
        self.login()
        state=self.states()
        for target in ['/candidate/logout','/candidate/intent','/candidate/auth/start']:
            self.assertEqual(self.request('GET',target)[0],405)
        self.assertEqual(self.request('POST','/candidate/logout',{'csrf':'forged'})[0],403)
        self.assertEqual(self.request('POST','/candidate/logout',{'csrf':state['csrf']},extra=(('Origin','https://evil.test'),))[0],403)
        self.assertEqual(self.request('GET','/my-jobs?owner=other')[0],409)
        self.assertEqual(self.request('GET','/candidate/login?return_to=https%3A%2F%2Fevil.test',cookies={})[0],409)
        self.assertEqual(self.request('GET','/my-jobs',cookies={'__Secure-next-auth.session-token':'employer'})[0],303)

    def test_restart_source_closure_inventory_and_delivery_failure_preserve_authority(self):
        self.login()
        self.action('save')
        before=self.states()['states']['9002']
        self.integration.close()
        self.integration=self.make_integration()
        self.assertEqual(before,self.states()['states']['9002'])
        with closing(connect(self.path)) as db:
            db.execute('UPDATE jobs SET is_active=0 WHERE id=9003')
            db.execute('UPDATE canonical_opportunities SET is_active=0 WHERE id=9002')
            db.commit()
            self.reader._prepared=()
            self.reader._refresh(self.clock())
        status,body,_=self.request('GET','/my-jobs')
        self.assertEqual(status,200,body)
        self.assertIn(b'Availability unconfirmed',body)
        self.assertNotIn(b'>Original listing</a>',body)
        self.assertEqual(self.action('applied')[0][0],409)
        self.assertEqual(self.action('not_interested')[0][0],303)
        self.assertEqual(self.action('show_again')[0][0],303)
        self.assertEqual(self.count('user_pipeline_items'),1)

    def test_public_reader_is_personal_state_free_and_catalog_key_cannot_write(self):
        before=self.reader.handle('GET','/jobs',(('Host','www.wahojobs.com'),)).body
        self.login()
        self.action('not_interested')
        after=self.reader.handle('GET','/jobs',(('Host','www.wahojobs.com'),('Cookie',SESSION+'='+self.cookies[SESSION]))).body
        self.assertEqual(before,after)
        self.assertNotIn(self.boundary.email.encode(),after)
        remote=RemoteBetaIntegration(Mock(),public_catalog=self.reader,catalog_key='a'*64,candidate=self.integration,candidate_key='b'*64)
        headers=(('Host','beta.wahojobs.com'),('X-Wahojobs-Catalog-Key','a'*64))
        for path in ['/_catalog/candidate/action','/_candidate/candidate/action']:
            self.assertIn(remote.handle('POST',path,headers,BytesIO(b'bad')).status,(404,405))

    def test_no_matching_scoring_or_collection_on_candidate_journey(self):
        with patch('wahojobs.authenticated_profile_matches.AuthenticatedProfileMatchesService.find_matches',
                   side_effect=AssertionError('Matching invoked'),create=True) as matches:
            self.login()
            for name in ['save','applied','undo_applied','not_interested','show_again','unsave']:
                self.assertEqual(self.action(name)[0][0],303)
            self.assertEqual(self.request('GET','/my-jobs')[0],200)
            matches.assert_not_called()
        self.assertEqual(self.count('match_runs'),0)


class SafeReturnTests(unittest.TestCase):
    def test_allowlist_preserves_safe_catalog_context(self):
        for target in ['/jobs','/jobs?q=Python&page=2','/my-jobs?view=in_progress',
                '/jobs/opportunity-9002?variant=9003&return_to=%2Fjobs%3Flocation%3DBrazil%26page%3D2']:
            self.assertEqual(safe_return(target),target)
        for target in ['//evil.test','https://evil.test','/user','/find-matches','/jobs#x','/jobs?owner=a',
                '/jobs?%zz','/jobs?q=a&q=b','/my-jobs?view=progress','/jobs/opportunity-9002?return_to=%2Fuser']:
            self.assertIsNone(safe_return(target))
