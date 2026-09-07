"""Real localhost login/callback delivery; synthetic disposable owners only."""
import sqlite3
import unittest
import tempfile
from pathlib import Path
from contextlib import closing

from scripts.local_recovery_login import existing_owner_local_login
from tests.durable_google_login_browser_test_support import (
    temporary_browser_login_state, _running_https_browser_handler,
    https_request, cookie_values, cookie_header, form_body,
)
from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler


class LocalRecoveryLoginTests(unittest.TestCase):
    def test_current_intake_schema_is_accepted_but_drift_and_temp_shadow_are_not(self):
        from tests.ai_profile_import_test_support import install_ai_profile_import_database
        from wahojobs.google_oidc_authorization_transaction_repository import _attest, _RepositoryFailure
        with tempfile.TemporaryDirectory() as directory:
            connection,_ = install_ai_profile_import_database(Path(directory)/'current.sqlite3')
            try:
                _attest(connection)
                connection.execute('CREATE TEMP TABLE unexpected_shadow(value TEXT)')
                with self.assertRaises(_RepositoryFailure): _attest(connection)
                connection.execute('DROP TABLE temp.unexpected_shadow')
                _attest(connection)
                connection.execute('DROP INDEX idx_google_oidc_authorization_transactions_prepared_expiry')
                with self.assertRaises(_RepositoryFailure): _attest(connection)
            finally:
                connection.close()
                # Existing migration fixture builders can retain temporary
                # connection cycles; release them before Windows file cleanup.
                import gc
                gc.collect()

    def test_reentry_expiry_logout_safe_return_and_preservation(self):
        with temporary_browser_login_state(port=8816) as state:
            def protected():
                with closing(sqlite3.connect(state.database_path)) as c:
                    return {t:c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()
                            for t in ('product_profiles','product_profile_revisions',
                                      'principal_account_bindings','ownership_binding_events')}
            before=protected()
            with existing_owner_local_login(state.configuration_path, account_id=state.account_id, clock=state.clock) as (config, app):
                with _running_https_browser_handler(config, make_durable_product_browser_handler(app)):
                    cookies={}
                    def request(method,target,body=None,jar=None):
                        jar=cookies if jar is None else jar
                        headers=[('Cookie',cookie_header(jar))]
                        if method=='POST':
                            headers += [('Origin',state.public_origin),('Sec-Fetch-Site','same-origin'),('Content-Type','application/x-www-form-urlencoded'),('Content-Length',str(len(body)))]
                        response=https_request(state,method,target,headers=tuple(headers),body=body)
                        for key,value in cookie_values(response).items():
                            if value: jar[key]=value
                            else: jar.pop(key,None)
                        return response
                    def login(next_path='/account/profile'):
                        self.assertEqual(request('GET','/login?next='+next_path).status,200)
                        started=request('POST','/auth/google/start',form_body(csrf=cookies['__Host-wahojobs_login_csrf']))
                        self.assertEqual(started.header_values('Location'),('/__fixture/google/approve',))
                        self.assertEqual(request('GET','/__fixture/google/approve').status,200)
                        complete=request('GET','/__fixture/google/complete')
                        callback=request('GET',complete.header_values('Location')[0])
                        self.assertEqual(callback.status,303)
                        return callback.header_values('Location')[0]
                    unauth=request('GET','/account/profile')
                    self.assertEqual(unauth.status,401)
                    self.assertIn(b'/login',unauth.body)
                    self.assertEqual(login(),'/account/profile')
                    self.assertEqual(request('GET','/account/profile').status,200, list(cookies))
                    with closing(sqlite3.connect(state.database_path)) as c:
                        self.assertEqual(c.execute('SELECT DISTINCT user_id FROM account_sessions').fetchall(),[(state.account_id,)])
                    first=dict(cookies)
                    state.clock.advance(3601)
                    self.assertEqual(request('GET','/account/profile',jar=first).status,401)
                    self.assertEqual(login('/find-matches'),'/find-matches')
                    self.assertNotEqual(first['wahojobs_session'],cookies['wahojobs_session'])
                    self.assertEqual(request('GET','/account/profile',jar=first).status,401)
                    self.assertEqual(request('GET','/logout').status,200)
                    previous=dict(cookies)
                    self.assertEqual(request('POST','/logout',form_body(csrf=cookies['__Host-wahojobs_session_csrf'])).status,303)
                    self.assertEqual(request('GET','/account/profile',jar=previous).status,401)
                    self.assertEqual(login('//external.invalid'),'/find-matches')
                    self.assertEqual(request('GET','/account/profile').status,200)
                    self.assertEqual(protected(),before)

    def test_unknown_owner_cannot_select_another_identity(self):
        with temporary_browser_login_state(port=8816) as state:
            with self.assertRaisesRegex(ValueError,'existing_local_identity_required'):
                with existing_owner_local_login(state.configuration_path,account_id='usr_'+('0'*32),clock=state.clock):
                    self.fail('must not start for a different owner')


if __name__=='__main__':
    unittest.main()
