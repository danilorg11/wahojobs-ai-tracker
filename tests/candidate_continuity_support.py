"""Disposable source/owner fixtures and a real authenticated HTTPS observer.

Only inventory and declared candidate facts are synthetic. Login, delivery,
owner authorization, product routes, forms, mutations and persistence are real.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import http.client
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode
from unittest.mock import patch

from tests.durable_google_login_browser_test_support import (
    temporary_browser_login_state, https_request, cookie_values, cookie_header, form_body,
)
from tests.test_authenticated_profile_matches import _seed_configured_inventory, AuthenticatedProfileMatchesTests


class Page(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.forms, self.links, self.current = [], [], None
        self.feed(body.decode() if isinstance(body, bytes) else body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.current = {'target': attrs.get('action'), 'fields': {}}
        elif tag == 'input' and self.current is not None and attrs.get('name'):
            self.current['fields'][attrs['name']] = attrs.get('value', '')
        elif tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_endtag(self, tag):
        if tag == 'form' and self.current is not None:
            self.forms.append(self.current); self.current = None

    def action(self, action, *, item=None):
        candidates = [f for f in self.forms if f['fields'].get('action') == action
                      and (item is None or f['fields'].get('pipeline_item_id') == item)]
        if len(candidates) != 1:
            raise AssertionError(f'Expected one generated {action} form, got {len(candidates)}')
        return candidates[0]


def reserve_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _insert_copy(connection, table, row, **changes):
    row = dict(row, **changes)
    connection.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES ('+
                       ','.join('?' for _ in row)+')', tuple(row.values()))


def capture(connection, job_id, observed, *, body=None, title=None, url=None):
    from wahojobs.crawler.types import JobCandidate
    from wahojobs.source_capture import SourceCaptureContext
    from wahojobs.db.repository import upsert_job_source_content, verify_job_source_acceptance_integrity
    row = dict(connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
    candidate = JobCandidate(**{k: row[k] for k in ('title','location','url','external_id','department',
        'expertise','commitment','opportunity_kind','availability_basis','source_hash')},
        include_in_live_market_estimate=bool(row['include_in_live_market_estimate']),
        source_body=body or ('## What you will do\nReview Python code and evaluate AI responses. '
                            'Build software tests and explain technical errors.\n\n'
                            '## Requirements\nPython software development experience. Remote work from Brazil.'),
        source_body_format='text/markdown', source_metadata={'synthetic_fixture': True},
        source_updated_at=observed)
    if title or url:
        from dataclasses import replace
        candidate = replace(candidate, title=title or candidate.title, url=url or candidate.url)
        # Match the legitimate repository observation boundary: the capture
        # writer accepts changed job fields and preserves the stable source key.
    context = SourceCaptureContext(None, 'success', False, True, True, False, 1, 1, 1, 0,
                                   'synthetic continuity fixture', 'synthetic-v1')
    result = upsert_job_source_content(connection, job_id, 'configured-production',
        'synthetic-posting-v1', candidate, observed, capture_context=context)
    verify_job_source_acceptance_integrity(connection, job_id)
    return result


def add_candidate(connection, profile, *, principal_id, account_id, now, key):
    from wahojobs.persistent_profiles import TrustedPrincipalContext, ConfirmedAboutYouTextSourceDraft
    from wahojobs.persistent_profiles_repository import (
        CreatePersistentProfileCommand, create_persistent_profile,
    )
    principal = TrustedPrincipalContext(principal_id=principal_id, environment_namespace='private_beta',
        principal_type='account_native', lifecycle_status='active', claim_policy='account_native',
        exclusive_account_binding=True, eligibility_mode='account_native', active_owner_binding=True)
    command = CreatePersistentProfileCommand.prepare(principal=principal, canonical_profile_v2=profile,
        sources=(ConfirmedAboutYouTextSourceDraft('Synthetic candidate: Python software and AI evaluation.', now),),
        normalizer_version='baseline_v1', reviewer_version=None, actor_type='authenticated_user',
        reason_code='profile.create', idempotency_key=key, accepted_at=now)
    return create_persistent_profile(connection, command).profile_id


@contextmanager
def synthetic_state(*, port=None, competitors=0, conditional=False, candidate_profile=None,
                    posting_title=None, posting_body=None):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with temporary_browser_login_state(port=port or reserve_port(), seed_existing_profile=False,
            seed_existing_identity=False, mutate_configuration=lambda d: d.update(environment='private_beta')) as state:
        from tests.google_oidc_gateway_test_support import ManualClock
        state.clock = ManualClock(now)
        _seed_configured_inventory(state.database_path, observed_at=now)
        from tests.test_canonical_profile_v2 import load_cases
        from wahojobs.profiles.canonical import complete_trusted_fixture_provenance
        from wahojobs.profiles.canonical_v2 import convert_v1_to_v2
        original = deepcopy(load_cases()[12]['expected_canonical_profile'])
        if conditional:
            from tests.test_confirmed_activity_matching import candidate
            original = candidate(['Model output evaluation'])
            original['experience']['recent_roles'] = ['Content marketing specialist']
        original['location']['country'] = 'Brazil'
        from wahojobs.profiles.canonical import field_sources_for_profile, PROFILE_SOURCE_USER_CONFIRMATION
        original['provenance']['field_sources'] = field_sources_for_profile(original, PROFILE_SOURCE_USER_CONFIRMATION, explicit=True)
        profile = convert_v1_to_v2(json.loads(json.dumps(complete_trusted_fixture_provenance(original))),
            persistent_profile_id='prf_0123456789abcdef0123456789abcdef',
            source_ordinal_resolver=lambda *_: [1])
        if candidate_profile is not None:
            profile = deepcopy(candidate_profile)
        with closing(sqlite3.connect(state.database_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute("UPDATE companies SET name='Continuity Demo Jobs' WHERE id=7001")
            connection.execute("UPDATE jobs SET title='Python Backend AI Coding Evaluator', "
                "department='Software Engineering', expertise='Software Engineering', commitment='Full-time', "
                "location='Remote - Brazil', url='https://jobs.example.test/PostingA' WHERE id=7003")
            connection.execute("UPDATE canonical_opportunities SET canonical_title='Python Backend AI Coding Evaluator', "
                               "source_category='Software Engineering' WHERE id=7002")
            if conditional:
                connection.execute("UPDATE jobs SET title='Portuguese AI Data Reviewer', department='',expertise='',commitment='' WHERE id=7003")
                connection.execute("UPDATE canonical_opportunities SET canonical_title='Portuguese AI Data Reviewer',source_category='' WHERE id=7002")
            if posting_title:
                connection.execute("UPDATE jobs SET title=?,department='',expertise='',commitment='Contract',location='Remote' WHERE id=7003",(posting_title,))
                connection.execute("UPDATE canonical_opportunities SET canonical_title=?,source_category='' WHERE id=7002",(posting_title,))
            first = dict(connection.execute('SELECT * FROM jobs WHERE id=7003').fetchone())
            _insert_copy(connection, 'jobs', first, id=7006, external_id='PostingB',
                         url='https://jobs.example.test/PostingB', source_hash='synthetic-posting-B')
            connection.execute('UPDATE canonical_opportunities SET variant_count=2 WHERE id=7002')
            canonical = dict(connection.execute('SELECT * FROM canonical_opportunities WHERE id=7002').fetchone())
            for index in range(competitors):
                canonical_id, job_id = 8000+index, 8100+index
                _insert_copy(connection, 'canonical_opportunities', canonical, id=canonical_id,
                             canonical_key=f'competitor-{index}', variant_count=1)
                _insert_copy(connection, 'jobs', first, id=job_id, canonical_opportunity_id=canonical_id,
                    external_id=f'competitor-{index}', url=f'https://jobs.example.test/competitor-{index}',
                    source_hash=f'competitor-{index}')
            for row in connection.execute('SELECT id FROM jobs').fetchall():
                capture(connection, row[0], now.isoformat(), body=posting_body or (
                    'Key Responsibilities\n\nEvaluate AI outputs.\n\nQualifications\n\nHands-on experience in a marketing role.'
                    if conditional else None))
            connection.commit()
            from tests.google_oidc_gateway_test_support import seed_existing_google_identity
            from tests.ownership_test_support import add_principal, add_binding, add_activation_event
            from tests.durable_google_login_browser_test_support import ACCOUNT_CREATED_AT
            first_owner = seed_existing_google_identity(connection, suffix='continuity-first', created_at=ACCOUNT_CREATED_AT)
            state.account_id = first_owner.user.user_id
            state.principal_id = add_principal(connection, suffix='71', environment='private_beta',
                principal_type='account_native', status='active', claim_policy='account_native', exclusive=1)
            binding = add_binding(connection, state.principal_id, state.account_id, suffix='71', environment='private_beta')
            add_activation_event(connection, state.principal_id, state.account_id, binding, suffix='71', environment='private_beta')
            connection.commit()
            state.profile_id = add_candidate(connection, profile, principal_id=state.principal_id,
                account_id=state.account_id, now=now, key='continuity-profile-first')
            other = seed_existing_google_identity(connection, suffix='continuity-second', created_at=ACCOUNT_CREATED_AT)
            principal_id = add_principal(connection, suffix='72', environment='private_beta',
                principal_type='account_native', status='active', claim_policy='account_native', exclusive=1)
            binding = add_binding(connection, principal_id, other.user.user_id, suffix='72', environment='private_beta')
            add_activation_event(connection, principal_id, other.user.user_id, binding, suffix='72', environment='private_beta')
            connection.commit()
            other_profile = add_candidate(connection, profile, principal_id=principal_id,
                account_id=other.user.user_id, now=now, key='continuity-profile-second')
        marker = {'synthetic_candidate_continuity_v1': True, 'now': now.isoformat(),
                  'owners': {'first': state.account_id, 'second': other.user.user_id},
                  'profiles': {'first': state.profile_id, 'second': other_profile}}
        (state.directory/'continuity-demo.json').write_text(json.dumps(marker), encoding='utf-8')
        yield state


_standard_send_output = http.client.HTTPSConnection._send_output


def _complete_form_output(self, message_body=None, encode_chunked=False):
    # Send the small, bounded test form with its headers in one TLS write.
    # Separate writes can race an intentional pre-body auth rejection on
    # Windows, whose close discards the unread second record. Never retry a
    # mutation or turn a truncated response into an asserted status.
    if isinstance(message_body, bytes) and not encode_chunked:
        self._buffer.extend((b'', b''))
        request = b'\r\n'.join(self._buffer) + message_body
        del self._buffer[:]
        self.send(request)
    else:
        _standard_send_output(self, message_body, encode_chunked)


class BrowserClient:
    def __init__(self, state, *, evidence=None):
        self.state, self.cookies, self.responses = state, {}, []
        self.evidence = Path(evidence) if evidence else None
        if self.evidence: self.evidence.mkdir(exist_ok=True)

    def request(self, method, target, body=None, *, csrf=True, json_response=False):
        jar = dict(self.cookies)
        if not csrf: jar.pop('__Host-wahojobs_session_csrf', None)
        headers = [('Cookie', cookie_header(jar))]
        if method == 'POST':
            headers += [('Origin', self.state.public_origin), ('Sec-Fetch-Site', 'same-origin'),
                ('Content-Type', 'application/x-www-form-urlencoded'), ('Content-Length', str(len(body)))]
        if json_response: headers.append(('Accept', 'application/json'))
        with patch.object(http.client.HTTPSConnection, '_send_output', _complete_form_output):
            response = https_request(self.state, method, target, headers=tuple(headers), body=body)
        for key,value in cookie_values(response).items():
            if value: self.cookies[key]=value
            else: self.cookies.pop(key,None)
        self.responses.append((method,target,response.status,response.body))
        if self.evidence:
            index=len(self.responses)
            (self.evidence/f'{index:03d}.body').write_bytes(response.body)
            (self.evidence/f'{index:03d}.json').write_text(json.dumps(dict(method=method,target=target,
                status=response.status)), encoding='utf-8')
        return response

    def login(self):
        assert self.request('GET','/login?next=/find-matches').status == 200
        response=self.request('POST','/auth/google/start',form_body(csrf=self.cookies['__Host-wahojobs_login_csrf']))
        assert response.header_values('Location') == ('/__fixture/google/approve',)
        assert self.request('GET','/__fixture/google/approve').status == 200
        response=self.request('GET','/__fixture/google/complete')
        response=self.request('GET',response.header_values('Location')[0])
        assert response.status == 303
        assert self.request('GET','/account/profile').status == 200

    def submit(self, form, **kwargs):
        return self.request('POST',form['target'],urlencode(form['fields']).encode(),**kwargs)


@contextmanager
def running_process(state, *, owner='first'):
    ready, stop = state.directory/'continuity-ready.json', state.directory/'continuity-stop'
    ready.unlink(missing_ok=True); stop.unlink(missing_ok=True)
    with (state.directory/'process.stdout.log').open('a',encoding='utf-8') as out, \
         (state.directory/'process.stderr.log').open('a',encoding='utf-8') as err:
        process = subprocess.Popen([sys.executable, '-B', '-m', 'scripts.candidate_continuity_demo',
            '--serve-state', str(state.directory), '--owner', owner], stdout=out, stderr=err)
        try:
            deadline=time.monotonic()+30
            while not ready.exists():
                if process.poll() is not None or time.monotonic()>deadline:
                    raise AssertionError('Synthetic product process did not become ready: '+
                        (state.directory/'process.stderr.log').read_text())
                time.sleep(.1)
            yield process
        except Exception as exc:
            raise AssertionError(str(exc)+'\nSynthetic process stderr:\n'+
                (state.directory/'process.stderr.log').read_text()+'\nSynthetic process stdout:\n'+
                (state.directory/'process.stdout.log').read_text()) from exc
        finally:
            stop.write_text('stop',encoding='utf-8')
            try: process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(timeout=10)
            assert process.returncode == 0, (state.directory/'process.stderr.log').read_text()


@contextmanager
def running_application(state, *, canary_gate=None):
    """Normal composition with a construction observer for deterministic races."""
    from scripts import local_recovery_login as runtime
    from tests.durable_google_login_browser_test_support import _running_https_browser_handler
    from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
    build = runtime._build_profile_integration
    observed = []
    def observe(connections, configuration, *args, **kwargs):
        if canary_gate is not None:
            configuration.public_job_canary_gate = canary_gate
        integration = build(connections, configuration, *args, **kwargs)
        observed.append(integration._matches_integration)
        return integration
    with patch.object(runtime, '_build_profile_integration', side_effect=observe), \
         runtime.existing_owner_local_login(state.directory/'runtime.json',
             account_id=state.account_id, clock=state.clock) as (config, app), \
         _running_https_browser_handler(config, make_durable_product_browser_handler(app)):
        assert len(observed) == 1
        yield observed[0]
