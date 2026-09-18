"""Emitted WorkOS form -> native HTTP -> real account services, OFFLINE provider.

Uses the existing DOM dependency and remote-beta fixture. No provider redirects
are followed. This covers the app's proxy-side HTTP boundary, not browser cookie
policy or public Caddy/TLS; those need the separate hosted owner verification.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time
import unittest
from datetime import timedelta
from http.client import HTTPConnection
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.private_beta_app import BetaServer
from tests import test_private_beta_operating as remote_fixture
from tests.workos_authkit_test_support import create_invitation
from wahojobs.remote_beta import make_remote_handler, PROXY_HEADER
from wahojobs.request_diagnostics import RequestDiagnostics

ORIGIN = remote_fixture.ORIGIN


SERIALIZE_FORM = r"""
const {JSDOM} = require('jsdom');
let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const request = JSON.parse(input);
  const dom = new JSDOM(request.html, {url: request.origin + '/login'});
  const form = dom.window.document.querySelector('form[action="/auth/workos/start"]');
  if (!form || form.method !== 'post') throw Error('Missing shipped login form');
  if (request.invitation !== null) form.elements.namedItem('invitation').value = request.invitation;
  const fields = new dom.window.FormData(form);
  process.stdout.write(JSON.stringify({method: form.method.toUpperCase(),
    action: new URL(form.action).pathname, contentType: form.enctype,
    body: new URLSearchParams(fields).toString(), names: [...fields.keys()],
    blankInvitation: fields.get('invitation') === ''}));
  dom.window.close();
});
"""


class RemoteBetaNativeLoginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = remote_fixture.RemoteBetaTests('test_fresh_storage_is_empty_m011_and_never_overwrites')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.diagnostics = RequestDiagnostics()
        self.server = BetaServer(('127.0.0.1', 0), make_remote_handler(
            self.fixture.runtime, 'a' * 64, clock=self.fixture.clock,
            diagnostics=self.diagnostics))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.cookies = {}

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, target, *, body=None, content_type=None, origin=ORIGIN):
        headers = {'Host': 'beta.example.test', PROXY_HEADER: 'a' * 64}
        if self.cookies:
            headers['Cookie'] = '; '.join(k + '=' + v for k, v in self.cookies.items())
        if method == 'POST':
            headers.update({'Origin': origin, 'Sec-Fetch-Site': 'same-origin',
                            'Content-Type': content_type})
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            status, fields, payload = response.status, response.getheaders(), response.read()
            for name, value in fields:
                if name.lower() == 'set-cookie':
                    key, cookie = value.split(';', 1)[0].split('=', 1)
                    if cookie:
                        self.cookies[key] = cookie
                    else:
                        self.cookies.pop(key, None)
            response_headers = dict((k.lower(), v) for k, v in fields)
            # The private record is emitted after body delivery. Synchronize on
            # this response ID rather than assuming the server's finally ran.
            request_id = response_headers.get('x-wahojobs-request-id')
            deadline = time.monotonic() + 3
            while not any(record.request_id == request_id for record in self.diagnostics.snapshot()):
                self.assertLess(time.monotonic(), deadline, 'Response diagnostic was not recorded')
                threading.Event().wait(0.005)
            return status, response_headers, payload
        finally:
            connection.close()

    def rendered_form(self, invitation=None):
        status, _, body = self.request('GET', '/login')
        self.assertEqual(status, 200)
        node = os.environ.get('WAHOJOBS_CLIENT_NODE') or shutil.which('node')
        self.assertTrue(node, 'Node 22+ and tests/client_dom dependencies required; no silent skip.')
        environment = dict(os.environ)
        environment.setdefault('NODE_PATH', str(Path(__file__).parent / 'client_dom' / 'node_modules'))
        result = subprocess.run([node, '--preserve-symlinks', '--preserve-symlinks-main',
            '-e', SERIALIZE_FORM], input=json.dumps({'html': body.decode('utf-8'),
                'origin': ORIGIN, 'invitation': invitation}), text=True, encoding='utf-8',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        form = json.loads(result.stdout)
        self.assertEqual(form['names'], ['csrf', 'invitation'])
        self.assertEqual(form['blankInvitation'], invitation is None)
        return form

    def submit(self, form, **options):
        return self.request(form['method'], form['action'], body=form['body'].encode('ascii'),
                            content_type=form['contentType'], **options)

    def callback(self, start_headers):
        state = parse_qs(urlsplit(start_headers['location']).query)['state'][0]
        target = '/auth/workos/callback?' + urlencode({'state': state, 'code': secrets.token_urlsafe(32)})
        return self.request('GET', target), target

    def counts(self):
        return tuple(self.fixture.seed.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                     for name in ('users', 'account_sessions', 'product_profiles', 'user_pipeline_transitions'))

    def test_blank_native_form_requires_invitation_only_for_new_verified_user(self):
        # Reproduce the hosted Chrome boundary with synthetic unrelated cookies.
        unrelated = {f'unrelated{i}': 'offline' for i in range(24)}
        self.cookies.update(unrelated)
        status, headers, _ = self.submit(self.rendered_form())
        self.assertEqual(status, 303)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        denied, _ = self.callback(headers)
        self.assertEqual(denied[0], 401)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

        invitation = create_invitation(self.fixture.seed, self.fixture.boundary.email)
        status, headers, _ = self.submit(self.rendered_form(invitation.invitation_token))
        self.assertEqual(status, 303)
        issued, _ = self.callback(headers)
        self.assertEqual(issued[0], 303)
        self.assertEqual(self.counts(), (1, 1, 0, 0))
        self.assertEqual(self.request('GET', '/account/profile')[0], 200)

        # Separate offline browser cookie jar; no real account/session deletion.
        self.cookies.clear()
        self.cookies.update(unrelated)
        status, headers, _ = self.submit(self.rendered_form())
        self.assertEqual(status, 303)
        returned, callback_target = self.callback(headers)
        self.assertEqual(returned[0], 303)
        self.assertEqual(self.request('GET', '/account/profile')[0], 200)
        self.assertEqual(self.counts(), (1, 2, 0, 0))
        exchange_count = self.fixture.boundary.exchange_count
        self.assertEqual(self.request('GET', callback_target)[0], 401)
        self.assertEqual(self.fixture.boundary.exchange_count, exchange_count)
        self.assertEqual(self.counts(), (1, 2, 0, 0))
        self.assertEqual(self.fixture.seed.execute(
            "SELECT count(*) FROM account_invitations WHERE invitation_status='consumed'").fetchone()[0], 1)

    def test_stale_and_foreign_forms_remain_rejected_with_fresh_form_recovery(self):
        stale = self.rendered_form()
        self.rendered_form()  # A second tab replaces the browser's shared CSRF cookie.
        self.assertEqual(self.submit(stale)[0], 403)
        self.assertEqual(self.diagnostics.snapshot()[-1].outcome, 'login_csrf_mismatch')
        self.assertEqual(self.fixture.boundary.authorization_count, 0)
        self.assertEqual(self.submit(self.rendered_form(), origin='https://foreign.example.test')[0], 400)
        self.assertEqual(self.fixture.boundary.authorization_count, 0)
        self.assertEqual(self.submit(self.rendered_form())[0], 303)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_invalid_and_expired_invitations_fail_before_provider_preparation(self):
        self.assertEqual(self.submit(self.rendered_form('invalid'))[0], 403)
        self.assertEqual(self.diagnostics.snapshot()[-1].outcome, 'login_invitation_shape_rejected')
        invitation = create_invitation(self.fixture.seed, self.fixture.boundary.email)
        self.fixture.clock.advance(timedelta(days=2))
        self.assertEqual(self.submit(self.rendered_form(invitation.invitation_token))[0], 403)
        self.assertEqual(self.diagnostics.snapshot()[-1].outcome, 'login_prepare_unavailable')
        self.assertEqual(self.fixture.boundary.authorization_count, 0)
        self.assertEqual(self.fixture.boundary.exchange_count, 0)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_native_rejection_labels_are_private_and_correlate_without_changing_checks(self):
        for failure in ('absent', 'target_absent', 'target_invalid', 'target_duplicate', 'size'):
            with self.subTest(failure=failure):
                self.cookies.clear()
                form = self.rendered_form()
                if failure == 'absent':
                    self.cookies.clear()
                    expected = 'login_cookie_header_absent'
                elif failure == 'target_absent':
                    self.cookies = {'unrelated': 'private-fixture-value'}
                    expected = 'login_cookie_target_absent'
                elif failure == 'target_invalid':
                    self.cookies['__Host-wahojobs_login_csrf'] = 'private-fixture-value'
                    expected = 'login_cookie_target_invalid'
                elif failure == 'target_duplicate':
                    self.cookies['unrelated'] = 'offline; __Host-wahojobs_login_csrf=' + self.cookies['__Host-wahojobs_login_csrf']
                    expected = 'login_cookie_target_duplicate'
                else:
                    self.cookies['unrelated'] = 'private-fixture-value' * 220
                    expected = 'login_cookie_size_rejected'
                status, headers, body = self.submit(form)
                self.assertEqual(status, 403)
                self.assertIn(b'Sign-in request rejected', body)
                self.assertIn('Max-Age=0', headers['set-cookie'])
                record = self.diagnostics.snapshot()[-1]
                self.assertEqual(record.outcome, expected)
                self.assertEqual(record.request_id, headers['x-wahojobs-request-id'])
                self.assertNotIn(expected, repr(headers) + body.decode())
                self.assertNotIn('private-fixture-value', repr(record))
                self.assertEqual(self.fixture.boundary.authorization_count, 0)
                self.assertEqual(self.fixture.boundary.exchange_count, 0)
        self.cookies.clear()
        status, headers, body = self.submit(self.rendered_form())
        self.assertEqual(status, 303)
        self.assertEqual(self.diagnostics.snapshot()[-1].outcome, 'login_authorization_prepared')
        self.assertNotIn('login_authorization_prepared', repr(headers) + body.decode())
        self.assertEqual(self.counts(), (0, 0, 0, 0))


if __name__ == '__main__':
    unittest.main()
