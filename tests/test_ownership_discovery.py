import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.ownership_test_support import database_snapshot, install_ownership
from wahojobs.ownership import discover_legacy_owners
from wahojobs.ownership_reconciliation import reconcile_ownership


class OwnershipDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "discovery.sqlite"
        self.conn = install_ownership(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_discovery_is_read_only_deterministic_and_registers_nothing(self):
        for index in range(10):
            self._profile(f"sample_{index}", f"mock_user_{index}", is_sample=1)
        self._profile("local_user", "local_user", is_sample=0)
        self._pipeline_item("pipe-one", "sample_1", "mock_user_1")
        self._transition("transition-one", "pipe-one", "sample_1")
        self._applicant(
            "update-one",
            "sample_1",
            user_id="mock_applicant",
            anonymous_user_key="mock_applicant",
            is_sample=1,
        )
        before = database_snapshot(self.conn)
        first = discover_legacy_owners(self.conn)
        second = discover_legacy_owners(self.conn)
        after = database_snapshot(self.conn)
        self.assertEqual(first.public_dict(), second.public_dict())
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM product_principals").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM legacy_owner_aliases").fetchone()[0], 0)
        local = [item for item in first.observations if item.classification == "development"]
        self.assertTrue(local)
        self.assertTrue(all(item.recommended_claimability == "nonclaimable" for item in local))
        public = json.dumps(first.public_dict(), sort_keys=True)
        self.assertNotIn("local_user", public)
        self.assertNotIn("mock_applicant", public)
        self.assertNotIn("_alias_value", public)
        self.assertNotIn("value_fingerprint", public)
        self.assertEqual(first.distinct_raw_value_count, 22)
        self.assertEqual(first.distinct_kind_value_pair_count, 25)
        self.assertEqual(first.observation_count, 28)
        self.assertEqual(
            sum(first.kind_value_pair_classification_counts.values()),
            first.distinct_kind_value_pair_count,
        )
        self.assertEqual(
            sum(first.observation_classification_counts.values()),
            first.observation_count,
        )
        for candidate in ("local_user", "sample_1", "mock_applicant"):
            self.assertNotIn(hashlib.sha256(candidate.encode()).hexdigest(), public)
        references = [item.report_reference for item in first.observations]
        self.assertTrue(all(reference.startswith("legacy-owner-") for reference in references))

    def test_unregistered_owners_are_informational_and_sample_divergence_is_safe(self):
        self._profile("sample_profile", "sample_owner", is_sample=1)
        self._applicant(
            "sample-update",
            "sample_profile",
            user_id="mock_applicant",
            anonymous_user_key="mock_applicant",
            is_sample=1,
        )
        report = reconcile_ownership(self.conn)
        self.assertFalse(report["blocking"], report["blocking_reasons"])
        self.assertTrue(report["informational"]["unregistered_legacy_owners"])
        self.assertTrue(report["informational"]["sample_applicant_owner_variants"])

    def test_real_pipeline_and_applicant_inconsistencies_are_blocking(self):
        self._profile("real_profile", "real_owner", is_sample=0)
        self._pipeline_item("wrong-item", "real_profile", "different_owner")
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self._transition("wrong-transition", "wrong-item", "other_profile")
        self._applicant(
            "wrong-update",
            "real_profile",
            user_id="different_owner",
            anonymous_user_key="different_owner",
            is_sample=0,
        )
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=ON")
        report = reconcile_ownership(self.conn)
        self.assertIn("pipeline_item_profile_inconsistencies", report["blocking_reasons"])
        self.assertIn("transition_owner_mismatches", report["blocking_reasons"])
        self.assertIn("applicant_owner_inconsistencies", report["blocking_reasons"])
        serialized = json.dumps(report, sort_keys=True)
        for raw in ("real_owner", "different_owner", "other_profile"):
            self.assertNotIn(raw, serialized)

    def test_malformed_owner_is_blocking_without_leaking_content(self):
        self._profile("bad\nprofile", "sample_owner", is_sample=1)
        report = reconcile_ownership(self.conn)
        self.assertIn("malformed_legacy_owners", report["blocking_reasons"])
        self.assertNotIn("bad\nprofile", json.dumps(report, sort_keys=True))

    def _profile(self, profile_id, user_id, *, is_sample):
        self.conn.execute(
            "INSERT INTO user_profiles "
            "(user_id, profile_id, display_name, is_sample) VALUES (?, ?, 'Fixture', ?)",
            (user_id, profile_id, is_sample),
        )

    def _pipeline_item(self, pipeline_item_id, profile_id, user_id):
        self.conn.execute(
            "INSERT INTO user_pipeline_items "
            "(pipeline_item_id, user_id, profile_id, source, opportunity_title, status, is_sample) "
            "VALUES (?, ?, ?, 'fixture', 'Private fixture title', 'saved', 1)",
            (pipeline_item_id, user_id, profile_id),
        )

    def _transition(self, transition_id, pipeline_item_id, profile_id):
        self.conn.execute(
            "INSERT INTO user_pipeline_transitions "
            "(transition_id, pipeline_item_id, profile_id, affected_dimension, action_name, "
            "before_state_json, after_state_json, occurred_at, actor_source, idempotency_key, "
            "request_fingerprint, state_version_before, state_version_after, metadata_json) "
            "VALUES (?, ?, ?, 'workflow', 'fixture', '{}', '{}', "
            "'2026-07-17T12:00:00+00:00', 'fixture', ?, ?, 0, 1, '{}')",
            (transition_id, pipeline_item_id, profile_id, f"idem-{transition_id}", "f" * 64),
        )

    def _applicant(
        self,
        update_id,
        profile_id,
        *,
        user_id,
        anonymous_user_key,
        is_sample,
    ):
        self.conn.execute(
            "INSERT INTO applicant_status_updates "
            "(update_id, user_id, anonymous_user_key, profile_id, source, opportunity_title, "
            "status, status_date, reported_at, evidence_type, confidence_level, is_sample) "
            "VALUES (?, ?, ?, ?, 'fixture', 'Private fixture title', 'saved', '2026-07-17', "
            "'2026-07-17T12:00:00+00:00', 'user_reported', 'high', ?)",
            (update_id, user_id, anonymous_user_key, profile_id, is_sample),
        )


if __name__ == "__main__":
    unittest.main()
