from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.public_job_identity_migration import (
    apply_public_job_identity_migration,
)
from tests.workos_authkit_test_support import build_m008
from wahojobs import public_job_identity as identity


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)


def _seed_canonical(connection, canonical_id):
    company_id = canonical_id + 10_000
    connection.execute(
        "INSERT INTO companies "
        "(id,name,slug,careers_url,source_tier,inventory_model,market_count_policy) "
        "VALUES (?, 'Synthetic Portability', 'synthetic-portability', "
        "'https://example.test/', 'core', 'live_feed', 'count_live')",
        (company_id,),
    )
    connection.execute(
        "INSERT INTO canonical_opportunities "
        "(id,company_id,canonical_key,canonical_title,normalized_title,"
        "source_category,first_seen_at,last_seen_at,is_active,variant_count) "
        "VALUES (?, ?, 'synthetic::portable-1', 'Synthetic Portability Subject', "
        "'synthetic portability subject', 'Generalist', ?, ?, 1, 0)",
        (canonical_id, company_id, NOW.isoformat(), NOW.isoformat()),
    )
    connection.commit()


class PublicJobIdentityOperationalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-public-registry-transfer-",
            ignore_cleanup_errors=True,
        )
        self.source = build_m008(Path(self.directory.name) / "source.sqlite3")
        self.target = build_m008(Path(self.directory.name) / "target.sqlite3")
        apply_public_job_identity_migration(self.source)
        apply_public_job_identity_migration(self.target)
        _seed_canonical(self.source, 101)
        _seed_canonical(self.target, 1001)
        self.allocation = identity.allocate_public_job(
            self.source,
            allocator=identity.PublicJobIdAllocator(
                "disposable-test-authority",
                random_source=lambda size: bytes.fromhex("11" * size),
            ),
            company_slug="synthetic-portability",
            canonical_title="Synthetic Portability Subject",
            canonical_opportunity_id=101,
            primary_path="/job/disposable-portability-subject",
            now=NOW,
        )

    def tearDown(self):
        self.target.close()
        self.source.close()
        self.directory.cleanup()

    def test_canonical_artifact_is_stable_hashed_and_excludes_bindings(self):
        artifact = identity.export_public_job_registry_artifact(self.source)
        payload = identity.decode_public_job_registry_artifact(artifact)

        self.assertEqual(
            artifact.sha256,
            hashlib.sha256(artifact.canonical_json).hexdigest(),
        )
        self.assertTrue(artifact.canonical_json.endswith(b"\n"))
        self.assertEqual(payload, identity.export_public_job_registry(self.source))
        self.assertNotIn(b"canonical_opportunity_id", artifact.canonical_json)
        self.assertNotIn(b'"bindings"', artifact.canonical_json)
        self.assertEqual(
            identity.public_job_registry_artifact(payload),
            artifact,
        )

    def test_noncanonical_or_digest_mismatched_artifacts_fail_closed(self):
        artifact = identity.export_public_job_registry_artifact(self.source)
        noncanonical = artifact.canonical_json[:-1] + b" \n"
        forged = identity.PublicJobRegistryArtifact(
            noncanonical,
            hashlib.sha256(noncanonical).hexdigest(),
        )
        with self.assertRaises(identity.InvalidPublicJobIdentity):
            identity.decode_public_job_registry_artifact(forged)
        with self.assertRaises(identity.InvalidPublicJobIdentity):
            identity.PublicJobRegistryArtifact(
                artifact.canonical_json,
                "0" * 64,
            )

    def test_post_construction_altered_artifact_fails_closed_before_import(self):
        artifact = identity.export_public_job_registry_artifact(self.source)
        original_sha256 = artifact.sha256
        replacement_id = "j" + "22" * 16
        altered_json = artifact.canonical_json.replace(
            self.allocation.public_job_id.encode("ascii"),
            replacement_id.encode("ascii"),
        )
        self.assertNotEqual(altered_json, artifact.canonical_json)
        self.assertEqual(
            identity.decode_public_job_registry_artifact(
                identity.PublicJobRegistryArtifact(
                    altered_json,
                    hashlib.sha256(altered_json).hexdigest(),
                )
            )["identities"][0]["public_job_id"],
            replacement_id,
        )

        object.__setattr__(artifact, "canonical_json", altered_json)
        self.assertEqual(artifact.sha256, original_sha256)
        with mock.patch.object(identity.json, "loads") as json_loads:
            with self.assertRaisesRegex(
                identity.InvalidPublicJobIdentity,
                "digest is invalid",
            ):
                identity.import_public_job_registry_artifact(self.target, artifact)
            json_loads.assert_not_called()

        self.assertEqual(
            tuple(
                self.target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "public_job_identities",
                    "public_job_paths",
                    "public_job_bindings",
                )
            ),
            (0, 0, 0),
        )

    def test_disposable_transfer_imports_then_rebinds_to_different_local_row(self):
        report = identity.verify_disposable_public_job_registry_transfer(
            self.source,
            self.target,
            local_bindings={self.allocation.public_job_id: 1001},
            now=LATER,
        )

        self.assertEqual(report.identity_count, 1)
        self.assertEqual(report.path_count, 1)
        self.assertEqual(report.binding_count, 1)
        self.assertEqual(
            identity.resolve_public_job_path(
                self.target,
                self.allocation.primary_path,
            ).canonical_opportunity_id,
            1001,
        )
        self.assertEqual(
            identity.export_public_job_registry_artifact(self.target),
            identity.export_public_job_registry_artifact(self.source),
        )
        identity.assert_public_job_identity_consistent(self.target)

    def test_failed_disposable_rebinding_rolls_target_back_to_empty(self):
        with self.assertRaises(identity.InvalidPublicJobIdentity):
            identity.verify_disposable_public_job_registry_transfer(
                self.source,
                self.target,
                local_bindings={self.allocation.public_job_id: 999_999},
                now=LATER,
            )
        self.assertEqual(
            tuple(
                self.target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "public_job_identities",
                    "public_job_paths",
                    "public_job_bindings",
                )
            ),
            (0, 0, 0),
        )

    def test_disposable_lifecycle_preserves_imported_identity_and_path(self):
        identity.verify_disposable_public_job_registry_transfer(
            self.source,
            self.target,
            local_bindings={self.allocation.public_job_id: 1001},
            now=LATER,
        )
        identity.mark_public_job_gone(
            self.target,
            self.allocation.public_job_id,
            now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
        )
        gone = identity.resolve_public_job_path(
            self.target,
            self.allocation.primary_path,
        )
        self.assertEqual((gone.kind, gone.public_job_id, gone.primary_path), (
            "gone",
            self.allocation.public_job_id,
            self.allocation.primary_path,
        ))
        identity.restore_public_job(
            self.target,
            self.allocation.public_job_id,
            now=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        )
        restored = identity.resolve_public_job_path(
            self.target,
            self.allocation.primary_path,
        )
        self.assertEqual((restored.kind, restored.public_job_id, restored.primary_path), (
            "serve",
            self.allocation.public_job_id,
            self.allocation.primary_path,
        ))


if __name__ == "__main__":
    unittest.main()
