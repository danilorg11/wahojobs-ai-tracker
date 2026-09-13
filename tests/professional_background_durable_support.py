"""Offline fresh-process proof using real AuthKit routes and stored profiles.

Only the identity provider and model are labelled local substitutes. No server,
socket, saved cookie jar, provider object or match-run context crosses processes.
"""
from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
import base64
import gc
import json
import os
from pathlib import Path
import secrets
import sqlite3
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from tests.professional_background_preparation_support import PreparationFixture, OfflineClient
from tests.test_workos_authkit_browser import _cookie_pair, _cookie_value, _header_values
from tests.workos_authkit_test_support import FakeWorkOSBoundary, build_m008
from wahojobs import accounts
from wahojobs.persistent_profiles import (TrustedPrincipalContext, CreatePersistentProfileCommand,
                                         ConfirmedAboutYouTextSourceDraft)
from wahojobs.persistent_profiles_repository import create_persistent_profile
from wahojobs.professional_background_preparation import RECIPE, ProfessionalBackgroundPreparer, PreparationBudget
from wahojobs.professional_background_semantics import ProfessionalBackgroundEvidence, digest
from wahojobs.professional_background_store import SQLiteProfessionalBackgroundStore, initialize_preparation_store
from wahojobs.workos_authkit_staging import (build_workos_authkit_staging_runtime,
    load_workos_authkit_staging_configuration, STAGING_PUBLIC_ORIGIN, STAGING_REDIRECT_URI)


NOW = datetime(2026, 7, 25, 14, tzinfo=timezone.utc)


def durable_fixture():
    fixture = PreparationFixture()
    path = fixture.f.path.with_name('derived.sqlite3')
    with closing(sqlite3.connect(path)) as connection:
        initialize_preparation_store(connection)
    attach_store(fixture, path)
    return fixture, path


def attach_store(fixture, path):
    evidence = ProfessionalBackgroundEvidence(recipe=RECIPE, model=fixture.client.model,
        basis='offline_labelled_stub', durable_store=SQLiteProfessionalBackgroundStore(path))
    fixture.evidence = fixture.preparer.evidence = evidence
    fixture.f.integration._professional_background_evidence = evidence
    return evidence


