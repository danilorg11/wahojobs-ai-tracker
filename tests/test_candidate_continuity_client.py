"""Shipped DOM/JavaScript -> real authenticated HTTPS -> durable state regression.

Requires Node 22+ and tests/client_dom/package-lock.json dependencies (npm ci).
Set WAHOJOBS_CLIENT_NODE / NODE_PATH when they are outside standard lookup.
The transport supplies browser-managed cookies and headers, never action fields
or successful responses. TLS is handled by the existing disposable HTTPS helper;
this is a DOM integration test, not a full browser/CSP/layout assertion.
"""
from contextlib import closing
import json
import base64
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch
import http.client

from tests.candidate_continuity_support import synthetic_state, running_process, _complete_form_output
from tests.durable_google_login_browser_test_support import https_request, cookie_values, cookie_header

SCRIPT = Path(__file__).with_name('candidate_continuity_client.cjs')


def persisted_state(state):
    with closing(sqlite3.connect(state.database_path.as_uri()+'?mode=ro',uri=True)) as connection:
        connection.row_factory=sqlite3.Row
        return {'items':[dict(r) for r in connection.execute(
            'SELECT i.*,s.workflow_status,s.visibility,s.reminder_at,s.version '
            'FROM user_pipeline_items i JOIN user_pipeline_state s USING(pipeline_item_id) ORDER BY i.id')],
            'transitions':[dict(r) for r in connection.execute('SELECT * FROM user_pipeline_transitions ORDER BY rowid')]}


def run_client(state, mode, *, evidence=None, script=SCRIPT, observe=persisted_state, fixture=None):
    if evidence is None and os.environ.get('WAHOJOBS_CLIENT_EVIDENCE'):
        evidence=Path(os.environ['WAHOJOBS_CLIENT_EVIDENCE'])/(mode+'.json')
        evidence.parent.mkdir(parents=True,exist_ok=True)
        suffix = 2
        while evidence.exists():
            evidence = evidence.with_name(mode+'-'+str(suffix)+'.json')
            suffix += 1
    node=os.environ.get('WAHOJOBS_CLIENT_NODE') or shutil.which('node')
    if not node:
        raise AssertionError('Node 22+ required; install tests/client_dom with npm ci (no silent skip).')
    env=dict(os.environ)
    env.setdefault('NODE_PATH',str(script.parent/'client_dom'/'node_modules'))
    cookies, wire, result = {}, [], None
    # Explicit flags also permit modules within a restricted Windows workspace.
    with subprocess.Popen([node,'--preserve-symlinks','--preserve-symlinks-main',str(script),
            state.public_origin,mode], stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',env=env) as process:
        errors=[]
        reader=threading.Thread(target=lambda: errors.append(process.stderr.read()),daemon=True);reader.start()
        timer=threading.Timer(180,process.kill);timer.start()
        try:
            for line in process.stdout:
                request=json.loads(line)
                if request['kind']=='result': result=request;break
                if request['kind']=='fixture': response=fixture
                elif request['kind']=='state': response=observe(state)
                else:
                    assert request['kind']=='http'
                    parsed=urlsplit(request['target'])
                    assert not parsed.scheme and not parsed.netloc and request['target'].startswith('/')
                    assert request['method'] in ('GET','POST')
                    assert urlsplit(state.public_origin).hostname=='localhost'
                    assert urlsplit(state.public_origin).port!=8802
                    headers=list(request['headers'].items())
                    assert not {'cookie','host','origin','content-length','transfer-encoding'} & {k.lower() for k,_ in headers}
                    headers.append(('Cookie',cookie_header(cookies)))
                    body=(base64.b64decode(request['body'],validate=True) if request.get('bodyEncoding')=='base64' else request['body'].encode('utf-8')) if request['body'] is not None else None
                    if request['method']=='POST':
                        headers.extend([('Origin',state.public_origin),('Sec-Fetch-Site','same-origin'),
                                        ('Content-Length',str(len(body)))])
                    with patch.object(http.client.HTTPSConnection,'_send_output',_complete_form_output):
                        started=time.perf_counter()
                        actual=https_request(state,request['method'],request['target'],headers=tuple(headers),body=body)
                        transport_ms=(time.perf_counter()-started)*1000
                    for key,value in cookie_values(actual).items():
                        if value: cookies[key]=value
                        else: cookies.pop(key,None)
                    # Cookies stay private to transport; Set-Cookie isn't readable by client fetch either.
                    response={'status':actual.status,'body':actual.body.decode('utf-8'),
                        'headers':[(k,v) for k,v in actual.headers if k.lower()!='set-cookie']}
                    wire.append(dict(request,status=actual.status,response=response,transport_ms=transport_ms))
                process.stdin.write(json.dumps(response)+'\n');process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=15)
            reader.join(timeout=5)
            error=''.join(errors)
        finally:
            timer.cancel()
            if process.poll() is None: process.kill();process.wait(timeout=10)
            if evidence:
                Path(evidence).write_text(json.dumps({'result':result,'wire':wire,
                    'final_state':observe(state),'returncode':process.returncode},indent=2),encoding='utf-8')
        assert process.returncode==0 and result, error or 'Client exited without a result'
    return result


class CandidateContinuityClientTests(unittest.TestCase):
    def test_shipped_client_journey_recovery_foreign_identity_and_fresh_return(self):
        with synthetic_state(competitors=10) as state:
            with running_process(state):
                result=run_client(state,'journey')
                self.assertEqual(len(result['scriptHashes']),1)
                before=persisted_state(state)
                self.assertEqual(len(before['items']),1)
                item=before['items'][0]
                self.assertEqual((item['opportunity_url'],item['workflow_status'],item['visibility']),
                    ('https://jobs.example.test/PostingB','applied','visible'))
                self.assertTrue(item['reminder_at'])
                self.assertEqual([t['action_name'] for t in before['transitions']],
                    ['user_created','product_noop_save','product_applied','product_remind_later',
                     'product_not_interested','product_show_again'])
            with running_process(state):
                result=run_client(state,'return')
                self.assertEqual(persisted_state(state),before,'Fresh authenticated reads cannot mutate history')
            with running_process(state,owner='second'):
                result=run_client(state,'foreign')
                after=persisted_state(state)
                self.assertEqual(after['items'][0],before['items'][0])
                self.assertEqual(after['transitions'][:6],before['transitions'])
                self.assertEqual(len(after['items']),2)
                self.assertEqual(after['items'][1]['workflow_status'],'applied')
                self.assertNotEqual(after['items'][1]['profile_id'],item['profile_id'])
                self.assertEqual([t['action_name'] for t in after['transitions'][6:]],
                    ['user_created','product_applied','product_remind_later'])


if __name__=='__main__': unittest.main()
