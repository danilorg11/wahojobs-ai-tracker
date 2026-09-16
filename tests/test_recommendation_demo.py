"""Normal synthetic invited sign-in, isolated simultaneous approvals and real forms."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from tests.private_beta_demo_support import beta_state, beta_application, preserve_fresh_fixture, reopen_fixture
from tests.private_beta_matching_support import CLOCK
from tests.recommendation_demo_support import configure_samples, SAMPLE_KEYS
from tests.candidate_continuity_support import Page
from tests.candidate_decision_support import verified_https_request
from tests.durable_google_login_browser_test_support import cookie_header, cookie_values
from tests.test_candidate_continuity_client import run_client
from tests.test_first_time_candidate import observe


def sample_client(state, key, *, returning=False, switch_sample=None):
    marker=json.loads((state.directory/'private-beta-demo.json').read_text())
    sample=marker['recommendation_samples'][key]
    fixture=dict(sample,practice_sample=key,sample_preparation=True,sample_return=returning,switch_sample=switch_sample)
    return run_client(state,'owner-return' if returning else 'owner-correction',
        script=Path(__file__).with_name('first_time_candidate_client.cjs'),observe=observe,fixture=fixture)


class RecommendationDemoTests(unittest.TestCase):
    def test_prepared_samples_use_normal_text_confirmation_and_later_login(self):
        with tempfile.TemporaryDirectory(prefix='recommendation-sample-') as directory:
            from tests.candidate_continuity_support import reserve_port
            state=preserve_fresh_fixture(Path(directory).resolve()/'samples',port=reserve_port(),samples=True)
            state=reopen_fixture(state.directory)
            self.assertEqual(state.clock(),CLOCK)
            with patch('tests.test_candidate_continuity_client.https_request',verified_https_request), beta_application(state):
                for key in ('alex','biology','software'):
                    before=observe(state)
                    result=sample_client(state,key)
                    points={row['label']:row for row in result['observations']}
                    self.assertEqual(len(points['sample-final-review']['state']['profiles']),len(before['profiles']))
                    if key!='alex':
                        self.assertEqual(points['sample-reviewed-facts']['state']['profiles'],before['profiles'])
                    after=observe(state)
                    self.assertEqual(len(after['profiles']),len(before['profiles'])+1)
                    self.assertEqual(len(after['revisions']),len(before['revisions'])+1)
                    previous={row['revision_id'] for row in before['revisions']}
                    created=next(row for row in after['revisions'] if row['revision_id'] not in previous)
                    profile=json.loads(created['structured_profile_json'])
                    self.assertEqual(profile['location']['country'],{'alex':'Brazil','biology':'Canada','software':'Germany'}[key])
                    if key=='biology':
                        self.assertEqual(profile['education']['education_level'],'doctorate')
                        self.assertIn('biology',profile['education']['fields_or_domains'])
                        self.assertIn('microbiology',profile['skills']['normalized'])
                        self.assertGreaterEqual(len(points['sample-matches']['matchCards']),2)
                    elif key=='software':
                        self.assertIn('python',profile['skills']['normalized'])
                        self.assertIn('software engineering',profile['experience']['professional_domains'])
                    else:
                        self.assertEqual(profile['experience']['years_by_domain'],[{'domain':'customer support','years':2}])
                        self.assertEqual(profile['preferences']['preference_model']['workloads'],['part_time'])
                    self.assertEqual(result['clientErrors'],[])
                    returned=sample_client(state,key,returning=True)
                    self.assertEqual(observe(state)['revisions'],after['revisions'])
                    self.assertTrue(returned['observations'])
                self.assertEqual(len(observe(state)['profiles']),3)
                revisions=observe(state)['revisions']
                switched=sample_client(state,'software',returning=True,switch_sample='biology')
                points={row['label']:row for row in switched['observations']}
                self.assertGreaterEqual(len(points['sample-switched-matches']['matchCards']),2)
                self.assertEqual(observe(state)['revisions'],revisions)
                with closing(sqlite3.connect(state.database_path)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_identities WHERE provider_subject='recommendation-practice-fresh'").fetchone()[0],0)

    def test_interleaved_approvals_keep_declared_identity_and_reject_replay(self):
        with beta_state(now=CLOCK) as state:
            configure_samples(state)
            with beta_application(state):
                jars={key:{} for key in ('alex','software')}
                def request(key,method,target,body=None):
                    headers=[('Cookie',cookie_header(jars[key]))]
                    if method=='POST':
                        headers.extend([('Origin',state.public_origin),('Sec-Fetch-Site','same-origin'),
                                        ('Content-Type','application/x-www-form-urlencoded'),('Content-Length',str(len(body)))])
                    result=verified_https_request(state,method,target,headers=tuple(headers),body=body)
                    for name,value in cookie_values(result).items():
                        if value:jars[key][name]=value
                        else:jars[key].pop(name,None)
                    return result
                for key in jars:
                    response=request(key,'GET','/login?practice='+key)
                    self.assertEqual(response.status,200)
                    page=Page(response.body)
                    form=next(f for f in page.forms if f['target']=='/auth/google/start')
                    response=request(key,'POST',form['target'],urlencode(form['fields']).encode())
                    self.assertEqual(response.status,303)
                    response=request(key,'GET','/__fixture/google/approve')
                    self.assertIn(key.encode() if key=='alex' else b'Software', response.body.lower() if key=='alex' else response.body)
                for key in reversed(jars):
                    response=request(key,'GET','/__fixture/google/complete')
                    self.assertEqual(response.status,303)
                    callback=next(v for k,v in response.headers if k.lower()=='location')
                    self.assertEqual(request(key,'GET',callback).status,303)
                    self.assertEqual(request(key,'GET','/__fixture/google/complete').status,400)
                with closing(sqlite3.connect(state.database_path)) as connection:
                    actual={row[0] for row in connection.execute('SELECT provider_subject FROM auth_identities')}
                    self.assertEqual(actual,{'recommendation-practice-alex','recommendation-practice-software'})
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM product_profiles').fetchone()[0],0)
                self.assertEqual(request('alex','GET','/login?practice=unlisted').status,400)
                self.assertEqual(request('alex','GET','/login?practice=alex&practice=software').status,400)
                # A foreign pending approval cannot become a session in this
                # browser: the production callback still binds its own state.
                from tests.recommendation_demo_support import PENDING_COOKIE
                for key in jars:
                    jars[key].clear()
                    response=request(key,'GET','/login?practice='+key)
                    form=next(f for f in Page(response.body).forms if f['target']=='/auth/google/start')
                    self.assertEqual(request(key,'POST',form['target'],urlencode(form['fields']).encode()).status,303)
                with closing(sqlite3.connect(state.database_path)) as connection:
                    sessions_before=connection.execute('SELECT COUNT(*) FROM account_sessions').fetchone()[0]
                jars['alex'][PENDING_COOKIE]=jars['software'][PENDING_COOKIE]
                response=request('alex','GET','/__fixture/google/complete')
                self.assertEqual(response.status,303)
                callback=next(v for k,v in response.headers if k.lower()=='location')
                self.assertIn(request('alex','GET',callback).status,(400,403))
                with closing(sqlite3.connect(state.database_path)) as connection:
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM account_sessions').fetchone()[0],sessions_before)


if __name__=='__main__':unittest.main()