def process_run(directory, phase, relation):
    directory = Path(directory)
    database = directory / 'application.sqlite3'
    derived = directory / 'derived.sqlite3'
    boundary = FakeWorkOSBoundary()
    invitation_key = secrets.token_bytes(32)
    invitation = None
    if phase == 'A':
        with closing(build_m008(database)) as connection:
            invitation = accounts.create_invitation(connection, email=boundary.email,
                lookup_key=invitation_key, expires_at=NOW.replace(day=26), created_by='offline_test',
                idempotency_key='durable-offline-invitation', now=NOW).invitation_token
        with closing(sqlite3.connect(derived)) as connection:
            initialize_preparation_store(connection)
    config_path = directory / ('config-' + phase + '.json')
    # Disposable configuration is not a persistence payload. Each process
    # creates fresh fake secrets, authenticates, then removes this configuration.
    config_path.write_text(json.dumps(dict(version=1, environment_namespace='private_beta',
        database_path=str(database), public_origin=STAGING_PUBLIC_ORIGIN,
        redirect_uri=STAGING_REDIRECT_URI, workos_client_id='client_0123456789abcdef',
        workos_api_key='sk_test_' + secrets.token_urlsafe(32),
        wahojobs_invitation_lookup_key_base64=base64.b64encode(invitation_key).decode(),
        session_idle_ttl_seconds=3600, session_absolute_ttl_seconds=28800)))
    if os.name != 'nt':
        config_path.chmod(0o600)
    client = OfflineClient(relation)
    evidence = ProfessionalBackgroundEvidence(recipe=RECIPE, model=client.model,
        basis='offline_labelled_stub', durable_store=SQLiteProfessionalBackgroundStore(derived))
    preparer = ProfessionalBackgroundPreparer(evidence, client=client, enabled=phase in ('A', 'replace'),
                                              budget=PreparationBudget(8, 250000))
    with patch('wahojobs.profile_intake.openai_adapter.configured_openai_profile_adapter', return_value=None):
        runtime = build_workos_authkit_staging_runtime(load_workos_authkit_staging_configuration(str(config_path)),
            sdk_boundary_factory=lambda **kw: boundary, clock=lambda: NOW,
            professional_background_preparer=preparer)
    config_path.unlink()
    try:
        app = runtime.browser_integration
        host = urlsplit(STAGING_PUBLIC_ORIGIN).netloc
        headers = [('Host', host)]
        login = app.handle('GET', '/login', headers)
        assert login.status == 200
        cookie = _cookie_pair(login, '__Host-wahojobs_login_csrf')
        login.acknowledge_delivery()
        form = dict(csrf=_cookie_value(cookie))
        if invitation is not None:
            form['invitation'] = invitation
        payload = urlencode(form).encode()
        start = app.handle('POST', '/auth/workos/start', headers + [
            ('Cookie', cookie), ('Origin', STAGING_PUBLIC_ORIGIN), ('Sec-Fetch-Site', 'same-origin'),
            ('Content-Type', 'application/x-www-form-urlencoded'), ('Content-Length', str(len(payload)))],
            BytesIO(payload))
        assert start.status == 303, start.status
        transaction = _cookie_pair(start, '__Host-wahojobs_workos_tx')
        state = parse_qs(urlsplit(_header_values(start, 'Location')[0]).query)['state'][0]
        start.acknowledge_delivery()
        callback = app.handle('GET', '/auth/workos/callback?' + urlencode(
            dict(code=secrets.token_urlsafe(32), state=state)), headers + [('Cookie', transaction)])
        assert callback.status == 303, callback.status
        session = _cookie_pair(callback, 'wahojobs_session')
        csrf = _cookie_pair(callback, '__Host-wahojobs_session_csrf')
        callback.acknowledge_delivery()
        headers += [('Cookie', session + '; ' + csrf)]
        if phase == 'A':
            seed_application(runtime)
        profile_response = app.handle('GET', '/account/profile', headers)
        assert profile_response.status == 200, profile_response.status
        matching = runtime._profile_integration._matches_integration
        credentials = dict(authentication_input=tuple(headers), session_token=_cookie_value(session),
                           csrf_secret=_cookie_value(csrf))
        authority = matching._service.resolve(method='POST', **credentials)
        assert authority.state == 'profile', authority.state
        authorized = authority.authorized_state()
        context = authorized.professional_background_context(evidence)
        selection = dict(profile_id=context.profile_id, job_ids=[7003], **credentials)
        plan = runtime.prepare_professional_background(**selection)
        if phase in ('A', 'replace'):
            assert plan['items'][0]['state'] == ('needs_preparation' if phase == 'A' else 'reusable'), plan['items']
            result = runtime.prepare_professional_background(**selection, execute=True, authorized=True,
                expected_plan_id=plan['plan_id'], replace=phase == 'replace')
            assert result['items'][0]['state'] == 'published', result['items']
        else:
            assert plan['items'][0]['state'] == 'reusable', plan['items']
        response = app.handle('GET', '/find-matches', headers)
        assert response.status == 200, response.status
        run = next(reversed(matching._registry._runs.values()))
        match = next(m for rows in run.recommendation_context['matches'].values()
                     for m in rows if m['job_id'] == 7003)
        from wahojobs.authenticated_variant_details import variant_detail_url
        detail_url = variant_detail_url(match, run_id=run.match_run_id)
        detail = app.handle('GET', detail_url, headers)
        assert detail.status == 200, detail.status
        rows = match['source_qualification_comparisons']
        semantic = next(r for r in rows if r['kind'] == 'professional_background')['components']['occupational_relevance']['semantic']
        assert semantic['relation'] == relation
        assert not match['primary_recommendation_eligible']
        assert len(client.session.calls) == (1 if phase in ('A', 'replace') else 0)
        from wahojobs.authenticated_profile_matches import _conditional_presentation_matches
        output = dict(phase=phase, pid=os.getpid(), owner=context.owner, profile=context.profile_id,
            revision=context.revision_id, profile_digest=context.profile_digest, request_id=plan['items'][0]['request_id'],
            relation=semantic['relation'], generation=evidence.generation, client_dispatches=len(client.session.calls),
            historical_accounting=preparer.accounting, callback=callback.status, profile_status=profile_response.status,
            matches_status=response.status, detail_status=detail.status, run_id=run.match_run_id,
            session_digest=digest(_cookie_value(session)), detail_url=detail_url,
            conditional_ids=[m['job_id'] for m in _conditional_presentation_matches(run.recommendation_context)],
            primary_eligible=match['primary_recommendation_eligible'], comparisons=rows)
        (directory / (phase + '.json')).write_text(json.dumps(output, indent=2))
    finally:
        runtime.close()
        gc.collect()


def seed_application(runtime):
    fixture = PreparationFixture()
    try:
        with runtime._connections.writable_connection_provider() as connection:
            # Only synthetic source fixture rows; no recorded or live database.
            with closing(sqlite3.connect(fixture.f.path)) as source:
                source.row_factory = sqlite3.Row
                for table in ('companies', 'canonical_opportunities', 'jobs', 'crawl_runs',
                              'job_source_contents', 'job_source_content_captures', 'job_source_content_acceptances'):
                    for row in source.execute('SELECT * FROM ' + table):
                        columns = row.keys()
                        connection.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES (' +
                                           ','.join('?' for _ in columns) + ')', tuple(row))
            connection.commit()
            principal_id = connection.execute('SELECT principal_id FROM product_principals').fetchone()[0]
            principal = TrustedPrincipalContext(principal_id=principal_id, environment_namespace='private_beta',
                principal_type='account_native', lifecycle_status='active', claim_policy='account_native',
                exclusive_account_binding=True, eligibility_mode='account_native', active_owner_binding=True)
            command = CreatePersistentProfileCommand.prepare(principal=principal, canonical_profile_v2=fixture.f.profile,
                sources=(ConfirmedAboutYouTextSourceDraft('OFFLINE SYNTHETIC confirmed support role.', NOW),),
                normalizer_version='baseline_v1', reviewer_version=None, actor_type='authenticated_user',
                reason_code='profile.create', idempotency_key='durable-offline-profile', accepted_at=NOW)
            create_persistent_profile(connection, command)
    finally:
        fixture.close()


if __name__ == '__main__':
    import sys
    process_run(*sys.argv[1:])
