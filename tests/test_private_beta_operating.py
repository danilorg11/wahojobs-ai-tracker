"""OFFLINE provider-shaped beta checks. FakeWorkOSBoundary verifies no real identity."""
from contextlib import closing
from datetime import timedelta
from email.message import Message
from http.client import HTTPConnection
from io import BytesIO
from pathlib import Path
import base64
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from scripts.private_beta_app import BetaServer, run
from scripts.private_beta_storage import initialize_fresh
from tests.test_workos_authkit_browser import (
    WorkOSAuthKitBrowserTests, _cookie_pair, _header_values,
    LOGIN_CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, SESSION_CSRF_COOKIE_NAME)
from tests.workos_authkit_test_support import FakeWorkOSBoundary, MutableClock, INVITATION_KEY, create_invitation
from wahojobs.remote_beta import RemoteBetaIntegration, make_remote_handler, PROXY_HEADER
from wahojobs.workos_authkit_staging import (
    load_workos_authkit_staging_configuration, build_workos_authkit_staging_runtime, WorkOSAuthKitStagingError)
from wahojobs import beta_recovery as recovery
from wahojobs.storage_relocation import reconcile_relocation, require_storage_activation, HOLD, LINEAGE, RETIRED, sidecar
from wahojobs.database_lifetime_ownership import (
    acquire_database_lifetime_ownership, release_database_lifetime_ownership,
    ROLE_DURABLE_RUNTIME, DatabaseLifetimeOwnershipError)

ORIGIN = 'https://beta.example.test'


