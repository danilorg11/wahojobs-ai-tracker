"""Disposable production composition with explicitly synthetic authority.

Only authentication/profile storage resolution is substituted. Accepted source
storage, preparation, client transport parsing, matching and cache behavior run
the production code. The HTTP session below never opens a socket.
"""
from contextlib import closing
from copy import deepcopy
import json
import io
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import requests

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_accepted_title_uncertainty import profile, BODY
from tests.test_accepted_task_matching import AcceptedTaskMatchingTests
from wahojobs import authenticated_profile_matches as browser
from wahojobs.crawler.types import JobCandidate
from wahojobs.db.repository import upsert_job_source_content
from wahojobs.opportunity_llm import OpenAIStructuredEnrichmentClient
from wahojobs.professional_background_preparation import (
    RECIPE, PreparationBudget, ProfessionalBackgroundPreparer,
)
from wahojobs.professional_background_semantics import ProfessionalBackgroundEvidence
from wahojobs.public_job_canary import PublicJobCanaryRoutingGate
from wahojobs.source_capture import SourceCaptureContext
from wahojobs.workos_authkit_staging import _build_profile_integration


_REQUESTED_MODEL = object()


def intercepted_response(request, *, model=_REQUESTED_MODEL, relation='supported_partial',
                         status=200, mode='success', usage=True):
    """Real Requests Response/PreparedRequest, entirely in-memory adapter I/O.

    Derived from the independent review reproductions. No socket or DNS call.
    The model field is intentionally NOT repaired to echo the requested alias.
    """
    body = json.loads(request.body)
    payload = json.loads(body['input'][1]['content'][0]['text'])
    output = dict(request_id=payload['request_id'], relation=relation,
                  candidate_fact_ids=[f['id'] for f in payload['candidate_facts']],
                  source_span=payload['occupational_span'], rationale='OFFLINE REVIEW STUB: occupational relation only.')
    data = dict(id='offline-intercepted', model=body['model'] if model is _REQUESTED_MODEL else model,
                status='completed', output=[dict(type='message', content=[dict(type='output_text', text=json.dumps(output))])])
    if usage:
        data['usage'] = dict(input_tokens=100, output_tokens=100, total_tokens=200)
    if mode == 'refused':
        data['output'][0]['content'] = [dict(type='refusal', refusal='OFFLINE first-attempt refusal')]
    elif mode == 'incomplete':
        data['status'] = 'incomplete'
    elif mode == 'malformed':
        data['output'][0]['content'][0]['text'] = '{'
    elif mode == 'missing_model':
        del data['model']
    response = requests.Response()
    response.status_code, response.url, response.request = status, request.url, request
    response.raw = io.BytesIO(json.dumps(data).encode())
    response.headers['Content-Type'] = 'application/json'
    if status in (301, 302, 303, 307, 308):
        response.headers['Location'] = request.url + '?offline_redirect=1'
    return response


class OfflineResponse:
    def __init__(self, raw, status=200):
        self.raw, self.status_code, self.closed = raw, status, False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start + chunk_size]

    def close(self):
        self.closed = True


class OfflineSession:
    """Response authoring is a labelled stub, never recognition quality."""
    def __init__(self, relation='supported_partial'):
        self.relation, self.mode, self.calls = relation, 'success', []
        self.after_request = None
        self.mutate_output = None
        self.last_response = None

    def post(self, url, **kwargs):
        self.calls.append(deepcopy(kwargs['json']))
        body = kwargs['json']
        payload = json.loads(body['input'][1]['content'][0]['text'])
        if self.after_request:
            self.after_request()
        if self.mode == 'transport':
            raise requests.ConnectionError('Offline labelled failure')
        output = dict(request_id=payload['request_id'], relation=self.relation,
                      candidate_fact_ids=[f['id'] for f in payload['candidate_facts']],
                      source_span=payload['occupational_span'],
                      rationale='OFFLINE LABELLED STUB: occupational relation only; no competence or duration claim.')
        if self.mutate_output:
            self.mutate_output(output)
        data = dict(id='offline-labelled-response', model=body['model'], status='completed',
                    usage=dict(input_tokens=100, output_tokens=100, total_tokens=200),
                    output=[dict(type='message', content=[dict(type='output_text', text=json.dumps(output))])])
        if self.mode == 'missing':
            data['output'] = []
        elif self.mode == 'refused':
            data['output'][0]['content'] = [dict(type='refusal', refusal='OFFLINE LABELLED refusal')]
        elif self.mode == 'incomplete':
            data['status'] = 'incomplete'
        elif self.mode == 'malformed':
            data['output'][0]['content'][0]['text'] = '{'
        elif self.mode == 'failed':
            data['status'] = 'failed'
        raw = json.dumps(data).encode()
        if self.mode == 'oversized':
            raw = b' ' * 40000
        self.last_response = OfflineResponse(raw, 503 if self.mode == 'http' else 200)
        return self.last_response


