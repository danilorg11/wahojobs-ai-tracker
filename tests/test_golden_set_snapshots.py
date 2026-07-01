import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as benchmark
import profile_match_digest as matcher


def profile(**overrides):
    payload = {
        "profile_id": "profile",
        "display_name": "Profile",
        "education_level": "not_specified",
        "degrees_or_domains": ["language"],
        "languages": ["English", "Portuguese"],
        "skills": ["review"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["AI evaluation"],
        "notes": "",
        "signals": [
            ("Portuguese signal", ["portuguese"], 12),
            ("Review signal", ["review", "rater"], 8),
            ("Generalist signal", ["generalist"], 8),
        ],
        "avoid_keywords": [],
        "location": "",
        "country": "",
        "residence": "",
        "city": "",
        "region": "",
    }
    payload.update(overrides)
    return payload


def case(**overrides):
    payload = {
        "case_id": "case_001",
        "profile_id": "profile",
        "source": "Example Source",
        "title": "Fixture Title",
        "location": "Remote",
        "department": "General",
        "expertise": "General",
        "commitment": "",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "expected_label": "weak",
        "label_source": "codex_draft",
        "rationale": "Test fixture.",
        "review_required": False,
        "expected_section": "explore_only",
    }
    payload.update(overrides)
    return payload


def human_case(**overrides):
    payload = case(
        label_source="human_reviewed",
        review_required=False,
        human_notes="Reviewed by human.",
    )
    payload.update(overrides)
    return payload


def live_row(**overrides):
    payload = {
        "job_id": 1,
        "external_id": "job-1",
        "source_hash": "hash-1",
        "title": "Changed Live Title",
        "canonical_title": None,
        "source": "Example Source",
        "source_slug": "example-source",
        "source_tier": "core",
        "location": "Changed Location",
        "url": "https://example.test/jobs/1",
        "department": "Changed Department",
        "expertise": "Changed Expertise",
        "source_category": "Changed Category",
        "commitment": "",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": None,
    }
    payload.update(overrides)
    return payload


def snapshot(**overrides):
    matcher_input = {
        "job_id": "snapshot-job",
        "external_id": "snapshot-external",
        "source_hash": "snapshot-hash",
        "title": "Portuguese Review Rater",
        "canonical_title": None,
        "source": "Example Source",
        "source_slug": "example-source",
        "source_tier": "core",
        "location": "Remote",
        "url": "https://example.test/snapshot",
        "department": "Language & Linguistics",
        "expertise": "Language & Linguistics",
        "source_category": "Language & Linguistics",
        "commitment": "",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": None,
        "language": "portuguese",
        "language_locale": "pt",
        "required_languages": ["portuguese"],
    }
    matcher_input.update(overrides)
    return {
        "snapshot_schema_version": benchmark.MATCHER_INPUT_SNAPSHOT_SCHEMA_VERSION,
        "matcher_input": matcher_input,
        "snapshot_metadata": {
            "snapshot_created_from": "unit_test",
            "created_by": "tests.test_golden_set_snapshots",
            "source_row_identifiers": {},
        },
    }


class GoldenSetSnapshotTests(unittest.TestCase):
    def test_embedded_snapshot_wins_over_changed_live_row(self):
        fixture = case(
            title="Old Fixture Title",
            url="https://example.test/jobs/1",
            matcher_input_snapshot=snapshot(title="Portuguese Review Rater", location="Remote"),
        )
        item = benchmark.evaluate_case(
            fixture,
            profile(),
            [live_row(title="Python Coding Role", location="United States")],
            matcher,
        )

        self.assertEqual(item.resolution.resolution_status, "fixture_self_contained_snapshot")
        self.assertEqual(item.row["title"], "Portuguese Review Rater")
        self.assertEqual(item.row["location"], "Remote")
        self.assertNotEqual(item.row["title"], "Python Coding Role")

    def test_snapshot_result_is_stable_when_live_category_changes(self):
        fixture = case(
            url="https://example.test/jobs/1",
            matcher_input_snapshot=snapshot(source_category="Language & Linguistics"),
        )
        before = benchmark.evaluate_case(fixture, profile(), [live_row(source_category="Coding")], matcher)
        after = benchmark.evaluate_case(
            fixture,
            profile(),
            [live_row(source_category="Healthcare", title="Medical Expert")],
            matcher,
        )

        self.assertEqual(before.score, after.score)
        self.assertEqual(before.evaluation_label, after.evaluation_label)
        self.assertEqual(before.evaluation_section, after.evaluation_section)

    def test_exact_live_url_without_snapshot_still_uses_safe_resolver(self):
        fixture = case(url="https://example.test/jobs/1")
        item = benchmark.evaluate_case(fixture, profile(), [live_row()], matcher)

        self.assertEqual(item.resolution.resolution_status, "live_db_url")
        self.assertTrue(item.resolution.used_live_db)

    def test_missing_identifier_without_snapshot_uses_fixture_snapshot(self):
        fixture = case(title="Fixture Only")
        item = benchmark.evaluate_case(fixture, profile(), [live_row(title="Fixture Only")], matcher)

        self.assertEqual(item.resolution.resolution_status, "fixture_snapshot_missing_identifier")
        self.assertTrue(item.resolution.used_fixture_snapshot)
        self.assertEqual(item.row["title"], "Fixture Only")

    def test_title_only_candidate_is_diagnostic_and_not_snapshot_fill(self):
        fixture = case(
            title="Same Title",
            matcher_input_snapshot=snapshot(title="Snapshot Title"),
        )
        item = benchmark.evaluate_case(fixture, profile(), [live_row(title="Same Title")], matcher)

        self.assertEqual(item.row["title"], "Snapshot Title")
        self.assertEqual(item.resolution.candidate_count, 1)
        self.assertEqual(item.resolution.diagnostic_candidates[0]["title"], "Same Title")

    def test_ambiguous_canonical_id_does_not_create_live_snapshot_candidate(self):
        fixture = case(canonical_opportunity_id=42)
        rows = [
            live_row(job_id=1, canonical_opportunity_id=42),
            live_row(job_id=2, canonical_opportunity_id=42),
        ]
        item = benchmark.evaluate_case(fixture, profile(), rows, matcher)
        proposal = benchmark.propose_matcher_input_snapshot(item.case, item.resolution)

        self.assertEqual(item.resolution.resolution_status, "ambiguous_fixture_fallback")
        self.assertEqual(proposal["status"], "ambiguous_candidate_fixture_only")
        self.assertFalse(proposal["exact_live_enrichment_allowed"])

    def test_incomplete_snapshot_is_detected_explicitly(self):
        incomplete = snapshot()
        del incomplete["matcher_input"]["title"]
        fixture = case(matcher_input_snapshot=incomplete)

        with self.assertRaises(benchmark.SnapshotValidationError):
            benchmark.evaluate_case(fixture, profile(), [], matcher)

    def test_snapshot_preserves_language_location_evergreen_and_projection_fields(self):
        fixture = case(
            matcher_input_snapshot=snapshot(
                title="Catalan Translation Quality Rater",
                opportunity_kind="evergreen_application",
                availability_basis="evergreen_page",
                inventory_model="evergreen_application",
                market_count_policy="report_separately",
                include_in_live_market_estimate=0,
                location="United States",
                department="Language & Linguistics",
                expertise="Language & Linguistics",
                source_category="Language & Linguistics",
                language="catalan",
                language_locale="ca",
                required_languages=["catalan"],
            )
        )
        item = benchmark.evaluate_case(fixture, profile(languages=["English"]), [], matcher)

        self.assertEqual(item.row["opportunity_kind"], "evergreen_application")
        self.assertEqual(item.evergreen_opportunity_kind, "broad_language")
        self.assertEqual(item.location_eligibility_status, "unknown")
        self.assertFalse(item.eligible_for_personalized)
        self.assertEqual(item.hard_gate_type, "language")
        self.assertEqual(item.evaluation_label, "false_positive")
        self.assertEqual(item.evaluation_section, "exclude")

    def test_current_human_reviewed_baseline_reflects_enriched_snapshots(self):
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        rows = benchmark.load_benchmark_db_rows()
        items = [
            benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, matcher)
            for case in fixture["cases"]
            if case.get("label_source") == "human_reviewed"
        ]

        label_agreement = sum(1 for item in items if item.case["expected_label"] == item.evaluation_label)
        section_agreement = sum(
            1 for item in items
            if benchmark.expected_section(item.case) == item.evaluation_section
        )
        full_agreement = sum(
            1 for item in items
            if item.case["expected_label"] == item.evaluation_label
            and benchmark.expected_section(item.case) == item.evaluation_section
        )

        self.assertEqual((label_agreement, section_agreement, full_agreement), (13, 19, 8))

    def test_migration_applies_snapshots_to_human_reviewed_cases_only(self):
        human = human_case(case_id="human_001", url="https://example.test/jobs/1")
        draft = case(case_id="draft_001", url="https://example.test/jobs/1")
        fixture = {"cases": [human, deepcopy(draft)]}
        rows = [live_row(title="Portuguese Review Rater")]
        profiles = {"profile": profile()}
        evaluated = [
            benchmark.evaluate_case(item, profiles[item["profile_id"]], rows, matcher)
            for item in fixture["cases"]
        ]

        migrated, summary = benchmark.migrate_matcher_input_snapshots(
            fixture,
            evaluated,
            profiles,
            only_human_reviewed=True,
        )

        self.assertEqual(summary["snapshotted_cases"], 1)
        self.assertIn("matcher_input_snapshot", migrated["cases"][0])
        self.assertNotIn("matcher_input_snapshot", migrated["cases"][1])
        self.assertEqual(migrated["cases"][1], draft)

    def test_exact_live_migration_preserves_resolved_matcher_input(self):
        fixture = {"cases": [human_case(case_id="human_001", url="https://example.test/jobs/1")]}
        row = live_row(
            title="Portuguese Review Rater",
            department="Resolved Department",
            expertise="Resolved Expertise",
            source_category="Resolved Category",
        )
        profiles = {"profile": profile()}
        evaluated = [benchmark.evaluate_case(fixture["cases"][0], profiles["profile"], [row], matcher)]

        migrated, summary = benchmark.migrate_matcher_input_snapshots(fixture, evaluated, profiles)
        matcher_input = migrated["cases"][0]["matcher_input_snapshot"]["matcher_input"]

        self.assertEqual(summary["counts"]["exact_live_snapshot_candidate"], 1)
        self.assertEqual(matcher_input["title"], "Portuguese Review Rater")
        self.assertEqual(matcher_input["department"], "Resolved Department")
        self.assertEqual(matcher_input["source_category"], "Resolved Category")
        self.assertTrue(migrated["cases"][0]["matcher_input_snapshot"]["snapshot_metadata"]["used_live_db"])

    def test_fixture_only_migration_preserves_fixture_input_without_enrichment(self):
        fixture = {
            "cases": [
                human_case(
                    case_id="human_001",
                    title="Fixture Only Role",
                    department="Fixture Department",
                    expertise="Fixture Expertise",
                    source_category="Fixture Category",
                )
            ]
        }
        profiles = {"profile": profile()}
        evaluated = [
            benchmark.evaluate_case(
                fixture["cases"][0],
                profiles["profile"],
                [live_row(title="Unrelated Role", department="Live Department")],
                matcher,
            )
        ]

        migrated, summary = benchmark.migrate_matcher_input_snapshots(fixture, evaluated, profiles)
        matcher_input = migrated["cases"][0]["matcher_input_snapshot"]["matcher_input"]

        self.assertEqual(summary["counts"]["fixture_only_snapshot_candidate"], 1)
        self.assertEqual(matcher_input["title"], "Fixture Only Role")
        self.assertEqual(matcher_input["department"], "Fixture Department")
        self.assertEqual(matcher_input["source_category"], "Fixture Category")
        self.assertFalse(migrated["cases"][0]["matcher_input_snapshot"]["snapshot_metadata"]["used_live_db"])

    def test_unsafe_title_only_migration_uses_fixture_input_not_candidate(self):
        fixture = {
            "cases": [
                human_case(
                    case_id="human_001",
                    title="Same Title",
                    department="Fixture Department",
                    expertise="Fixture Expertise",
                    source_category="Fixture Category",
                )
            ]
        }
        row = live_row(
            title="Same Title",
            department="Unsafe Live Department",
            expertise="Unsafe Live Expertise",
            source_category="Unsafe Live Category",
        )
        profiles = {"profile": profile()}
        evaluated = [benchmark.evaluate_case(fixture["cases"][0], profiles["profile"], [row], matcher)]

        migrated, summary = benchmark.migrate_matcher_input_snapshots(fixture, evaluated, profiles)
        matcher_input = migrated["cases"][0]["matcher_input_snapshot"]["matcher_input"]

        self.assertEqual(summary["counts"]["unsafe_title_candidate_fixture_only"], 1)
        self.assertEqual(matcher_input["department"], "Fixture Department")
        self.assertEqual(matcher_input["source_category"], "Fixture Category")
        self.assertNotEqual(matcher_input["department"], "Unsafe Live Department")

    def test_migration_refuses_prediction_changes(self):
        fixture = {"cases": [human_case(case_id="human_001")]}
        profiles = {"profile": profile()}
        evaluated = [benchmark.evaluate_case(fixture["cases"][0], profiles["profile"], [], matcher)]
        fake_diagnostics = {
            "prediction_changes": [{"case_id": "human_001"}],
            "proposals": [],
        }

        with patch.object(benchmark, "build_snapshot_dry_run", return_value=fake_diagnostics):
            with self.assertRaises(benchmark.SnapshotMigrationError):
                benchmark.migrate_matcher_input_snapshots(fixture, evaluated, profiles)

    def test_migrated_case_evaluates_from_snapshot_without_live_db(self):
        fixture = {"cases": [human_case(case_id="human_001", url="https://example.test/jobs/1")]}
        profiles = {"profile": profile()}
        evaluated = [
            benchmark.evaluate_case(
                fixture["cases"][0],
                profiles["profile"],
                [live_row(title="Portuguese Review Rater")],
                matcher,
            )
        ]
        migrated, _summary = benchmark.migrate_matcher_input_snapshots(fixture, evaluated, profiles)

        item = benchmark.evaluate_case(migrated["cases"][0], profiles["profile"], [], matcher)

        self.assertEqual(item.resolution.resolution_status, "fixture_self_contained_snapshot")
        self.assertFalse(item.resolution.used_live_db)
        self.assertEqual(item.row["title"], "Portuguese Review Rater")

    def test_human_decision_fields_are_unchanged_by_migration(self):
        original = human_case(
            case_id="human_001",
            url="https://example.test/jobs/1",
            expected_label="plausible",
            expected_section="also_worth_reviewing",
            human_notes="Keep this calibrated label.",
            rationale="Human-calibrated rationale.",
        )
        fixture = {"cases": [deepcopy(original)]}
        profiles = {"profile": profile()}
        evaluated = [
            benchmark.evaluate_case(fixture["cases"][0], profiles["profile"], [live_row()], matcher)
        ]

        migrated, _summary = benchmark.migrate_matcher_input_snapshots(fixture, evaluated, profiles)

        self.assertEqual(benchmark.decision_fields(migrated["cases"][0]), benchmark.decision_fields(original))


if __name__ == "__main__":
    unittest.main()