class RemoteBetaTests(unittest.TestCase):
    _start = WorkOSAuthKitBrowserTests._start
    _callback = WorkOSAuthKitBrowserTests._callback
    _successful_login = WorkOSAuthKitBrowserTests._successful_login

    def setUp(self):
        clean = {k:v for k,v in os.environ.items() if k not in {
            'OPENAI_API_KEY','WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED','WAHOJOBS_ALLOW_LOCAL_LOGIN',
            'WAHOJOBS_PRACTICE_LOGIN','WAHOJOBS_TEST_OVERLAY'}}
        environment = patch.dict(os.environ, clean, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix='offline-remote-beta-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        initialize_fresh(self.root / 'fresh')
        self.database = self.root / 'fresh' / 'product.sqlite3'
        self.seed = sqlite3.connect(self.database)
        self.seed.row_factory = sqlite3.Row
        self.seed.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.seed.close)
        self.config_path = self.root / 'config.json'
        self.document = dict(version=2, runtime_mode='remote_beta', environment_namespace='private_beta',
            database_path=str(self.database), public_origin=ORIGIN, redirect_uri=ORIGIN+'/auth/workos/callback',
            workos_client_id='client_0123456789abcdef', workos_api_key='sk_test_OFFLINE_ONLY_1234567890',
            wahojobs_invitation_lookup_key_base64=base64.b64encode(INVITATION_KEY).decode(),
            proxy_secret='a'*64, session_idle_ttl_seconds=3600, session_absolute_ttl_seconds=28800)
        self.write_config()
        self.clock = MutableClock()
        self.boundary = FakeWorkOSBoundary()
        self.runtime = None
        self.addCleanup(self.stop)
        self.start()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.document), encoding='utf-8')
        self.config_path.chmod(0o600)

    def start(self):
        config = load_workos_authkit_staging_configuration(str(self.config_path), remote_beta=True)
        with patch('socket.create_connection', side_effect=AssertionError('OFFLINE ONLY')):
            self.runtime = build_workos_authkit_staging_runtime(config,
                sdk_boundary_factory=lambda **kw: self.boundary, clock=self.clock)
        self.browser = RemoteBetaIntegration(self.runtime, clock=self.clock)

    def stop(self):
        if self.runtime:
            self.runtime.close()
            self.runtime = None

    @staticmethod
    def _get_headers(*, cookie=None):
        return (('Host', 'beta.example.test'),) + ((('Cookie', cookie),) if cookie else ())

    @staticmethod
    def _post_headers(payload, *, cookie):
        return (('Host', 'beta.example.test'), ('Origin', ORIGIN), ('Sec-Fetch-Site','same-origin'),
            ('Cookie', cookie), ('Content-Type','application/x-www-form-urlencoded'), ('Content-Length',str(len(payload))))

    def test_fresh_storage_is_empty_m011_and_never_overwrites(self):
        for table in ('jobs','users','account_sessions','account_invitations'):
            self.assertEqual(self.seed.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
        with self.assertRaises(ValueError):
            initialize_fresh(self.database.parent)

    def test_remote_config_rejects_bad_origins_callbacks_switches_and_placeholders(self):
        for origin in ('http://beta.example.test','https://127.0.0.1','https://beta.example.test/',
                'https://evil@beta.example.test','https://beta.example.test:443','https://*.example.test'):
            with self.subTest(origin=origin):
                self.document['public_origin']=origin
                self.write_config()
                with self.assertRaises(WorkOSAuthKitStagingError):
                    load_workos_authkit_staging_configuration(str(self.config_path), remote_beta=True)
        self.document['public_origin']=ORIGIN
        for key, value in (('redirect_uri', ORIGIN+'/wrong'),('runtime_mode','practice'),
                ('proxy_secret','REPLACE_ME'),('practice_login',True),('version',1)):
            previous = dict(self.document)
            self.document[key]=value
            self.write_config()
            with self.subTest(key=key), self.assertRaises(WorkOSAuthKitStagingError):
                load_workos_authkit_staging_configuration(str(self.config_path), remote_beta=True)
            self.document=previous
        self.write_config()
        for name in ('WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED','OPENAI_API_KEY','WAHOJOBS_PRACTICE_LOGIN'):
            with patch.dict(os.environ,{name:'1'}), self.assertRaises(WorkOSAuthKitStagingError):
                load_workos_authkit_staging_configuration(str(self.config_path), remote_beta=True)

    def test_real_flow_shape_provisions_once_and_survives_restart(self):
        reply, target, code, tx_cookie = self._successful_login()
        cookies = '; '.join(_cookie_pair(reply, name) for name in (SESSION_COOKIE_NAME, SESSION_CSRF_COOKIE_NAME))
        for value in _header_values(reply, 'Set-Cookie'):
            self.assertIn('Secure', value)
            self.assertIn('SameSite=', value)
            self.assertNotIn('Domain=', value)
        reply.acknowledge_delivery()
        duplicate = self.browser.handle('GET',target,self._get_headers(cookie=tx_cookie))
        self.assertNotEqual(duplicate.status,303)
        self.assertEqual(self.boundary.exchange_count,1)
        self.assertEqual(self.seed.execute('SELECT count(*) FROM users').fetchone()[0],1)
        self.stop(); self.start()
        for path in ('/account/profile','/find-matches','/jobs'):
            page=self.browser.handle('GET',path,self._get_headers(cookie=cookies))
            self.assertEqual(page.status,200,path)
        # The accepted product requires a confirmed profile before My Jobs.
        self.assertEqual(self.browser.handle('GET','/tracker',self._get_headers(cookie=cookies)).status,409)
        self.assertEqual(self.browser.handle('GET','/jobs',self._get_headers()).status,303)
        url,tx=self._start()
        reply,_,_=self._callback(url,tx)
        self.assertEqual(reply.status,303)
        reply.acknowledge_delivery()
        self.assertEqual(self.seed.execute('SELECT count(*) FROM users').fetchone()[0],1)

    def test_expired_invalid_noninvited_and_wrong_method_do_not_provision(self):
        url,tx=self._start()
        reply,_,_=self._callback(url,tx)
        self.assertNotEqual(reply.status,303)
        invitation=create_invitation(self.seed,self.boundary.email)
        url,tx=self._start(invitation_token=invitation.invitation_token)
        self.clock.advance(timedelta(minutes=11))
        count=self.boundary.exchange_count
        reply,_,_=self._callback(url,tx)
        self.assertNotEqual(reply.status,303)
        self.assertEqual(self.boundary.exchange_count,count)
        url,tx=self._start(invitation_token=invitation.invitation_token)
        self.boundary.verified=False
        reply,_,_=self._callback(url,tx)
        self.assertNotEqual(reply.status,303)
        self.assertEqual(self.seed.execute('SELECT count(*) FROM users').fetchone()[0],0)

    def test_proxy_ingress_and_guest_catalog_gate_with_real_loopback_transport(self):
        server=BetaServer(('127.0.0.1',0),make_remote_handler(self.runtime,'a'*64,clock=self.clock))
        thread=threading.Thread(target=server.serve_forever); thread.start()
        try:
            for extra,status in (({},403),({PROXY_HEADER:'b'*64},403),
                    ({PROXY_HEADER:'a'*64,'X-Forwarded-Proto':'https'},403),
                    ({PROXY_HEADER:'a'*64,'Forwarded':'host=evil'},403),
                    ({PROXY_HEADER:'a'*64},200)):
                client=HTTPConnection(*server.server_address,timeout=5)
                try:
                    client.request('GET','/login',headers={'Host':'beta.example.test',**extra})
                    reply=client.getresponse(); self.assertEqual(reply.status,status); reply.read()
                finally: client.close()
            for path in ('/jobs','/company/alignerr','/job/opportunity-1'):
                self.assertEqual(self.browser.handle('GET',path,self._get_headers()).status,303)
            upload=BytesIO(b'should never be read')
            self.assertEqual(self.browser.handle('POST','/account/profile/intake',self._get_headers(),upload).status,404)
            self.assertEqual(upload.tell(),0)
            page=self.browser.handle('GET','/login?practice=alex',self._get_headers())
            self.assertNotIn(b'wahojobs_session=',str(page.headers).encode())
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_missing_config_never_binds_or_constructs_provider(self):
        with patch('scripts.private_beta_app.BetaServer') as server:
            with self.assertRaises(WorkOSAuthKitStagingError):
                run(str(self.root/'absent.json'),server_factory=server)
            server.assert_not_called()

    def test_private_config_staging_keeps_proxy_and_invitation_secrets_consistent(self):
        from scripts.private_beta_configure import stage_configuration
        target=self.root/'staged-configuration'
        stage_configuration(self.config_path,target)
        runtime=json.loads((target/'runtime.json').read_text())
        operations=json.loads((target/'invitation-operations.json').read_text())
        self.assertEqual((target/'invitation.key').read_bytes(),base64.b64decode(runtime['wahojobs_invitation_lookup_key_base64']))
        self.assertEqual(operations['account_invitation_lookup_key_file'],str(target/'invitation.key'))
        self.assertIn(runtime['proxy_secret'],(target/'caddy.env').read_text())
        configured=load_workos_authkit_staging_configuration(str(target/'runtime.json'),remote_beta=True)
        configured.clear_secrets()
        if os.name == 'posix':
            self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in target.iterdir()))
        with self.assertRaises(ValueError): stage_configuration(self.config_path,target)

    def test_operator_export_owner_isolation_and_account_closure(self):
        from scripts.private_beta_accounts import export_account, request_closure, status
        reply,_,_,_=self._successful_login()
        cookies='; '.join(_cookie_pair(reply,n) for n in (SESSION_COOKIE_NAME,SESSION_CSRF_COOKIE_NAME))
        reply.acknowledge_delivery()
        first=self.seed.execute('SELECT user_id,row_version FROM users').fetchone()
        self.boundary.subject='user_different12345678'; self.boundary.email='foreign@example.test'
        reply,_,_,_=self._successful_login(); reply.acknowledge_delivery()
        self.stop()
        self.assertEqual(status(self.database,first['user_id'])['row_version'],first['row_version'])
        export_account(self.database,first['user_id'],self.root/'account-export')
        exported=(self.root/'account-export'/'account-data.json').read_text()
        self.assertIn('person@example.test',exported)
        self.assertNotIn('foreign@example.test',exported)
        self.assertNotIn('token_hash',exported)
        self.assertNotIn('csrf_secret_hash',exported)
        result=request_closure(self.database,first['user_id'],expected_version=first['row_version'],
            request_id='synthetic-closure-request-001',cooling_days=2,purge_days=9,now=self.clock())
        self.assertFalse(result['data_erased'])
        # Exact idempotent replay goes through the existing lifecycle ledger.
        self.assertEqual(result,request_closure(self.database,first['user_id'],expected_version=first['row_version'],
            request_id='synthetic-closure-request-001',cooling_days=2,purge_days=9,now=self.clock()))
        self.start()
        self.assertEqual(self.browser.handle('GET','/account/profile',self._get_headers(cookie=cookies)).status,303)

    def test_failed_service_close_retains_database_ownership_until_terminal(self):
        integration=self.runtime._profile_integration
        original=type(integration).close
        with patch.object(type(integration),'close',return_value=False):
            with self.assertRaises(WorkOSAuthKitStagingError): self.runtime.close()
            with self.assertRaises(DatabaseLifetimeOwnershipError):
                acquire_database_lifetime_ownership(self.database,role=ROLE_DURABLE_RUNTIME)
        self.runtime.close(); self.runtime=None
        lease=acquire_database_lifetime_ownership(self.database,role=ROLE_DURABLE_RUNTIME)
        release_database_lifetime_ownership(lease,role=ROLE_DURABLE_RUNTIME,database_path=self.database)

    def test_failed_startup_cleanup_retains_lease_and_explicit_retry_finishes(self):
        from unittest.mock import Mock
        self.stop()
        configuration=load_workos_authkit_staging_configuration(str(self.config_path),remote_beta=True)
        service=Mock(); service.activate.return_value=True; service.close.return_value=False
        with (patch('wahojobs.workos_authkit_staging._build_profile_integration',return_value=service),
              patch('wahojobs.workos_authkit_staging.WorkOSAuthKitBrowserIntegration',side_effect=ValueError('OFFLINE'))):
            with self.assertRaises(WorkOSAuthKitStagingError) as failure:
                build_workos_authkit_staging_runtime(configuration,sdk_boundary_factory=lambda **kw:self.boundary)
        with self.assertRaises(DatabaseLifetimeOwnershipError):
            acquire_database_lifetime_ownership(self.database,role=ROLE_DURABLE_RUNTIME)
        service.close.return_value=True
        self.assertTrue(failure.exception.cleanup_owner.close())
        self.start()

    def test_activation_starts_then_fails_and_retains_incomplete_cleanup(self):
        from wahojobs.persistent_profiles_browser import PersistentProfileBrowserIntegration
        self.stop()
        configuration=load_workos_authkit_staging_configuration(str(self.config_path),remote_beta=True)
        original=PersistentProfileBrowserIntegration.activate
        def started_then_failed(integration):
            original(integration)
            raise ValueError('OFFLINE activation interruption')
        with (patch.object(PersistentProfileBrowserIntegration,'activate',started_then_failed),
              patch.object(PersistentProfileBrowserIntegration,'close',return_value=False)):
            with self.assertRaises(WorkOSAuthKitStagingError) as failure:
                build_workos_authkit_staging_runtime(configuration,sdk_boundary_factory=lambda **kw:self.boundary)
        with self.assertRaises(DatabaseLifetimeOwnershipError):
            acquire_database_lifetime_ownership(self.database,role=ROLE_DURABLE_RUNTIME)
        self.assertTrue(failure.exception.cleanup_owner.close())
        self.start()

    def test_existing_invitation_operator_on_m011_uses_private_neutral_configuration(self):
        from wahojobs import private_beta_invitation_operations as ops
        self.stop()
        key=self.root/'invitation.key'; key.write_bytes(INVITATION_KEY); key.chmod(0o600)
        config=self.root/'invitation-operations.json'
        config.write_text(json.dumps(dict(version=2,purpose='private_beta_invitations',environment='private_beta',
            database_path=str(self.database),account_invitation_lookup_key_file=str(key))))
        config.chmod(0o600)
        output=self.root/'credentials'; output.mkdir(mode=0o700)
        ops._harden_private_output_directory_for_testing(output)
        arguments=dict(configuration_path=config,database_path=self.database,invitation_key_path=key,
                       _clock=self.clock)
        result=ops.create_private_beta_invitation(**arguments,request_id='offline-beta-invitation-001',
            expires_at=(self.clock()+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            credential_output=output/'owner.json',hidden_email_reader=lambda:('person@example.test','person@example.test'))
        arguments['invitation_reference']=result.invitation_reference
        self.assertEqual(ops.status_private_beta_invitation(**arguments).status,'pending')
        ops.revoke_private_beta_invitation(**arguments)
        self.assertEqual(ops.status_private_beta_invitation(**arguments).status,'revoked')
        self.assertEqual(self.seed.execute('SELECT count(*) FROM users').fetchone()[0],0)

    def test_confirmed_profile_and_workflow_survive_remote_restart(self):
        from tests.candidate_continuity_support import add_candidate
        from tests.test_accepted_title_uncertainty import profile
        reply,_,_,_=self._successful_login()
        cookies='; '.join(_cookie_pair(reply,n) for n in (SESSION_COOKIE_NAME,SESSION_CSRF_COOKIE_NAME))
        reply.acknowledge_delivery()
        user=self.seed.execute('SELECT user_id FROM users').fetchone()[0]
        principal=self.seed.execute('SELECT principal_id FROM principal_account_bindings WHERE user_id=?',(user,)).fetchone()[0]
        # Supported confirmed-profile repository; all facts labelled synthetic.
        self.stop()
        pid=add_candidate(self.seed,profile('Customer support specialist',6),principal_id=principal,
                          account_id=user,now=self.clock(),key='offline-remote-confirmed-profile')
        self.start()
        for path in ('/account/profile','/find-matches','/tracker'):
            self.assertEqual(self.browser.handle('GET',path,self._get_headers(cookie=cookies)).status,200)
        self.stop(); self.start()
        self.assertEqual(self.browser.handle('GET','/tracker',self._get_headers(cookie=cookies)).status,200)
        self.assertEqual(self.seed.execute('SELECT count(*) FROM product_profiles WHERE profile_id=?',(pid,)).fetchone()[0],1)


class StorageRelocationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='offline-relocation-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        initialize_fresh(self.root/'source')
        self.source=self.root/'source'/'product.sqlite3'
        self.snapshot=self.root/'snapshot'
        self.destination=self.root/'recovered'

    def backup_restore(self):
        recovery.create_snapshot(self.source,self.snapshot,code_commit='b'*40,configuration_revision='offline')
        recovery.restore_snapshot(self.snapshot,self.destination)
        return self.destination/'product.sqlite3'

    def test_unpinned_restore_held_then_reconciled_and_source_retired(self):
        target=self.backup_restore()
        with self.assertRaises(DatabaseLifetimeOwnershipError):
            acquire_database_lifetime_ownership(target,role=ROLE_DURABLE_RUNTIME)
        reconcile_relocation(self.snapshot,self.destination,self.source)
        require_storage_activation(target)
        with self.assertRaises(DatabaseLifetimeOwnershipError):
            acquire_database_lifetime_ownership(self.source,role=ROLE_DURABLE_RUNTIME)
        self.assertEqual(self.source.read_bytes(),target.read_bytes())

    def test_stale_backup_and_unknown_authoritative_source_stay_held(self):
        target=self.backup_restore()
        with self.assertRaises((ValueError,FileNotFoundError)):
            reconcile_relocation(self.snapshot,self.destination,self.root/'missing.sqlite3')
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute('PRAGMA user_version=21')
        with self.assertRaisesRegex(ValueError,'history_changed'):
            reconcile_relocation(self.snapshot,self.destination,self.source)
        self.assertFalse(sidecar(target,LINEAGE).exists())
        self.assertFalse(sidecar(self.source,RETIRED).exists())

    def test_fence_interruption_same_input_retry_and_second_generation(self):
        target=self.backup_restore()
        def stop(stage):
            if stage=='source_retired': raise OSError('OFFLINE simulated interruption')
        with self.assertRaises(OSError):
            reconcile_relocation(self.snapshot,self.destination,self.source,checkpoint=stop)
        with self.assertRaises(ValueError): require_storage_activation(target)
        reconcile_relocation(self.snapshot,self.destination,self.source)
        self.source=target; self.snapshot=self.root/'second-snapshot'; self.destination=self.root/'second-recovery'
        second=self.backup_restore()
        reconcile_relocation(self.snapshot,self.destination,self.source)
        require_storage_activation(second)
        self.assertIsNotNone(json.loads(sidecar(second,LINEAGE).read_text())['predecessor'])

    def test_consumed_journal_preserved_and_fresh_maintenance_at_new_path(self):
        from tests.evidence_maintenance_support import source_execute,T0
        from wahojobs.evidence_maintenance import journal_binding,execute_plan,report
        journal=self.root/'journal'
        plan,result=source_execute(self.source,journal,T0)
        target=self.backup_restore()
        pin=sidecar(self.source,'.evidence-maintenance.json').read_bytes()
        reconcile_relocation(self.snapshot,self.destination,self.source)
        self.assertEqual(pin,sidecar(target,'.evidence-maintenance.json').read_bytes())
        self.assertEqual(report(self.destination/'journal',plan['plan_id']),result)
        replay=execute_plan(plan,self.destination/'journal',authorized=True,authorize_sources=True)
        self.assertEqual(replay,result)
        _,next_result=source_execute(target,self.destination/'journal',T0+timedelta(hours=73))
        self.assertIn(next_result['status'],('completed','partially_completed'))
        self.assertEqual(journal_binding(target)['journal_root'],str(self.destination/'journal'))

    def test_journal_only_new_consumed_reservation_blocks_stale_restore(self):
        from tests.evidence_maintenance_support import source_execute,T0
        journal=self.root/'journal'
        source_execute(self.source,journal,T0)
        self.backup_restore()
        (journal/'uncertain-request.json').write_text('{"consumed":true}')
        with self.assertRaisesRegex(ValueError,'history_changed'):
            reconcile_relocation(self.snapshot,self.destination,self.source)

    def test_nonparticipating_writer_blocks_handoff(self):
        self.backup_restore()
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                reconcile_relocation(self.snapshot,self.destination,self.source)
            writer.rollback()
        self.assertFalse(sidecar(self.source,RETIRED).exists())

    def test_tampered_lineage_or_copied_activation_does_not_authorize(self):
        target=self.backup_restore()
        reconcile_relocation(self.snapshot,self.destination,self.source)
        record=json.loads(sidecar(target,LINEAGE).read_text())
        record['database']['inode']+=1
        sidecar(target,LINEAGE).write_text(json.dumps(record))
        with self.assertRaises(DatabaseLifetimeOwnershipError):
            acquire_database_lifetime_ownership(target,role=ROLE_DURABLE_RUNTIME)

    def test_unpinned_then_first_maintenance_and_multigeneration_lineage(self):
        from tests.evidence_maintenance_support import source_execute,T0
        target=self.backup_restore()
        reconcile_relocation(self.snapshot,self.destination,self.source)
        source_execute(target,self.destination/'journal',T0)
        ancestor=sidecar(target,LINEAGE).read_bytes()
        self.source=target; self.snapshot=self.root/'snapshot-2'; self.destination=self.root/'recovered-2'
        second=self.backup_restore()
        reconcile_relocation(self.snapshot,self.destination,self.source)
        history=self.destination/'lineage-history'
        self.assertIn(ancestor,[p.read_bytes() for p in history.iterdir()])
        recovery.create_snapshot(second,self.root/'snapshot-3',code_commit='b'*40,configuration_revision='offline')
        inherited=next(history.iterdir()); inherited.unlink()
        with self.assertRaises(ValueError): require_storage_activation(second)
        with self.assertRaises(DatabaseLifetimeOwnershipError):
            recovery.create_snapshot(second,self.root/'snapshot-invalid',code_commit='b'*40,configuration_revision='offline')

    def test_invalid_target_and_destination_persist_failure_precede_fence(self):
        target=self.backup_restore()
        sidecar(target,RETIRED).write_text('{}')
        with self.assertRaisesRegex(ValueError,'destination_retired'):
            reconcile_relocation(self.snapshot,self.destination,self.source)
        sidecar(target,RETIRED).unlink()
        sidecar(target,LINEAGE).write_text('{}')
        with self.assertRaisesRegex(ValueError,'already_assigned'):
            reconcile_relocation(self.snapshot,self.destination,self.source)
        sidecar(target,LINEAGE).unlink()
        history=self.destination/'lineage-history'; history.mkdir()
        unexpected=history/('a'*64+'.json'); unexpected.write_text('{}')
        with self.assertRaisesRegex(ValueError,'lineage_invalid'):
            reconcile_relocation(self.snapshot,self.destination,self.source)
        unexpected.unlink()
        with patch('wahojobs.storage_relocation._persist_destination',side_effect=OSError('OFFLINE fsync failure')):
            with self.assertRaises(OSError): reconcile_relocation(self.snapshot,self.destination,self.source)
        self.assertFalse(sidecar(self.source,RETIRED).exists())
        stages=[]
        reconcile_relocation(self.snapshot,self.destination,self.source,checkpoint=stages.append)
        self.assertEqual(stages,['destination_persisted','source_retired','destination_activated'])

    def test_companion_presence_identity_and_writer_contention(self):
        from wahojobs.professional_background_store import initialize_preparation_store
        companion=self.root/'companion.sqlite3'
        with closing(sqlite3.connect(companion)) as connection: initialize_preparation_store(connection)
        recovery.create_snapshot(self.source,self.snapshot,companion=companion,code_commit='b'*40,configuration_revision='offline')
        recovery.restore_snapshot(self.snapshot,self.destination)
        with self.assertRaisesRegex(ValueError,'companion_required'):
            reconcile_relocation(self.snapshot,self.destination,self.source)
        for path in (companion,self.destination/'companion.sqlite3'):
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError):
                    reconcile_relocation(self.snapshot,self.destination,self.source,authoritative_companion=companion)
                writer.rollback()
            self.assertFalse(sidecar(self.source,RETIRED).exists())
        reconcile_relocation(self.snapshot,self.destination,self.source,authoritative_companion=companion)

    def test_interrupted_enrichment_consumed_reservation_survives_relocation(self):
        from tests.evidence_maintenance_support import source_execute,T0,offline_enrichment,COHORT,TRANSPORT
        from wahojobs import evidence_maintenance as maintenance
        journal=self.root/'journal'
        source_execute(self.source,journal,T0)
        state=maintenance.build_plan(self.source,COHORT,now=T0)
        canonical=state['sources'][1]['jobs'][0]['canonical_id']
        repair=offline_enrichment(canonical,journal); repair.client.session.interrupt=True
        plan=maintenance.build_plan(self.source,COHORT,now=T0,phase='derived',enrichment=repair,transport_binding=TRANSPORT)
        with self.assertRaises(KeyboardInterrupt):
            maintenance.execute_plan(plan,journal,authorized=True,authorize_enrichment=True,enrichment=repair,
                                     now=T0,transport_binding=TRANSPORT)
        target=self.backup_restore()
        reconcile_relocation(self.snapshot,self.destination,self.source)
        new_repair=offline_enrichment(canonical,self.destination/'journal')
        fresh=maintenance.build_plan(target,COHORT,now=T0+timedelta(seconds=1),phase='derived',enrichment=new_repair)
        self.assertIn('enrichment_attempt_already_consumed',fresh['enrichment_scope']['blocked'])
        self.assertEqual(new_repair.client.session.calls,0)
        self.assertEqual(maintenance.report(self.destination/'journal',plan['plan_id'])['status'],'interrupted')


del WorkOSAuthKitBrowserTests  # Helpers reused; do not select the imported suite twice.

if __name__=='__main__': unittest.main()