class OfflineClient(OpenAIStructuredEnrichmentClient):
    offline_labelled_stub = True

    def __init__(self, relation='supported_partial'):
        super().__init__('offline-not-a-credential', model='offline-labelled-model', session=OfflineSession(relation))


class PreparationFixture:
    def __init__(self, *, client=None, budget=None, audit_sink=None, enabled=True, real=False):
        self.f = SyntheticMatcherFixture()
        self.f.profile = profile('Customer support specialist', 6)
        self.revision = 'synthetic-confirmed-revision-1'
        self.client = client or OfflineClient()
        self.evidence = ProfessionalBackgroundEvidence(recipe=RECIPE, model=self.client.model,
            basis='semantic_model_output' if real else 'offline_labelled_stub')
        self.preparer = ProfessionalBackgroundPreparer(self.evidence, client=self.client, enabled=enabled,
            allow_real_requests=real, budget=budget or PreparationBudget(8, 250000), audit_sink=audit_sink)
        overlay = self.f.integration._metadata_overlay
        self.f.integration.close()
        configuration = SimpleNamespace(environment_namespace='synthetic', public_origin='https://app.test',
                                        public_job_canary_gate=PublicJobCanaryRoutingGate.disabled())
        connections = SimpleNamespace(read_only_connection_provider=self.f.provider,
                                      writable_connection_provider=self.f.forbidden_write)
        with (patch('wahojobs.profile_intake.openai_adapter.configured_openai_profile_adapter', return_value=None),
              patch('wahojobs.matching.metadata_overlay.load_overlay', return_value=overlay)):
            self.outer = _build_profile_integration(connections, configuration, lambda: self.f.now,
                                                   professional_background_preparer=self.preparer)
        self.f.integration = self.outer._matches_integration
        self.f.authority = self.authority
        AcceptedTaskMatchingTests.role(self, 'Uncatalogued assessment position', 'Remote')
        self.source(BODY)

    def authority(self):
        return browser.MatchesAuthorityResult('profile', browser._AuthorizedMatchesState(
            'profile', draft_binding=self.f.owner * 64, account_id='account-' + self.f.owner,
            environment_namespace='synthetic', principal_id='principal-' + self.f.owner,
            session_id='session-' + self.f.owner, profile_id=self.f.profile['identity']['profile_id'],
            profile_v2=self.f.profile, revision_id=self.revision))

    def source(self, body, *, job_id=7003, body_format='text/markdown', metadata=None):
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.row_factory = sqlite3.Row
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            job = JobCandidate(external_id=row['external_id'], title=row['title'], location=row['location'],
                url=row['url'], source_hash=row['source_hash'], department=row['department'],
                expertise=row['expertise'], commitment=row['commitment'], source_body=body,
                source_body_format=body_format, source_metadata=metadata or {})
            upsert_job_source_content(c, job_id, 'configured-production', 'fixture', job,
                self.f.now.isoformat(), capture_context=SourceCaptureContext(
                    crawl_run_id=None, provider_outcome='success', used_sample_data=False,
                    snapshot_complete=True, pagination_complete=True, empty_snapshot_validated=False,
                    raw_record_count=1, normalized_record_count=1, candidate_count=1, rejected_record_count=0,
                    payload_shape='synthetic-fixture', schema_fingerprint='synthetic-fixture'))

    def frozen_source(self):
        source = json.loads((Path(__file__).parent / 'fixtures' / 'professional_background_frozen_source.json').read_text())
        # Only the disposable job/capture identity is rehomed; source wording
        # remains byte-for-byte frozen, including its full structured lists.
        self.source(source['body'], body_format=source['body_format'], metadata=source['metadata'])
        return source

    def prepare(self, **options):
        selection = dict(profile_id=self.f.profile['identity']['profile_id'], job_ids=[7003],
                         authentication_input=None, session_token='synthetic-session', csrf_secret='synthetic-csrf')
        selection.update(options)
        with patch.object(browser.AuthenticatedProfileMatchesService, 'resolve', side_effect=lambda **kw: self.authority()):
            return self.outer.prepare_professional_background(**selection)

    def execute(self, plan=None, **options):
        plan = plan or self.prepare()
        return self.prepare(execute=True, authorized=True, expected_plan_id=plan['plan_id'], **options)

    def current(self, target='/find-matches'):
        response = self.f.get(target)
        if response.status != 200:
            raise AssertionError((response.status, response.body))
        run = self.f.last_run()
        match = next(m for rows in run.recommendation_context['matches'].values()
                     for m in rows if m['job_id'] == 7003)
        return run, match

    def close(self):
        self.outer.close()
        self.f.close()
