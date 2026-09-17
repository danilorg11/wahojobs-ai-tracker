"""Declared synthetic sample accounts using the normal invited login/profile path.

This is an offline rehearsal adapter, never a production identity provider. Each
approval is bound to its own browser's opaque pending transaction; neither the
gateway's default subject nor another browser's pending authorization is mutated.
"""
from contextlib import closing
from datetime import timedelta
from html import escape
from http.cookies import SimpleCookie
import json
import re
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

from scripts.durable_google_login_fixture_demo import (
    FIXTURE_APPROVAL_ROUTE, FIXTURE_COMPLETE_ROUTE, _fixture_html_response,
    _fixture_redirect, _trusted_fixture_request,
)
from scripts.local_recovery_login import _ResponseView

CHOICE_COOKIE = '__Host-wahojobs_practice_candidate'
PENDING_COOKIE = '__Host-wahojobs_practice_pending'
SAMPLE_KEYS = ('alex', 'biology', 'software', 'fresh')
BEGINNER_KEY = 'beginner'


def sample_definitions():
    from tests.private_beta_demo_support import BACKGROUND
    return {
        'beginner': dict(label='Beginner — first job',
            background=('I live in Brazil. My name is Beginner Sample. '
                'I am looking for my first job. I have no prior work experience. '
                'I speak Portuguese at native level and English fluently. '
                'I have completed high school and do not have a university degree. '
                'My skills include customer support, data entry, writing, attention to detail and Python. '
                'I want remote AI evaluation, annotation, language review and customer support work. '
                'I am interested in reviewing AI-generated responses. I prefer part-time work.'),
            purpose='First-job practice candidate with relevant interests, no prior work or activities; compare with Alex.'),
        'alex': dict(label='Alex — customer support', background=BACKGROUND,
                     purpose='Generalist work and the part-time preference; no AI employment is claimed.'),
        'biology': dict(label='Biology researcher — specialist contrast',
            review_fields=dict(country='Canada', job_titles=['Biology Researcher'],
                skills=['genomics', 'computational biology', 'microbiology', 'scientific writing']),
            background=('I live in Canada. My name is Research Sample. I am a biology researcher. '
                'I have seven years of total work experience. I have a PhD in biology. '
                'My professional domains are biology and microbiology. '
                'My skills include genomics, computational biology, microbiology and scientific writing. '
                'I speak English at native level and French at professional level. '
                'I want remote biology AI training and scientific evaluation work.'),
            purpose='Text-and-review example based on the existing synthetic biology persona; specialist experience supports different work.'),
        'software': dict(label='Software engineer — technical contrast',
            review_fields=dict(country='Germany', job_titles=['Senior Software Engineer'],
                skills=['Python', 'TypeScript', 'APIs', 'backend systems']),
            background=('I live in Germany. My name is Software Sample. I am a senior software engineer. '
                'I have nine years of total work experience. I have a bachelor degree. '
                'My skills include Python, TypeScript, APIs and backend systems. '
                'I speak German at native level, English fluently and Dutch at professional level. '
                'I want remote AI coding evaluation and software review work.'),
            purpose='Text-and-review example based on the existing synthetic software persona; technical work and unrelated specialist limits.'),
        'fresh': dict(label='Fresh Alex — try full onboarding', background=BACKGROUND,
                      purpose='An unconfirmed sample account for the complete text-entry and review journey.'),
    }


