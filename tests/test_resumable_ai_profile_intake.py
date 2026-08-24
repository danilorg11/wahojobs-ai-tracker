from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tests.ai_profile_import_test_support import (
    NOW,
    confirmed_review,
    import_source_metadata,
    install_ai_profile_import_database,
    intake_grant,
)
from tests.persistent_profiles_repository_test_support import create_command
from tests.browser_session_authentication_test_support import seed_browser_session
from tests.persistent_profile_read_authorization_test_support import transition_binding
from wahojobs import accounts
from wahojobs.ai_profile_import import (
    AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX,
    AI_PROFILE_IMPORT_RESERVATION_LEASE,
    AI_PROFILE_INTAKE_CHECKPOINT_RETENTION,
    AIProfileImportError,
    AIProfileImportService,
    prepare_confirmed_ai_profile_import,
)
from wahojobs.persistent_profiles_repository import PersistentProfileRepository
from wahojobs.profile_intake.contracts import ProfileIntakeError
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    PROFILE_INTAKE_REVIEW_STEPS,
    hydrate_profile_intake_checkpoint,
    profile_intake_checkpoint_review_step,
    serialize_profile_intake_checkpoint,
)


class ResumableAIProfileIntakeCoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-resumable-intake-", ignore_cleanup_errors=True
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection, self.session = install_ai_profile_import_database(
            self.path, suffix="141"
        )
        self.grant = intake_grant(self.path, self.session)
        self.service = AIProfileImportService()
        self.review = confirmed_review()
        self.metadata = import_source_metadata()

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def create(self, *, now=NOW):
        return self.service.create_checkpoint(
            self.connection,
            self.grant,
            self.review,
            self.metadata,
            now=now,
        )

    def assert_code(self, code, callback, *args, **kwargs):
        with self.assertRaises(AIProfileImportError) as raised:
            callback(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_checkpoint_round_trip_is_minimized_and_contains_no_source_evidence(self):
        created = self.create()
        row = self.connection.execute(
            "SELECT review_payload_json,review_payload_sha256,source_metadata_json,"
            "row_version,reservation_generation,expires_at FROM ai_profile_intake_checkpoints"
        ).fetchone()
        self.assertEqual(
            hashlib.sha256(row[0].encode("ascii")).hexdigest(), row[1]
        )
        hydrated = hydrate_profile_intake_checkpoint(row[0])
        self.assertEqual(
            serialize_profile_intake_checkpoint(hydrated), row[0]
        )
        self.assertEqual((row[3], row[4]), (1, 1))
        self.assertEqual(
            json.loads(row[0])["review_step"],
            PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
        )
        self.assertEqual(row[5], (NOW + timedelta(days=7)).isoformat(timespec="seconds"))
        retained = row[0].casefold()
        for forbidden in (
            "document_reference",
            "evidence_block",
            "filename",
            "resume_text",
            "prompt",
            "provider_request",
            "input_tokens",
            "raw_response",
        ):
            self.assertNotIn(forbidden, retained)
        self.assertEqual(created.checkpoint.row_version, 1)

    def test_total_years_integer_and_existing_float_checkpoints_round_trip_exactly(self):
        template = replace(
            self.review.facts[0],
            field_path="experience.total_years",
            review_field="total_years",
            suggested=False,
            decision="keep",
            conflict_group=None,
        )
        for value in (6, 6.0):
            with self.subTest(value_type=type(value).__name__):
                review = replace(
                    self.review,
                    facts=(*self.review.facts, replace(template, value=value)),
                )
                payload = serialize_profile_intake_checkpoint(review)
                raw_value = next(
                    fact["value"]
                    for fact in json.loads(payload)["facts"]
                    if fact["field_path"] == "experience.total_years"
                )
                hydrated_value = next(
                    fact.value
                    for fact in hydrate_profile_intake_checkpoint(payload).facts
                    if fact.field_path == "experience.total_years"
                )
                self.assertIs(type(raw_value), type(value))
                self.assertIs(type(hydrated_value), type(value))
                self.assertEqual(hydrated_value, value)

    def test_review_step_hint_is_closed_backward_compatible_and_non_semantic(self):
        baseline = serialize_profile_intake_checkpoint(self.review)
        baseline_payload = json.loads(baseline)
        for step in PROFILE_INTAKE_REVIEW_STEPS:
            payload = serialize_profile_intake_checkpoint(
                self.review,
                review_step=step,
            )
            self.assertEqual(profile_intake_checkpoint_review_step(payload), step)
            self.assertEqual(hydrate_profile_intake_checkpoint(payload), self.review)
            semantic_payload = json.loads(payload)
            semantic_payload.pop("review_step")
            expected = dict(baseline_payload)
            expected.pop("review_step")
            self.assertEqual(semantic_payload, expected)

        legacy_payload = dict(baseline_payload)
        legacy_payload.pop("review_step")
        legacy_payload.pop("user_facts")
        legacy = json.dumps(
            legacy_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            profile_intake_checkpoint_review_step(legacy),
            PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
        )
        self.assertEqual(hydrate_profile_intake_checkpoint(legacy), self.review)
        invalid_payload = dict(baseline_payload)
        invalid_payload["review_step"] = "https://example.invalid/arbitrary-route"
        invalid = json.dumps(
            invalid_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            profile_intake_checkpoint_review_step(invalid),
            PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
        )
        self.assertEqual(hydrate_profile_intake_checkpoint(invalid), self.review)

    def test_every_valid_review_step_round_trips_through_resume(self):
        created = self.create()
        authority = created.checkpoint
        current_step = PROFILE_INTAKE_DEFAULT_REVIEW_STEP
        for index, step in enumerate(PROFILE_INTAKE_REVIEW_STEPS):
            if step != current_step:
                authority = self.service.update_checkpoint(
                    self.connection,
                    self.grant,
                    authority,
                    self.review,
                    review_step=step,
                    now=NOW + timedelta(seconds=index),
                )
                current_step = step
            resumed = self.service.resume_checkpoint(
                self.connection,
                self.grant,
                authority.checkpoint_id,
                expected_version=authority.row_version,
                now=NOW + timedelta(seconds=index),
            )
            self.assertEqual(resumed.review_step, step)
            self.assertEqual(resumed.review, self.review)
            authority = resumed.checkpoint

    def test_contact_pii_is_rejected_instead_of_transformed_or_stored(self):
        changed = replace(
            self.review.facts[0], value="candidate@example.invalid"
        )
        review = replace(
            self.review, facts=(changed, *self.review.facts[1:])
        )
        with self.assertRaises(ProfileIntakeError) as raised:
            serialize_profile_intake_checkpoint(review)
        self.assertEqual(raised.exception.code, "checkpoint_contact_pii_rejected")
        self.assert_code(
            "content_rejected",
            self.service.create_checkpoint,
            self.connection,
            self.grant,
            review,
            self.metadata,
            now=NOW,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
            ).fetchone()[0],
            0,
        )

    def test_oversize_version_and_digest_tampering_fail_closed(self):
        with self.assertRaises(ProfileIntakeError) as raised:
            hydrate_profile_intake_checkpoint("{" + "x" * 70_000 + "}")
        self.assertEqual(raised.exception.code, "checkpoint_too_large")
        created = self.create()
        payload = json.loads(
            self.connection.execute(
                "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
            ).fetchone()[0]
        )
        payload["schema_version"] = "profile_intake_checkpoint_v999"
        tampered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            "UPDATE ai_profile_intake_checkpoints SET row_version=2,review_payload_json=?,"
            "review_payload_sha256=?,review_saved_at=?,updated_at=?,expires_at=? WHERE checkpoint_id=?",
            (
                tampered,
                hashlib.sha256(tampered.encode("ascii")).hexdigest(),
                (NOW + timedelta(seconds=1)).isoformat(timespec="seconds"),
                (NOW + timedelta(seconds=1)).isoformat(timespec="seconds"),
                (NOW + timedelta(days=7, seconds=1)).isoformat(timespec="seconds"),
                created.checkpoint.checkpoint_id,
            ),
        )
        self.connection.commit()
        self.assert_code(
            "checkpoint_tampered",
            self.service.resume_checkpoint,
            self.connection,
            self.grant,
            created.checkpoint.checkpoint_id,
            expected_version=2,
            now=NOW + timedelta(seconds=2),
        )

    def test_validated_change_refreshes_seven_days_and_stale_update_loses(self):
        created = self.create()
        unchanged = self.service.update_checkpoint(
            self.connection,
            self.grant,
            created.checkpoint,
            self.review,
            now=NOW + timedelta(minutes=30),
        )
        self.assertEqual(unchanged.row_version, 1)
        self.assertEqual(unchanged.expires_at, created.checkpoint.expires_at)
        self.assertEqual(
            tuple(
                self.connection.execute(
                    "SELECT row_version,review_saved_at,expires_at "
                    "FROM ai_profile_intake_checkpoints"
                ).fetchone()
            ),
            (1, NOW.isoformat(timespec="seconds"), created.checkpoint.expires_at),
        )
        changed = replace(self.review.facts[0], value="Updated Candidate")
        updated_review = replace(
            self.review, facts=(changed, *self.review.facts[1:])
        )
        later = NOW + timedelta(hours=1)
        authority = self.service.update_checkpoint(
            self.connection,
            self.grant,
            created.checkpoint,
            updated_review,
            now=later,
        )
        self.assertEqual(authority.row_version, 2)
        self.assertEqual(
            authority.expires_at,
            (later + AI_PROFILE_INTAKE_CHECKPOINT_RETENTION).isoformat(timespec="seconds"),
        )
        self.assert_code(
            "checkpoint_stale",
            self.service.update_checkpoint,
            self.connection,
            self.grant,
            created.checkpoint,
            self.review,
            now=later + timedelta(seconds=1),
        )

    def test_seven_day_expiry_deletes_checkpoint_and_releases_orphan(self):
        created = self.create()
        now = NOW + timedelta(days=7, seconds=1)
        session = accounts.create_session(
            self.connection,
            user_id=self.session["account_id"],
            idle_ttl=timedelta(hours=2),
            absolute_ttl=timedelta(days=1),
            idempotency_key="resumable-intake-expiry-session-0001",
            now=now - timedelta(minutes=5),
        )
        self.connection.commit()
        later_session = dict(self.session)
        later_session.update(
            session_id=session.session.session_id,
            session_token=session.session_token,
            csrf_secret=session.csrf_secret,
        )
        grant = intake_grant(self.path, later_session, now=now)
        self.assert_code(
            "checkpoint_expired",
            self.service.resume_checkpoint,
            self.connection,
            grant,
            created.checkpoint.checkpoint_id,
            expected_version=1,
            now=now,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM ai_profile_import_entitlements"
            ).fetchone()[0],
            "available",
        )

    def test_process_restart_and_cross_session_resume_use_no_extraction(self):
        created = self.create()
        stepped = self.service.update_checkpoint(
            self.connection,
            self.grant,
            created.checkpoint,
            self.review,
            review_step="review-preferences",
            now=NOW + timedelta(seconds=1),
        )
        session = accounts.create_session(
            self.connection,
            user_id=self.session["account_id"],
            idle_ttl=timedelta(hours=3),
            absolute_ttl=timedelta(days=1),
            idempotency_key="resumable-intake-cross-session-0001",
            now=NOW - timedelta(minutes=5),
        )
        self.connection.commit()
        second = dict(self.session)
        second.update(
            session_id=session.session.session_id,
            session_token=session.session_token,
            csrf_secret=session.csrf_secret,
        )
        self.connection.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        grant = intake_grant(self.path, second, now=NOW + timedelta(minutes=1))
        read_connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro", uri=True
        )
        read_connection.row_factory = sqlite3.Row
        read_connection.execute("PRAGMA foreign_keys = ON")
        read_connection.execute("PRAGMA query_only = ON")
        summary = AIProfileImportService().inspect_checkpoint(
            read_connection, grant, now=NOW + timedelta(minutes=1)
        )
        read_connection.close()
        self.assertIsNotNone(summary)
        with mock.patch(
            "wahojobs.profile_intake.runtime.extract_profile_document",
            side_effect=AssertionError("extraction must not run"),
        ):
            resumed = AIProfileImportService().resume_checkpoint(
                self.connection,
                grant,
                summary.checkpoint.checkpoint_id,
                expected_version=summary.checkpoint.row_version,
                now=NOW + timedelta(minutes=1),
            )
        self.assertEqual(resumed.checkpoint.row_version, stepped.row_version)
        self.assertEqual(resumed.checkpoint.reservation_generation, 1)
        self.assertTrue(resumed.replayed_reservation)
        self.assertEqual(resumed.review_step, "review-preferences")
        self.assertEqual(
            serialize_profile_intake_checkpoint(resumed.review),
            serialize_profile_intake_checkpoint(self.review),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM ai_profile_import_attempts"
            ).fetchone()[0],
            1,
        )

    def test_expired_short_lease_reacquires_fresh_generation(self):
        created = self.create()
        later = NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1)
        grant = intake_grant(self.path, self.session, now=later)
        resumed = self.service.resume_checkpoint(
            self.connection,
            grant,
            created.checkpoint.checkpoint_id,
            expected_version=1,
            now=later,
        )
        self.assertEqual(resumed.checkpoint.row_version, 2)
        self.assertEqual(resumed.checkpoint.reservation_generation, 2)
        self.assertEqual(
            tuple(
                row[0]
                for row in self.connection.execute(
                    "SELECT state FROM ai_profile_import_attempts ORDER BY created_at,attempt_id"
                )
            ),
            ("expired", "reserved"),
        )

    def test_renewal_is_rate_limited_and_generation_is_bounded(self):
        created = self.create()
        at_four = NOW + timedelta(minutes=4)
        same = self.service.renew_checkpoint_reservation(
            self.connection,
            intake_grant(self.path, self.session, now=at_four),
            created.checkpoint,
            created.reservation,
            now=at_four,
        )
        self.assertTrue(same.replayed)
        self.assertEqual(same.authority.lease_expires_at, created.reservation.lease_expires_at)
        at_six = NOW + timedelta(minutes=6)
        renewed = self.service.renew_checkpoint_reservation(
            self.connection,
            intake_grant(self.path, self.session, now=at_six),
            created.checkpoint,
            created.reservation,
            now=at_six,
        )
        self.assertGreater(renewed.authority.lease_expires_at, created.reservation.lease_expires_at)
        self.assertLessEqual(
            renewed.authority.lease_expires_at,
            (NOW + AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX).isoformat(timespec="seconds"),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT lease_expires_at FROM ai_profile_import_entitlements"
            ).fetchone()[0],
            renewed.authority.lease_expires_at,
        )
        current = renewed.authority
        for minutes in range(12, 115, 6):
            at = NOW + timedelta(minutes=minutes)
            current = self.service.renew_checkpoint_reservation(
                self.connection,
                intake_grant(self.path, self.session, now=at),
                created.checkpoint,
                current,
                now=at,
            ).authority
        generation_end = (
            NOW + AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX
        ).isoformat(timespec="seconds")
        self.assertEqual(current.lease_expires_at, generation_end)
        after_cap = NOW + AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX + timedelta(seconds=1)
        self.assert_code(
            "reservation_expired",
            self.service.renew_checkpoint_reservation,
            self.connection,
            intake_grant(self.path, self.session, now=after_cap),
            created.checkpoint,
            current,
            now=after_cap,
        )
        resumed = self.service.resume_checkpoint(
            self.connection,
            intake_grant(self.path, self.session, now=after_cap),
            created.checkpoint.checkpoint_id,
            expected_version=1,
            now=after_cap,
        )
        self.assertEqual(resumed.checkpoint.reservation_generation, 2)

    def test_manual_create_wins_and_checkpoint_cannot_bypass_create_once(self):
        created = self.create()
        PersistentProfileRepository().create_account_native(
            self.connection,
            create_command(
                self.grant.principal_for_repository(),
                idempotency_key="manual-wins-resumable-checkpoint-0001",
                accepted_at=NOW,
            ),
            account_lineage=self.grant.lineage_for_repository(),
        )
        self.assert_code(
            "profile_already_exists",
            self.service.resume_checkpoint,
            self.connection,
            self.grant,
            created.checkpoint.checkpoint_id,
            expected_version=1,
            now=NOW + timedelta(seconds=1),
        )

    def test_wrong_account_and_changed_pb_own_lineage_cannot_resume(self):
        created = self.create()
        other = seed_browser_session(
            self.connection, suffix="142", idle_ttl=timedelta(hours=3)
        )
        other_grant = intake_grant(self.path, other, now=NOW + timedelta(seconds=1))
        self.assert_code(
            "ownership_stale",
            self.service.resume_checkpoint,
            self.connection,
            other_grant,
            created.checkpoint.checkpoint_id,
            expected_version=1,
            now=NOW + timedelta(seconds=1),
        )
        transition_binding(self.connection, self.session, "suspended")
        self.assert_code(
            "ownership_stale",
            self.service.resume_checkpoint,
            self.connection,
            self.grant,
            created.checkpoint.checkpoint_id,
            expected_version=1,
            now=NOW + timedelta(seconds=1),
        )

    def test_discard_deletes_checkpoint_releases_reservation_and_preserves_entitlement(self):
        created = self.create()
        self.service.discard_checkpoint(
            self.connection,
            self.grant,
            created.checkpoint,
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            tuple(
                self.connection.execute(
                    "SELECT state,consumed_at FROM ai_profile_import_entitlements"
                ).fetchone()
            ),
            ("available", None),
        )

    def test_final_save_deletes_checkpoint_atomically_and_exact_replay_is_safe(self):
        created = self.create()
        confirmed = prepare_confirmed_ai_profile_import(self.review, self.metadata)
        result = self.service.commit_confirmed_ai_profile_import(
            self.connection,
            self.grant,
            created.reservation,
            confirmed,
            checkpoint=created.checkpoint,
            now=NOW,
        )
        self.assertFalse(result.replayed)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
            ).fetchone()[0],
            0,
        )
        replay = self.service.commit_confirmed_ai_profile_import(
            self.connection,
            self.grant,
            created.reservation,
            confirmed,
            checkpoint=created.checkpoint,
            now=NOW + timedelta(seconds=1),
        )
        self.assertTrue(replay.replayed)
        self.assertEqual((replay.profile_id, replay.revision_id), (result.profile_id, result.revision_id))

    def test_new_generation_winner_prevents_stale_generation_from_creating_again(self):
        original = self.create()
        later = NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1)
        grant = intake_grant(self.path, self.session, now=later)
        winner = self.service.resume_checkpoint(
            self.connection,
            grant,
            original.checkpoint.checkpoint_id,
            expected_version=1,
            now=later,
        )
        confirmed = prepare_confirmed_ai_profile_import(winner.review, winner.source_metadata)
        self.service.commit_confirmed_ai_profile_import(
            self.connection,
            grant,
            winner.reservation,
            confirmed,
            checkpoint=winner.checkpoint,
            now=later,
        )
        self.assert_code(
            "reservation_mismatch",
            self.service.commit_confirmed_ai_profile_import,
            self.connection,
            grant,
            original.reservation,
            confirmed,
            checkpoint=original.checkpoint,
            now=later + timedelta(seconds=1),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
