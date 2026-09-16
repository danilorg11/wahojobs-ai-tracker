"""Real localhost login/callback delivery; synthetic disposable owners only."""
import sqlite3
import unittest
import tempfile
from pathlib import Path
from contextlib import closing, contextmanager
from unittest.mock import patch

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


class LocalRecoveryPreparationTests(unittest.TestCase):
    """Actual local-login entry point; only synthetic owners and offline outputs."""

    @contextmanager
    def source_state(self):
        from tests.professional_background_preparation_support import PreparationFixture
        from tests.professional_background_durable_support import NOW
        from tests.google_oidc_gateway_test_support import ManualClock
        from wahojobs.persistent_profiles import (
            TrustedPrincipalContext, CreatePersistentProfileCommand,
            ConfirmedAboutYouTextSourceDraft,
        )
        from wahojobs.persistent_profiles_repository import create_persistent_profile
        with temporary_browser_login_state(port=8818, seed_existing_profile=False) as state:
            fixture = PreparationFixture()
            try:
                with closing(sqlite3.connect(state.database_path)) as target:
                    with closing(sqlite3.connect(fixture.f.path)) as source:
                        source.row_factory = sqlite3.Row
                        for table in ('companies', 'canonical_opportunities', 'jobs', 'crawl_runs',
                                      'job_source_contents', 'job_source_content_captures',
                                      'job_source_content_acceptances'):
                            for row in source.execute('SELECT * FROM ' + table):
                                columns = row.keys()
                                target.execute('INSERT INTO ' + table + '(' + ','.join(columns) +
                                    ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(row))
                    target.commit()
                    target.execute('PRAGMA foreign_keys = ON')
                    principal = TrustedPrincipalContext(principal_id=state.principal_id,
                        environment_namespace='test', principal_type='account_native',
                        lifecycle_status='active', claim_policy='account_native',
                        exclusive_account_binding=True, eligibility_mode='account_native',
                        active_owner_binding=True)
                    command = CreatePersistentProfileCommand.prepare(principal=principal,
                        canonical_profile_v2=fixture.f.profile,
                        sources=(ConfirmedAboutYouTextSourceDraft('OFFLINE SYNTHETIC support role.', NOW),),
                        normalizer_version='baseline_v1', reviewer_version=None,
                        actor_type='authenticated_user', reason_code='profile.create',
                        idempotency_key='local-offline-profile', accepted_at=NOW)
                    state.profile_id = create_persistent_profile(target, command).profile_id
                state.clock = ManualClock(NOW)
            finally:
                fixture.close()
            def protected():
                with closing(sqlite3.connect(state.database_path)) as connection:
                    tables = ('product_profiles', 'product_profile_revisions', 'product_profile_sources',
                        'principal_account_bindings', 'ownership_binding_events', 'companies',
                        'canonical_opportunities', 'jobs', 'crawl_runs', 'job_source_contents',
                        'job_source_content_captures', 'job_source_content_acceptances', 'sqlite_master')
                    return {table: connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                            for table in tables}
            before = protected()
            yield state
            self.assertEqual(protected(), before)

    def configured_preparer(self, path, *, enabled=False, initialize=False):
        from tests.professional_background_preparation_support import OfflineClient
        from wahojobs.professional_background_preparation import (
            RECIPE, ProfessionalBackgroundPreparer, PreparationBudget,
        )
        from wahojobs.professional_background_semantics import ProfessionalBackgroundEvidence
        from wahojobs.professional_background_store import (
            SQLiteProfessionalBackgroundStore, initialize_preparation_store,
        )
        if initialize:
            with closing(sqlite3.connect(path)) as connection:
                self.assertTrue(initialize_preparation_store(connection))
                self.assertFalse(initialize_preparation_store(connection))
        client = OfflineClient()
        evidence = ProfessionalBackgroundEvidence(recipe=RECIPE, model=client.model,
            basis='offline_labelled_stub', durable_store=SQLiteProfessionalBackgroundStore(path))
        preparer = ProfessionalBackgroundPreparer(evidence, client=client, enabled=enabled,
            budget=PreparationBudget(8, 250000) if enabled else None)
        return preparer, client

    @contextmanager
    def recovery(self, state, preparer=None, *, omitted=False):
        options = {} if omitted else dict(professional_background_preparer=preparer)
        attempts = preparer.accounting if preparer is not None else None
        with (patch.dict('os.environ', {'WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED': '0',
                                       'WAHOJOBS_OPENAI_ENRICHMENT': '0'}),
              patch('wahojobs.professional_background_store.initialize_preparation_store',
                    side_effect=AssertionError('reads must not initialize companion storage')),
              existing_owner_local_login(state.configuration_path, account_id=state.account_id,
                  clock=state.clock, **options) as (config, app)):
            product = app.delegate._delegate._profile_integration
            matching = product._matches_integration
            self.assertIs(matching._professional_background_preparer, preparer)
            if preparer is not None:
                self.assertIs(matching._professional_background_evidence, preparer.evidence)
            with _running_https_browser_handler(config, make_durable_product_browser_handler(app)):
                cookies = {}
                def request(method, target, body=None, *, jar=None):
                    jar = cookies if jar is None else jar
                    headers = [('Cookie', cookie_header(jar))]
                    if method == 'POST':
                        headers += [('Origin', state.public_origin), ('Sec-Fetch-Site', 'same-origin'),
                            ('Content-Type', 'application/x-www-form-urlencoded'),
                            ('Content-Length', str(len(body)))]
                    response = https_request(state, method, target, headers=tuple(headers), body=body)
                    for key, value in cookie_values(response).items():
                        if value: jar[key] = value
                        else: jar.pop(key, None)
                    return response
                self.assertEqual(request('GET', '/account/profile').status, 401)
                self.assertEqual(request('GET', '/find-matches').status, 401)
                self.assertEqual(request('GET', '/login?next=/find-matches').status, 200)
                started = request('POST', '/auth/google/start',
                    form_body(csrf=cookies['__Host-wahojobs_login_csrf']))
                self.assertEqual(started.header_values('Location'), ('/__fixture/google/approve',))
                self.assertEqual(request('GET', '/__fixture/google/approve').status, 200)
                completed = request('GET', '/__fixture/google/complete')
                callback = request('GET', completed.header_values('Location')[0])
                self.assertEqual(callback.status, 303)
                self.assertEqual(callback.header_values('Location'), ('/find-matches',))
                self.assertEqual(request('GET', '/account/profile').status, 200)
                from urllib.parse import urlsplit
                credentials = dict(authentication_input=(('Host', urlsplit(state.public_origin).netloc),
                    ('Cookie', cookie_header(cookies))), session_token=cookies['wahojobs_session'],
                    csrf_secret=cookies['__Host-wahojobs_session_csrf'])
                if preparer is not None:
                    self.assertEqual(preparer.accounting, attempts)
                yield product, matching, request, credentials

    def read_match(self, matching, request):
        from wahojobs.authenticated_variant_details import variant_detail_url
        from wahojobs import authenticated_variant_details as variants
        from urllib.parse import parse_qs, urlsplit
        preparer = matching._professional_background_preparer
        attempts = preparer.accounting if preparer is not None else None
        response = request('GET', '/find-matches')
        self.assertEqual(response.status, 200, response.body)
        run = next(reversed(matching._registry._runs.values()))
        match = next(m for rows in run.recommendation_context['matches'].values()
                     for m in rows if m['job_id'] == 7003)
        detail_url = variant_detail_url(match, run_id=run.match_run_id)
        self.assertEqual(parse_qs(urlsplit(detail_url).query),
                         {'variant': ['7003'], 'run': [run.match_run_id]})
        with patch.object(variants, 'prepare_variant_notice', wraps=variants.prepare_variant_notice) as notice:
            detail = request('GET', detail_url)
        self.assertEqual(detail.status, 200, detail.body)
        notice.assert_called_once()
        job = notice.call_args.args[0]
        self.assertEqual(job['job_id'], match['job_id'])
        self.assertEqual(job['official_url'], match['url'])
        self.assertEqual(request('GET', detail_url, jar={}).status, 401)
        rows = match['source_qualification_comparisons']
        semantic = next(row for row in rows if row['kind'] == 'professional_background')[
            'components']['occupational_relevance'].get('semantic')
        if preparer is not None:
            self.assertEqual(preparer.accounting, attempts)
        if not semantic:
            self.assertNotIn(b"id='opportunity-7003'", response.body)
            self.assertIsNone(job['_authenticated_recommendation'])
            self.assertFalse(job['_authenticated_local_checks']['passes'])
            local_background = next(row for row in job['_authenticated_local_checks']['match']['source_qualification_comparisons']
                                    if row['kind'] == 'professional_background')
            self.assertFalse(local_background['components']['occupational_relevance'].get('semantic'))
            self.assertIn(b'5+ years of relevant professional experience', detail.body)
        else:
            self.assertEqual(job['_authenticated_recommendation']['job_id'], match['job_id'])
            self.assertTrue(job['_authenticated_membership_known'])
            self.assertTrue(job['_authenticated_local_checks']['passes'])
            local_match = job['_authenticated_local_checks']['match']
            for key in ('job_id', 'canonical_opportunity_id', 'url'):
                self.assertEqual(local_match[key], match[key])
            local_background = next(row for row in local_match['source_qualification_comparisons']
                                    if row['kind'] == 'professional_background')
            original_background = next(row for row in rows if row['kind'] == 'professional_background')
            self.assertEqual(local_background['source'], original_background['source'])
            self.assertEqual(local_background['status'], 'unresolved')
            self.assertEqual(local_background['components']['occupational_relevance']['semantic'], semantic)
            self.assertEqual(local_background['components']['required_duration']['status'], 'unresolved')
        return run, match, semantic, response, detail, detail_url

    def publish(self, state, preparer):
        with self.recovery(state, preparer) as (product, matching, request, credentials):
            selection = dict(profile_id=state.profile_id, job_ids=[7003], **credentials)
            plan = product.prepare_professional_background(**selection)
            self.assertEqual(plan['items'][0]['state'], 'needs_preparation')
            result = product.prepare_professional_background(**selection, execute=True,
                authorized=True, expected_plan_id=plan['plan_id'])
            self.assertEqual(result['items'][0]['state'], 'published')
            run, match, semantic, response, detail, url = self.read_match(matching, request)
            self.assertEqual(semantic['relation'], 'supported_partial')
            self.assertFalse(match['primary_recommendation_eligible'])
            return run.match_run_id, url, plan['items'][0]['request_id']

    def test_omitted_argument_keeps_default_authenticated_composition(self):
        with self.source_state() as state:
            with self.recovery(state, omitted=True) as (_, matching, request, _):
                _, match, semantic, _, _, _ = self.read_match(matching, request)
                self.assertIsNone(matching._professional_background_evidence)
                self.assertFalse(semantic)
                self.assertFalse(match['primary_recommendation_eligible'])

    def test_empty_durable_store_exact_forwarding_and_disabled_authenticated_reads(self):
        with self.source_state() as state:
            path = state.directory / 'derived.sqlite3'
            preparer, client = self.configured_preparer(path, initialize=True)
            self.assertEqual(preparer.evidence._store.path, path.resolve())
            with self.recovery(state, preparer) as (product, matching, request, credentials):
                _, match, semantic, response, detail, _ = self.read_match(matching, request)
                self.assertFalse(semantic)
                self.assertFalse(match['primary_recommendation_eligible'])
                self.assertNotIn(b'OFFLINE LABELLED', response.body + detail.body)
                plan = product.prepare_professional_background(profile_id=state.profile_id,
                    job_ids=[7003], **credentials)
                self.assertEqual(plan['items'][0]['state'], 'needs_preparation')
                self.assertFalse(plan['execution_enabled'])
                self.assertIsNone(plan['budget'])
                with self.assertRaisesRegex(ValueError, 'preparation_execution_disabled'):
                    product.prepare_professional_background(profile_id=state.profile_id,
                        job_ids=[7003], **credentials, execute=True, authorized=True,
                        expected_plan_id=plan['plan_id'])
            self.assertEqual(client.session.calls, [])
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM preparation_results').fetchone(), (0,))

    def test_valid_persisted_evidence_fresh_local_composition_without_generation_budget(self):
        with self.source_state() as state:
            path = state.directory / 'derived.sqlite3'
            writer, writer_client = self.configured_preparer(path, enabled=True, initialize=True)
            old_run, old_url, request_id = self.publish(state, writer)
            self.assertEqual(len(writer_client.session.calls), 1)
            reader, reader_client = self.configured_preparer(path)
            self.assertIsNot(reader.evidence, writer.evidence)
            with self.recovery(state, reader) as (product, matching, request, credentials):
                authority = matching._service.resolve(method='POST', **credentials)
                context = authority.authorized_state().professional_background_context(reader.evidence)
                self.assertEqual(context.profile_id, state.profile_id)
                self.assertEqual(context.owner, (state.account_id, 'test', state.principal_id))
                plan = product.prepare_professional_background(profile_id=state.profile_id,
                    job_ids=[7003], **credentials)
                self.assertEqual(plan['items'][0]['state'], 'reusable')
                self.assertEqual(plan['items'][0]['request_id'], request_id)
                self.assertFalse(plan['execution_enabled'])
                self.assertIsNone(plan['budget'])
                run, match, semantic, response, detail, url = self.read_match(matching, request)
                self.assertNotEqual(run.match_run_id, old_run)
                self.assertEqual(run.owner_profile_id, state.profile_id)
                self.assertNotEqual(url, old_url)
                self.assertIn(url.encode().replace(b'&', b'&amp;'), response.body)
                self.assertEqual(semantic['relation'], 'supported_partial')
                self.assertFalse(match['primary_recommendation_eligible'])
                requirement = 'Check the requirement: “**5+ years of relevant professional experience in Customer success / support operations.**”.'.encode()
                self.assertIn(requirement, response.body)
                self.assertIn(requirement, detail.body)
                comparison = next(row for row in match['source_qualification_comparisons']
                                  if row['kind'] == 'professional_background')
                self.assertEqual(comparison['status'], 'unresolved')
                self.assertEqual(comparison['components']['required_duration']['status'], 'unresolved')
                self.assertEqual(reader.accounting['attempts'], 0)
                self.assertEqual(reader.accounting['physical_attempts'], 0)
                with self.assertRaisesRegex(ValueError, 'preparation_profile_mismatch'):
                    product.prepare_professional_background(profile_id='foreign-profile',
                        job_ids=[7003], **credentials)
            self.assertEqual(reader_client.session.calls, [])
            self.assertEqual(len(writer_client.session.calls), 1)

    def test_persisted_evidence_does_not_transfer_to_another_confirmed_profile(self):
        with self.source_state() as first:
            path = first.directory / 'derived.sqlite3'
            writer, writer_client = self.configured_preparer(path, enabled=True, initialize=True)
            _, _, request_id = self.publish(first, writer)
            with self.source_state() as second:
                self.assertNotEqual(second.profile_id, first.profile_id)
                reader, client = self.configured_preparer(path)
                with self.recovery(second, reader) as (product, matching, request, credentials):
                    plan = product.prepare_professional_background(profile_id=second.profile_id,
                        job_ids=[7003], **credentials)
                    self.assertEqual(plan['items'][0]['state'], 'needs_preparation')
                    self.assertNotEqual(plan['items'][0]['request_id'], request_id)
                    _, match, semantic, _, detail, _ = self.read_match(matching, request)
                    self.assertFalse(semantic)
                    self.assertFalse(match['primary_recommendation_eligible'])
                    self.assertNotIn(b'OFFLINE LABELLED', detail.body)
                self.assertEqual(client.session.calls, [])
            self.assertEqual(len(writer_client.session.calls), 1)

    def test_configured_preparer_cannot_bypass_existing_identity_or_profile_ownership(self):
        with temporary_browser_login_state(port=8818, seed_existing_profile=False) as state:
            preparer, client = self.configured_preparer(state.directory / 'derived.sqlite3', initialize=True)
            for account_id, reason in ((state.account_id, 'existing_profile_owner_required'),
                                      ('usr_' + '0' * 32, 'existing_local_identity_required')):
                with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                    with existing_owner_local_login(state.configuration_path, account_id=account_id,
                            clock=state.clock, professional_background_preparer=preparer):
                        self.fail('configured evidence must not confer owner authority')
            self.assertEqual(client.session.calls, [])

    def test_uninitialized_or_invalid_companion_is_not_created_or_adopted(self):
        with self.source_state() as state:
            for kind in ('missing', 'empty', 'invalid'):
                path = state.directory / (kind + '.sqlite3')
                if kind != 'missing':
                    with closing(sqlite3.connect(path)) as connection:
                        if kind == 'invalid': connection.execute('PRAGMA application_id = 1')
                before = path.read_bytes() if path.exists() else None
                with self.subTest(kind=kind), self.assertRaisesRegex(ValueError,
                        'professional_evidence_storage_unavailable'):
                    preparer, _ = self.configured_preparer(path)
                    with existing_owner_local_login(state.configuration_path, account_id=state.account_id,
                            clock=state.clock, professional_background_preparer=preparer):
                        self.fail('an unavailable store must fail before composition')
                self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_companion_failure_after_configuration_is_explicit_on_local_matches(self):
        with self.source_state() as state:
            path = state.directory / 'derived.sqlite3'
            preparer, client = self.configured_preparer(path, initialize=True)
            path.unlink()
            with self.recovery(state, preparer) as (_, matching, request, _):
                response = request('GET', '/find-matches')
                self.assertEqual(response.status, 503, response.body)
                self.assertEqual(len(matching._registry._runs), 0)
                self.assertNotIn(b'OFFLINE LABELLED', response.body)
                self.assertIs(matching._professional_background_evidence, preparer.evidence)
            self.assertFalse(path.exists())
            self.assertEqual(client.session.calls, [])


if __name__=='__main__':
    unittest.main()