def configure_samples(state, *, include_beginner=False):
    """Create invitation records only; profiles are subsequently confirmed via HTTP."""
    from tests.accounts_test_support import INVITATION_KEY
    from wahojobs.accounts import create_invitation
    marker_path = state.directory / 'private-beta-demo.json'
    marker = json.loads(marker_path.read_text())
    assert marker.get('synthetic_private_beta_v1') is True
    assert state.database_path.resolve().parent == state.directory.resolve()
    if 'recommendation_samples' in marker:
        raise ValueError('sample_rehearsal_already_configured')
    keys = SAMPLE_KEYS + ((BEGINNER_KEY,) if include_beginner else ())
    samples = {key: value for key, value in sample_definitions().items() if key in keys}
    with closing(sqlite3.connect(state.database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        for key in keys:
            email = f'practice-{key}@example.test'
            invitation = create_invitation(connection, email=email, lookup_key=INVITATION_KEY,
                expires_at=state.clock() + timedelta(days=7), created_by='synthetic_recommendation_rehearsal',
                idempotency_key='recommendation-practice-'+key, now=state.clock())
            samples[key].update(email=email, subject='recommendation-practice-'+key,
                                invitation=invitation.invitation_token)
    marker['recommendation_samples'] = samples
    marker_path.write_text(json.dumps(marker), encoding='utf-8')


def _samples(state):
    marker = json.loads((state.directory / 'private-beta-demo.json').read_text())
    samples = marker.get('recommendation_samples')
    if (marker.get('synthetic_private_beta_v1') is not True or type(samples) is not dict
            or set(samples) not in (set(SAMPLE_KEYS), set(SAMPLE_KEYS) | {BEGINNER_KEY})
            or state.database_path.resolve().parent != state.directory.resolve()
            or urlsplit(state.public_origin).port == 8802):
        raise ValueError('declared_synthetic_samples_required')
    for key, value in samples.items():
        if value.get('email') != f'practice-{key}@example.test' or value.get('subject') != 'recommendation-practice-'+key:
            raise ValueError('declared_synthetic_samples_required')
    return samples


def _cookie(headers, name):
    values = SimpleCookie()
    try:
        values.load(headers.get('Cookie', ''))
        return values[name].value if name in values else None
    except (ValueError, TypeError):
        return None


def _set_cookie(name, value, max_age=600):
    return ('Set-Cookie', f'{name}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Lax')


class SampleProviderBridge:
    """Test-only fixed identities, chosen for a new normal authentication transaction."""
    def __init__(self, delegate, state):
        self.delegate, self.state = delegate, state
        self.samples = _samples(state)
        self.pending = {}
        self.lock = threading.Lock()

    def matches_route(self, path):
        return path in (FIXTURE_APPROVAL_ROUTE, FIXTURE_COMPLETE_ROUTE) or self.delegate.matches_route(path)

    def handle(self, method, target, headers, body_stream=None):
        path = urlsplit(target)
        if path.path in (FIXTURE_APPROVAL_ROUTE, FIXTURE_COMPLETE_ROUTE):
            if method != 'GET' or path.query or not _trusted_fixture_request(self.state, target, headers):
                return _fixture_html_response(400, 'Practice sign-in unavailable', '<p>Start a new practice sign-in.</p>')
            token = _cookie(headers, PENDING_COOKIE)
            with self.lock:
                now = time.monotonic()
                self.pending = {key: value for key, value in self.pending.items() if value[2] > now}
                value = self.pending.get(token)
                if value is not None and path.path == FIXTURE_COMPLETE_ROUTE:
                    self.pending.pop(token)
            if value is None:
                return _fixture_html_response(400, 'Practice sign-in unavailable', '<p>Start a new practice sign-in.</p>')
            authorization, choice, _ = value
            if path.path == FIXTURE_APPROVAL_ROUTE:
                return _fixture_html_response(200, 'Synthetic sign-in',
                    '<p>Continue as '+escape(self.samples[choice]['label'])+'. This is a synthetic local account.</p>'
                    +f"<p><a href='{FIXTURE_COMPLETE_ROUTE}'>Continue with this practice account</a></p>")
            from tests.durable_google_login_browser_test_support import provider_callback_for
            sample = self.samples[choice]
            callback = urlsplit(provider_callback_for(self.state, authorization, code=secrets.token_urlsafe(24),
                claims_overrides={'sub': sample['subject'], 'email': sample['email'], 'email_verified': True}))
            response = _fixture_redirect(callback.path+'?'+callback.query)
            return _ResponseView(response, headers=(*response.headers, _set_cookie(PENDING_COOKIE, '', 0)))
        response = self.delegate.handle(method, target, headers, body_stream)
        locations = [value for key, value in response.headers if key.lower() == 'location']
        if (path.path == '/auth/google/start' and method == 'POST' and response.status == 303
                and len(locations) == 1 and locations[0].startswith('https://accounts.google.com/o/oauth2/v2/auth?')):
            choice = _cookie(headers, CHOICE_COOKIE)
            if choice not in self.samples:
                return _fixture_html_response(400, 'Select a practice account', '<p>Return to practice sign-in.</p>')
            with self.lock:
                now = time.monotonic()
                self.pending = {key: value for key, value in self.pending.items() if value[2] > now}
                if len(self.pending) >= 64:
                    return _fixture_html_response(503, 'Practice sign-in busy', '<p>Please try again later.</p>')
                token = secrets.token_urlsafe(24)
                self.pending[token] = (locations[0], choice, now+600)
            return _ResponseView(response, headers=tuple((k,v) for k,v in response.headers if k.lower() != 'location')
                + (('Location', FIXTURE_APPROVAL_ROUTE), _set_cookie(PENDING_COOKIE, token)))
        return response


class SampleNotice:
    """Visible sample selector around ordinary routes, separate from product explanations."""
    def __init__(self, delegate, state):
        self.delegate, self.state = delegate, state
        self.samples = _samples(state)
        self.replay_date = json.loads((state.directory / 'private-beta-demo.json').read_text())['now'][:10]

    def handle(self, method, target, headers, body_stream=None):
        path = urlsplit(target)
        login = method == 'GET' and path.path == '/login'
        choice = _cookie(headers, CHOICE_COOKIE) or 'alex'
        if login:
            query = parse_qs(path.query, keep_blank_values=True)
            values = query.get('practice', [choice])
            if len(values) != 1 or values[0] not in self.samples:
                return _fixture_html_response(400, 'Unknown practice account', '<p>Choose a listed practice account.</p>')
            choice = values[0]
        response = self.delegate.handle(method, target, headers, body_stream)
        if not response.body or not any(k.lower() == 'content-type' and 'text/html' in v for k,v in response.headers):
            return response
        selected = self.samples.get(choice, self.samples['alex'])
        links = ' · '.join(f"<a href='/login?next=/find-matches&amp;practice={key}'>{escape(sample['label'])}</a>"
                           for key, sample in self.samples.items())
        panel = ("<aside class='practice-selector' aria-label='Synthetic practice accounts' "
                 "style='max-width:1080px;margin:12px auto;padding:12px 20px;border:1px solid #dbc98d;border-radius:10px;font:14px/1.5 system-ui'>"
                 '<strong>Practice accounts</strong><p>'+links+'</p>'
                 '<p>Simulated date: '+escape(self.replay_date)+'. These are replayed sources and practice examples, not current vacancies.</p>')
        if login:
            panel += ('<p>'+escape(selected['purpose'])+'</p><p>Practice accounts use the normal profile review '
                      'and confirmation flow. Choose Fresh Alex to try onboarding.</p>'
                      '<details><summary>Declared synthetic facts</summary><p>'+escape(selected['background'])+'</p></details>')
        panel += '</aside>'
        body = re.sub(rb'<body\b[^>]*>', lambda match: match[0]+panel.encode(), response.body, count=1)
        if login and response.status == 200:
            body = body.replace(b"name='invitation'", ("name='invitation' value='"+escape(selected['invitation'],quote=True)+"'").encode())
        final_headers = tuple((k,str(len(body)) if k.lower() == 'content-length' else v) for k,v in response.headers)
        if login and response.status == 200:
            final_headers += (_set_cookie(CHOICE_COOKIE, choice),)
        return _ResponseView(response, body=body, headers=final_headers)
