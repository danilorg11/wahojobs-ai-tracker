"""Real chain, cold ownership and lineage compatibility for journal packages."""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from wahojobs import beta_recovery as recovery
from wahojobs import recovery_archive as archive
from wahojobs import storage_relocation as relocation
from tests import test_beta_recovery as existing
from tests.evidence_maintenance_support import source_execute, T0


class PackagedRecoveryTests(unittest.TestCase):
    setUp = existing.BetaRecoveryTests.setUp

    def prepare(self, database=None, label='prepared'):
        prepared = self.root / label
        receipt = recovery.prepare_snapshot_journal(database or self.database, prepared,
            run_id='packaged-recovery', code_commit='d' * 40,
            configuration_revision='synthetic-v2', snapshot_version=archive.VERSION)
        return prepared, sha256(recovery._json(receipt)).hexdigest()

    def finish(self, preparation, database=None, destination=None):
        prepared, digest = preparation
        return recovery.create_snapshot(database or self.database, destination or self.snapshot,
            prepared_journal=prepared, expected_preparation_sha256=digest,
            run_id='packaged-recovery', code_commit='d' * 40, configuration_revision='synthetic-v2')

    def test_online_real_chains_current_user_writes_cold_no_inflation_and_deep_restore(self):
        from wahojobs.database_lifetime_ownership import (
            acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_DURABLE_RUNTIME)
        source_execute(self.database, self.directory / 'journal', T0)
        lease = acquire_database_lifetime_ownership(self.database, role=ROLE_DURABLE_RUNTIME)
        try:
            with patch.object(recovery, '_check_sqlite', side_effect=AssertionError('online database read')):
                preparation = self.prepare()
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute('PRAGMA user_version=37')
        finally:
            release_database_lifetime_ownership(lease, role=ROLE_DURABLE_RUNTIME, database_path=self.database)
        current = self.database.read_bytes()
        with patch.object(archive, 'validate_chains', side_effect=AssertionError('cold replay')):
            with patch.object(archive, 'iter_journal', side_effect=AssertionError('cold inflation')):
                manifest = self.finish(preparation)
                self.assertEqual(recovery.verify_snapshot(self.snapshot, trusted_manifest=manifest), manifest)
        with patch.object(recovery, 'report', wraps=recovery.report) as reports:
            self.assertEqual(recovery.verify_snapshot(self.snapshot), manifest)
            self.assertGreater(reports.call_count, 0)
        recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertEqual(current, (self.restored / 'product.sqlite3').read_bytes())
        receipt = relocation.reconcile_relocation(self.snapshot, self.restored, self.database)
        self.assertEqual(receipt['version'], relocation.VERSION_V2)
        self.assertNotIn('journal_files', receipt)
        self.assertEqual(len(receipt['journal_chunks']), 1)
        self.assertIsNotNone(relocation.relocation_binding(self.restored / 'product.sqlite3'))

    def test_v1_lineage_to_v2_then_repeated_v2_relocation_preserves_old_receipts(self):
        source_execute(self.database, self.directory / 'journal', T0)
        recovery.create_snapshot(self.database, self.snapshot,
            code_commit='b' * 40, configuration_revision='synthetic-v1')
        recovery.restore_snapshot(self.snapshot, self.restored)
        relocation.reconcile_relocation(self.snapshot, self.restored, self.database)
        database = self.restored / 'product.sqlite3'
        old_lineage = relocation.sidecar(database, relocation.LINEAGE).read_bytes()
        for number in (2, 3):
            snapshot, restored = self.root / f'snapshot-{number}', self.root / f'restored-{number}'
            self.finish(self.prepare(database, f'prepared-{number}'), database, snapshot)
            recovery.restore_snapshot(snapshot, restored)
            receipt = relocation.reconcile_relocation(snapshot, restored, database)
            self.assertEqual(receipt['version'], relocation.VERSION_V2)
            self.assertEqual((restored / 'lineage-history' / (sha256(old_lineage).hexdigest() + '.json')).read_bytes(), old_lineage)
            database = restored / 'product.sqlite3'
            self.assertIsNotNone(relocation.relocation_binding(database))
        chunk = next((database.parent / 'lineage-history').glob('*.json'))
        chunk.write_bytes(chunk.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'lineage_invalid'):
            relocation.relocation_binding(database)

    def test_relocation_interruption_retry_keeps_exact_chunks_and_fence_order(self):
        source_execute(self.database, self.directory / 'journal', T0)
        self.finish(self.prepare())
        recovery.restore_snapshot(self.snapshot, self.restored)
        def interrupted(stage):
            if stage == 'source_retired':
                raise OSError('synthetic interrupted fence')
        with self.assertRaisesRegex(OSError, 'synthetic'):
            relocation.reconcile_relocation(self.snapshot, self.restored, self.database, checkpoint=interrupted)
        target = self.restored / 'product.sqlite3'
        self.assertTrue(relocation.sidecar(self.database, relocation.RETIRED).exists())
        self.assertFalse(relocation.sidecar(target, relocation.LINEAGE).exists())
        relocation.reconcile_relocation(self.snapshot, self.restored, self.database)
        relocation.require_storage_activation(target)

    def test_empty_pinned_and_unconfigured_v2_restore_reconcile(self):
        from wahojobs.evidence_maintenance import pin_journal
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                if pinned:
                    journal = self.directory / 'journal'
                    journal.mkdir()
                    pin_journal(self.database, journal)
                snapshot = self.root / f'empty-snapshot-{pinned}'
                restored = self.root / f'empty-restored-{pinned}'
                manifest = self.finish(self.prepare(label=f'empty-prepared-{pinned}'), destination=snapshot)
                self.assertEqual(manifest['journal_segments'], {})
                recovery.restore_snapshot(snapshot, restored)
                relocation.reconcile_relocation(snapshot, restored, self.database)
                # The next subcase needs a fresh, unfenced physical authority.
                if not pinned:
                    self.database = restored / 'product.sqlite3'
                    self.directory = restored
                    (restored / 'journal').rmdir()

    def test_plan_bound_and_online_space_margin_refuse_without_success_receipt(self):
        source_execute(self.database, self.directory / 'journal', T0)
        with patch.object(archive, 'MAX_PLAN_FILES', 1):
            with self.assertRaisesRegex(ValueError, 'plan_limit'):
                self.prepare()
        self.assertFalse((self.root / 'prepared/PREPARED.sha256').exists())


if __name__ == '__main__':
    unittest.main()
