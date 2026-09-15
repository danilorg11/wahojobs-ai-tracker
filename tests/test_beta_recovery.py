"""Cold recovery through supported composition, disposable storage, no listeners."""
from contextlib import closing, redirect_stderr
import base64
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from wahojobs import beta_recovery as recovery
from wahojobs.database_lifetime_ownership import (
    ROLE_DURABLE_RUNTIME, acquire_database_lifetime_ownership,
    release_database_lifetime_ownership, DatabaseLifetimeOwnershipError,
)
from tests.evidence_maintenance_support import (
    new_inventory, source_execute, seed_owner, prepare_cycle, T0,
)


class BetaRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='beta-recovery-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / 'fixture'
        self.database = new_inventory(self.directory)
        self.snapshot = self.root / 'snapshot'
        self.restored = self.root / 'restored'

    def backup(self, **kwargs):
        return recovery.create_snapshot(self.database, self.snapshot,
            code_commit='b' * 40, configuration_revision='synthetic-config-v1', **kwargs)

    def test_full_snapshot_restore_preserves_private_state_consumed_attempts_and_immutable_pin(self):
        source_execute(self.database, self.directory / 'journal', T0)
        owner = seed_owner(self.database, self.directory)
        prepare_cycle(self.directory, T0)
        from wahojobs.profile_correction_drafts import store_connection, save, load
        with closing(sqlite3.connect(self.database)) as connection:
            with store_connection(connection, write=True) as drafts:
                save(drafts, reference='synthetic-recovery-proposal', owner='synthetic-owner',
                     base_revision='synthetic-revision', base_hash='c' * 64,
                     payload={'city': 'Synthetic review draft'}, created_at=T0.isoformat())
        companion = self.directory / 'professional-background.sqlite3'
        with patch('socket.create_connection', side_effect=AssertionError('No external requests')):
            manifest = self.backup(companion=companion)
            receipt = recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertTrue(manifest['maintenance_pinned'])
        self.assertEqual(receipt['maintenance'], 'held_original_physical_identity')
        recovered = self.restored / 'product.sqlite3'
        self.assertEqual(self.database.read_bytes(), recovered.read_bytes())
        self.assertEqual(companion.read_bytes(), (self.restored / 'companion.sqlite3').read_bytes())
        pin = self.database.with_name(self.database.name + '.evidence-maintenance.json')
        self.assertEqual(pin.read_bytes(), (self.restored / 'product.sqlite3.evidence-maintenance.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'maintenance_journal_database_identity_changed'):
            recovery.journal_binding(recovered)
        with closing(sqlite3.connect(recovered)) as connection:
            with store_connection(connection) as drafts:
                self.assertEqual(load(drafts, owner='synthetic-owner')[3]['city'], 'Synthetic review draft')
                self.assertIsNone(load(drafts, owner='different-owner'))
        # Fresh normal runtime can consume recovered profiles/preparations while
        # the original pinned source maintenance is explicitly held. Session
        # return is evidence of restoration, never new-account/provider login.
        self.assert_recovered_runtime(recovered, owner)
        self.assertTrue(self.database.exists())
        self.assertTrue(Path(str(self.database) + '.wahojobs-lifetime.lock').exists())

    def assert_recovered_runtime(self, database, owner):
        from wahojobs.workos_authkit_staging import (
            load_workos_authkit_staging_configuration, build_workos_authkit_staging_runtime,
            STAGING_PUBLIC_ORIGIN, STAGING_REDIRECT_URI,
        )
        from tests.workos_authkit_test_support import FakeWorkOSBoundary
        config = self.root / 'synthetic-runtime.json'
        config.write_text(json.dumps(dict(version=1, environment_namespace='private_beta',
            database_path=str(database), public_origin=STAGING_PUBLIC_ORIGIN,
            redirect_uri=STAGING_REDIRECT_URI, workos_client_id='client_0123456789abcdef',
            workos_api_key='sk_test_' + secrets.token_urlsafe(32),
            wahojobs_invitation_lookup_key_base64=base64.b64encode(secrets.token_bytes(32)).decode(),
            session_idle_ttl_seconds=3600, session_absolute_ttl_seconds=28800,
            professional_background_companion=dict(path=str(self.restored / 'companion.sqlite3'),
                model='gpt-5.4-mini', basis='offline_labelled_stub'))), encoding='utf-8')
        if os.name != 'nt':
            config.chmod(0o600)
        configuration = load_workos_authkit_staging_configuration(str(config))
        with (patch('wahojobs.profile_intake.openai_adapter.configured_openai_profile_adapter', return_value=None),
              patch('socket.create_connection', side_effect=AssertionError('No external requests'))):
            runtime = build_workos_authkit_staging_runtime(configuration,
                sdk_boundary_factory=lambda **kwargs: FakeWorkOSBoundary(), clock=lambda: T0)
        configuration.clear_secrets()
        try:
            headers = [('Host', '127.0.0.1:8443'), ('Cookie', 'wahojobs_session=' + owner['session_token']
                + '; __Host-wahojobs_session_csrf=' + owner['csrf_secret'])]
            page = runtime.browser_integration.handle('GET', '/account/profile', headers)
            self.assertEqual(page.status, 200)
            # Protected GETs issue no session delivery lease. Auth callback
            # responses own that optional contract, as the HTTP adapter checks.
            if callable(getattr(page, 'acknowledge_delivery', None)):
                page.acknowledge_delivery()
            page = runtime.browser_integration.handle('GET', '/find-matches', headers)
            self.assertEqual(page.status, 200)
            if callable(getattr(page, 'acknowledge_delivery', None)):
                page.acknowledge_delivery()
        finally:
            runtime.close()

    def test_snapshot_rejects_running_database_owner_and_keeps_lock(self):
        lease = acquire_database_lifetime_ownership(self.database, role=ROLE_DURABLE_RUNTIME)
        try:
            with self.assertRaises(DatabaseLifetimeOwnershipError):
                self.backup()
            self.assertFalse(self.snapshot.exists())
        finally:
            release_database_lifetime_ownership(lease, role=ROLE_DURABLE_RUNTIME, database_path=self.database)
        self.assertTrue(Path(str(self.database) + '.wahojobs-lifetime.lock').exists())

    def test_snapshot_rejects_nonparticipating_sqlite_writer_without_touching_source(self):
        before = self.database.read_bytes()
        with closing(sqlite3.connect(self.database)) as writer:
            # A RESERVED write transaction can have no journal yet. The cold
            # source probe must reject it even though the lifetime registry is free.
            writer.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    self.backup()
                self.assertTrue(writer.in_transaction)
                self.assertEqual(self.database.read_bytes(), before)
                self.assertFalse(self.snapshot.exists())
            finally:
                writer.rollback()

    def test_restore_never_overwrites_existing_destination_or_source(self):
        self.backup()
        before = self.database.read_bytes()
        for destination in (self.directory, self.snapshot):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, 'new_absolute_directory'):
                recovery.restore_snapshot(self.snapshot, destination)
        self.assertEqual(self.database.read_bytes(), before)

    def test_corrupted_backup_is_rejected_before_restore_directory_created(self):
        self.backup()
        with (self.snapshot / 'product.sqlite3').open('ab') as stream:
            stream.write(b'damaged')
        with self.assertRaisesRegex(ValueError, 'snapshot_integrity_failed'):
            recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertFalse(self.restored.exists())

    def test_interrupted_snapshot_and_restore_have_no_success_receipt(self):
        with patch.object(recovery.shutil, 'copyfileobj', side_effect=OSError('synthetic-interruption')):
            with self.assertRaises(OSError):
                self.backup()
        self.assertFalse((self.snapshot / 'COMPLETE.sha256').exists())
        self.snapshot = self.root / 'new-snapshot'
        self.backup()
        with patch.object(recovery.shutil, 'copyfileobj', side_effect=OSError('synthetic-interruption')):
            with self.assertRaises(OSError):
                recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertFalse((self.restored / 'RECOVERY-READY.json').exists())

    def test_source_change_during_copy_is_rejected_and_not_completed(self):
        original = recovery.shutil.copyfileobj
        def changing_copy(source, target, *args, **kwargs):
            original(source, target, *args, **kwargs)
            if source.name == str(self.database):
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute('PRAGMA user_version=99')
        with patch.object(recovery.shutil, 'copyfileobj', side_effect=changing_copy):
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                self.backup()
        self.assertFalse((self.snapshot / 'COMPLETE.sha256').exists())

    def test_unconfigured_companion_is_explicit_and_config_secrets_not_copied(self):
        (self.directory / 'private-config.json').write_text('SYNTHETIC secret not selected')
        manifest = self.backup()
        self.assertFalse(manifest['companion_configured'])
        self.assertEqual(set(manifest['files']), {'product.sqlite3'})
        self.assertNotIn(b'SYNTHETIC secret', (self.snapshot / 'manifest.json').read_bytes())
        receipt = recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertEqual(receipt['maintenance'], 'unconfigured')

    def test_sidecars_and_relative_paths_are_rejected(self):
        sidecar = Path(str(self.database) + '-wal')
        sidecar.write_bytes(b'synthetic')
        with self.assertRaises(ValueError):
            self.backup()
        self.assertFalse(self.snapshot.exists())
        with self.assertRaises(ValueError):
            recovery.verify_snapshot(Path('relative'))

    def test_verification_rejects_each_snapshot_sidecar_before_sqlite_and_preserves_bytes(self):
        self.backup()
        product = self.snapshot / 'product.sqlite3'
        before = product.read_bytes()
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = Path(str(product) + suffix)
            sidecar.write_bytes(b'synthetic-sidecar-must-be-preserved')
            try:
                with patch.object(recovery.sqlite3, 'connect', side_effect=AssertionError('SQLite must not open')):
                    with self.assertRaisesRegex(ValueError, 'sqlite_sidecars_present'):
                        recovery.verify_snapshot(self.snapshot)
                self.assertEqual(product.read_bytes(), before)
                self.assertEqual(sidecar.read_bytes(), b'synthetic-sidecar-must-be-preserved')
                with self.assertRaisesRegex(ValueError, 'sqlite_sidecars_present'):
                    recovery.restore_snapshot(self.snapshot, self.restored)
                self.assertFalse(self.restored.exists())
            finally:
                sidecar.unlink()  # This exact synthetic test-owned file only.

    def test_hot_journal_verification_never_recovers_or_claims_success(self):
        self.backup()
        product = self.snapshot / 'product.sqlite3'
        child = '''import os, sqlite3, sys
c=sqlite3.connect(sys.argv[1])
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
c.execute('CREATE TABLE unfinished_recovery_fixture (data BLOB)')
c.execute('INSERT INTO unfinished_recovery_fixture VALUES (zeroblob(1000000))')
os._exit(0)
'''
        result = subprocess.run([sys.executable, '-B', '-c', child, str(product)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        journal = Path(str(product) + '-journal')
        self.assertTrue(journal.exists())
        crashed, journal_bytes = product.read_bytes(), journal.read_bytes()
        # Reproduce the review's hash-valid crashed snapshot. Exact product
        # attestation must not recover it into a different valid old database.
        manifest = json.loads((self.snapshot / 'manifest.json').read_text())
        manifest['files']['product.sqlite3']['sha256'] = recovery.sha256(crashed).hexdigest()
        raw = recovery._json(manifest)
        (self.snapshot / 'manifest.json').write_bytes(raw)
        (self.snapshot / 'COMPLETE.sha256').write_text(recovery.sha256(raw).hexdigest() + '\n')
        with self.assertRaisesRegex(ValueError, 'sqlite_sidecars_present'):
            recovery.verify_snapshot(self.snapshot)
        self.assertEqual(product.read_bytes(), crashed)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        with self.assertRaisesRegex(ValueError, 'sqlite_sidecars_present'):
            recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertFalse(self.restored.exists())

    def test_cli_failure_reports_allowlisted_category_without_private_exception_content(self):
        from scripts.beta_recovery import main
        self.backup()
        Path(str(self.snapshot / 'product.sqlite3') + '-journal').write_bytes(b'synthetic')
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(['verify', '--snapshot', str(self.snapshot)]), 2)
        self.assertIn('recovery_sqlite_sidecars_present', output.getvalue())
        self.assertNotIn(str(self.snapshot), output.getvalue())
        for error in (ValueError('private-profile/session=SECRET'), OSError('private-owner/path=SECRET')):
            output = io.StringIO()
            with patch('scripts.beta_recovery.verify_snapshot', side_effect=error), redirect_stderr(output):
                self.assertEqual(main(['verify', '--snapshot', str(self.snapshot)]), 2)
            self.assertNotIn('SECRET', output.getvalue())
            self.assertNotIn('private-', output.getvalue())


if __name__ == '__main__':
    unittest.main()
