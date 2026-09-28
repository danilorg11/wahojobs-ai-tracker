"""Online evidence staging preserves the existing cold recovery authority."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from wahojobs import beta_recovery as recovery
from wahojobs.database_lifetime_ownership import (
    acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_DURABLE_RUNTIME,
    DatabaseLifetimeOwnershipError)
from tests.evidence_maintenance_support import source_execute, T0
from tests import test_beta_recovery as existing


class OnlineJournalPreparationTests(unittest.TestCase):
    setUp = existing.BetaRecoveryTests.setUp
    backup = existing.BetaRecoveryTests.backup

    def prepare(self):
        self.prepared = self.root / 'prepared'
        receipt = recovery.prepare_snapshot_journal(self.database, self.prepared, run_id='daily-fixture',
            code_commit='b' * 40, configuration_revision='synthetic-config-v1')
        self.preparation_sha256 = sha256(recovery._json(receipt)).hexdigest()
        return receipt

    def finish(self, **changes):
        options = dict(prepared_journal=self.prepared, run_id='daily-fixture',
            code_commit='b' * 40, configuration_revision='synthetic-config-v1',
            expected_preparation_sha256=self.preparation_sha256)
        options.update(changes)
        return recovery.create_snapshot(self.database, self.snapshot, **options)

    def seed_journal(self):
        source_execute(self.database, self.directory / 'journal', T0)
        return next((self.directory / 'journal').rglob('*.raw'))

    def test_runtime_writes_during_preparation_survive_v1_snapshot_restore_and_reconciliation(self):
        self.seed_journal()
        lease = acquire_database_lifetime_ownership(self.database, role=ROLE_DURABLE_RUNTIME)
        try:
            with patch.object(recovery, '_check_sqlite', side_effect=AssertionError('No online SQLite validation')):
                with patch.object(recovery, 'report', wraps=recovery.report) as reports:
                    receipt = self.prepare()
                    self.assertGreater(reports.call_count, 0)
            self.assertNotIn('product.sqlite3', receipt['files'])
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute('PRAGMA user_version=23')
        finally:
            release_database_lifetime_ownership(lease, role=ROLE_DURABLE_RUNTIME, database_path=self.database)
        current = self.database.read_bytes()
        with patch.object(recovery, 'report', side_effect=AssertionError('Chains already validated online')):
            with patch.object(recovery.shutil, 'copyfileobj', wraps=recovery.shutil.copyfileobj) as copies:
                manifest = self.finish()
        self.assertEqual(manifest['version'], recovery.VERSION)
        self.assertFalse(any('/journal/' in Path(call.args[0].name).as_posix() for call in copies.call_args_list))
        self.assertEqual(recovery.verify_snapshot(self.snapshot), manifest)
        self.assertEqual((self.snapshot / 'product.sqlite3').read_bytes(), current)
        recovery.restore_snapshot(self.snapshot, self.restored)
        from wahojobs.storage_relocation import reconcile_relocation
        reconcile_relocation(self.snapshot, self.restored, self.database)
        self.assertEqual((self.restored / 'product.sqlite3').read_bytes(), current)
        self.assertFalse((self.prepared / 'journal').exists())

    def test_invalid_chain_has_no_prepared_success_marker(self):
        raw = self.seed_journal()
        raw.write_bytes(raw.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.prepare()
        self.assertFalse((self.root / 'prepared/PREPARED.sha256').exists())
        self.assertFalse(self.snapshot.exists())

    def test_file_limit_is_checked_online_before_any_copy_or_stop(self):
        self.seed_journal()
        with patch.object(recovery, 'MAX_FILES', 2):
            with self.assertRaisesRegex(ValueError, 'snapshot_file_limit'):
                self.prepare()
        self.assertFalse((self.root / 'prepared').exists())

    def test_source_mutation_during_preparation_is_rejected(self):
        raw = self.seed_journal()
        original = recovery.shutil.copyfileobj
        def changing_copy(source, target, *args, **kwargs):
            original(source, target, *args, **kwargs)
            if source.name == str(raw):
                raw.write_bytes(raw.read_bytes() + b'changed after copying')
        with patch.object(recovery.shutil, 'copyfileobj', side_effect=changing_copy):
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                self.prepare()
        self.assertFalse((self.prepared / 'PREPARED.sha256').exists())

    def test_absent_expired_or_wrong_binding_cannot_enter_cold_copy(self):
        self.seed_journal()
        self.prepare()
        for changes in (dict(run_id='other-run'), dict(code_commit='c' * 40),
                        dict(configuration_revision='other-config'), dict(prepared_journal=self.root / 'absent')):
            with self.subTest(changes=changes), self.assertRaises((ValueError, OSError)):
                self.finish(**changes)
            self.assertFalse(self.snapshot.exists())
        receipt = json.loads((self.prepared / 'PREPARED.json').read_text())
        receipt['created_at'] = (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
        raw = recovery._json(receipt)
        (self.prepared / 'PREPARED.json').write_bytes(raw)
        (self.prepared / 'PREPARED.sha256').write_text(sha256(raw).hexdigest() + '\n')
        self.preparation_sha256 = sha256(raw).hexdigest()  # Explicitly authorize this synthetic expired receipt.
        with self.assertRaisesRegex(ValueError, 'preparation_expired'):
            self.finish()
        self.assertFalse(self.snapshot.exists())

    def test_forged_disk_receipt_cannot_replace_the_trusted_chain_proof(self):
        raw = self.seed_journal()
        receipt = self.prepare()
        with self.assertRaisesRegex(ValueError, 'preparation_integrity_failed'):
            self.finish(expected_preparation_sha256=None)
        name = 'journal/' + raw.relative_to(self.directory / 'journal').as_posix()
        damaged = raw.read_bytes() + b'broken chain with matching rewritten disk checksums'
        raw.write_bytes(damaged)
        (self.prepared / name).write_bytes(damaged)
        receipt['files'][name]['sha256'] = sha256(damaged).hexdigest()
        forged = recovery._json(receipt)
        (self.prepared / 'PREPARED.json').write_bytes(forged)
        (self.prepared / 'PREPARED.sha256').write_text(sha256(forged).hexdigest() + '\n')
        with self.assertRaisesRegex(ValueError, 'preparation_integrity_failed'):
            self.finish()
        self.assertFalse(self.snapshot.exists())

    def test_current_membership_and_artifact_bytes_are_rechecked(self):
        raw = self.seed_journal()
        self.prepare()
        extra = self.directory / 'journal/new-claim.json'
        extra.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'preparation_binding_invalid'):
            self.finish()
        extra.unlink()
        extra = self.prepared / 'unexpected.json'
        extra.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'preparation_integrity_failed'):
            self.finish()
        extra.unlink()
        artifact = self.prepared / 'journal' / raw.relative_to(self.directory / 'journal')
        artifact.write_bytes(artifact.read_bytes() + b'changed artifact')
        with self.assertRaisesRegex(ValueError, 'preparation_integrity_failed'):
            self.finish()
        self.assertFalse(self.snapshot.exists())

    def test_source_change_after_adoption_still_has_no_complete_marker(self):
        raw = self.seed_journal()
        self.prepare()
        original = recovery.shutil.copyfileobj
        def changing_copy(source, target, *args, **kwargs):
            original(source, target, *args, **kwargs)
            if source.name == str(self.database):
                raw.write_bytes(raw.read_bytes() + b'changed while copying current database')
        with patch.object(recovery.shutil, 'copyfileobj', side_effect=changing_copy):
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                self.finish()
        self.assertFalse((self.snapshot / 'COMPLETE.sha256').exists())

    def test_empty_or_unconfigured_journal_uses_unchanged_v1_manifest(self):
        receipt = self.prepare()
        self.assertEqual(receipt['files'], {})
        manifest = self.finish()
        self.assertEqual(set(manifest['files']), {'product.sqlite3'})
        self.assertEqual(recovery.verify_snapshot(self.snapshot), manifest)

    def test_relocated_inventory_keeps_lineage_validation_and_rejects_tampering(self):
        raw = self.seed_journal()
        self.backup()
        recovery.restore_snapshot(self.snapshot, self.restored)
        from wahojobs import storage_relocation as relocation
        relocation.reconcile_relocation(self.snapshot, self.restored, self.database)
        recovered = self.restored / 'product.sqlite3'
        with patch.object(relocation, 'relocation_binding', wraps=relocation.relocation_binding) as binding:
            recovery._inventory(recovered, None)
            self.assertEqual(binding.call_count, 2)
        artifact = self.restored / 'journal' / raw.relative_to(self.directory / 'journal')
        artifact.write_bytes(artifact.read_bytes() + b'changed retained relocation history')
        with self.assertRaisesRegex(ValueError, 'lineage_invalid'):
            recovery._inventory(recovered, None)
        with self.assertRaises(DatabaseLifetimeOwnershipError):
            recovery.create_snapshot(recovered, self.root / 'second-snapshot',
                code_commit='b' * 40, configuration_revision='synthetic-config-v1')
        self.assertFalse((self.root / 'second-snapshot/COMPLETE.sha256').exists())

    def test_relocated_database_without_original_pin_does_not_invent_one(self):
        self.backup()
        recovery.restore_snapshot(self.snapshot, self.restored)
        from wahojobs import storage_relocation as relocation
        relocation.reconcile_relocation(self.snapshot, self.restored, self.database)
        recovered = self.restored / 'product.sqlite3'
        self.assertIsNotNone(relocation.relocation_binding(recovered))
        self.assertEqual(set(recovery._inventory(recovered, None)),
            {'product.sqlite3', 'maintenance-lineage.json', 'recovery-hold.json'})
        journal = self.restored / 'journal'
        from wahojobs.evidence_maintenance import pin_journal
        pin_journal(recovered, journal)
        pin = recovered.with_name(recovered.name + '.evidence-maintenance.json')
        self.assertTrue(pin.exists())
        self.assertIn('maintenance-pin.json', recovery._inventory(recovered, None))


if __name__ == '__main__':
    unittest.main()
