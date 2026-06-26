import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as benchmark


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


def row(**overrides):
    payload = {
        "job_id": 1,
        "source": "Example Source",
        "source_slug": "example-source",
        "source_tier": "core",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "external_id": "job-1",
        "source_hash": "hash-1",
        "title": "Live Title",
        "canonical_title": None,
        "location": "Remote",
        "url": "https://example.test/jobs/1",
        "department": "General",
        "expertise": "General",
        "commitment": "",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": None,
        "source_category": "General",
    }
    payload.update(overrides)
    return payload


class GoldenSetResolutionTests(unittest.TestCase):
    def test_exact_url_resolution_is_global_and_allows_title_drift(self):
        fixture = case(title="Old Fixture Title", url="https://example.test/jobs/1")
        rows = [
            row(job_id=1, title="Current Drifted Title", url="https://example.test/jobs/1"),
            row(job_id=2, title="Old Fixture Title", url="https://example.test/jobs/2"),
        ]

        resolution = benchmark.resolve_case_opportunity(fixture, rows)

        self.assertTrue(resolution.used_live_db)
        self.assertEqual(resolution.resolution_status, "live_db_url")
        self.assertEqual(resolution.identifier_used, "url")
        self.assertEqual(resolution.row["job_id"], 1)

    def test_exact_external_id_resolves_only_when_unique(self):
        fixture = case(external_id="job-1")
        resolution = benchmark.resolve_case_opportunity(fixture, [row(external_id="job-1")])

        self.assertTrue(resolution.used_live_db)
        self.assertEqual(resolution.resolution_status, "live_db_external_id")

        duplicate = benchmark.resolve_case_opportunity(
            fixture,
            [
                row(job_id=1, external_id="job-1"),
                row(job_id=2, external_id="job-1"),
            ],
        )
        self.assertFalse(duplicate.used_live_db)
        self.assertEqual(duplicate.resolution_status, "ambiguous_fixture_fallback")
        self.assertEqual(duplicate.identifier_used, "external_id")
        self.assertEqual(duplicate.candidate_count, 2)

    def test_source_hash_resolves_only_when_unique(self):
        fixture = case(source_hash="hash-1")
        resolution = benchmark.resolve_case_opportunity(fixture, [row(source_hash="hash-1")])

        self.assertTrue(resolution.used_live_db)
        self.assertEqual(resolution.resolution_status, "live_db_source_hash")

        duplicate = benchmark.resolve_case_opportunity(
            fixture,
            [
                row(job_id=1, source_hash="hash-1"),
                row(job_id=2, source_hash="hash-1"),
            ],
        )
        self.assertFalse(duplicate.used_live_db)
        self.assertEqual(duplicate.resolution_status, "ambiguous_fixture_fallback")

    def test_canonical_id_with_multiple_variants_is_ambiguous(self):
        fixture = case(canonical_opportunity_id=42)
        resolution = benchmark.resolve_case_opportunity(
            fixture,
            [
                row(job_id=1, canonical_opportunity_id=42, title="Variant A"),
                row(job_id=2, canonical_opportunity_id=42, title="Variant B"),
            ],
        )

        self.assertFalse(resolution.used_live_db)
        self.assertTrue(resolution.used_fixture_snapshot)
        self.assertEqual(resolution.resolution_status, "ambiguous_fixture_fallback")
        self.assertEqual(resolution.identifier_used, "canonical_opportunity_id")
        self.assertIn("matched 2 active rows", resolution.ambiguity_reason)

    def test_title_only_match_is_diagnostic_not_selected(self):
        fixture = case(title="Same Title")
        resolution = benchmark.resolve_case_opportunity(
            fixture,
            [row(job_id=5, title="Same Title")],
        )

        self.assertFalse(resolution.used_live_db)
        self.assertTrue(resolution.used_fixture_snapshot)
        self.assertEqual(resolution.resolution_status, "fixture_snapshot_missing_identifier")
        self.assertEqual(resolution.candidate_count, 1)
        self.assertEqual(resolution.diagnostic_candidates[0]["job_id"], "5")
        self.assertTrue(str(resolution.row["job_id"]).startswith("fixture:"))

    def test_missing_live_row_remains_evaluable_from_fixture_snapshot(self):
        fixture = case(external_id="missing-id")
        resolution = benchmark.resolve_case_opportunity(fixture, [row(external_id="other-id")])

        self.assertFalse(resolution.used_live_db)
        self.assertEqual(resolution.resolution_status, "fixture_snapshot_missing_live_row")
        self.assertEqual(resolution.row["title"], "Fixture Title")

    def test_fixture_validation_accepts_draft_and_human_reviewed_sources(self):
        draft = case(label_source="codex_draft")
        benchmark.validate_case(draft, 1)

        reviewed = case(
            label_source="human_reviewed",
            review_required=False,
            human_notes="Reviewed by a human.",
        )
        benchmark.validate_case(reviewed, 2)

    def test_dataannotation_bilingual_snapshot_is_not_auto_enriched_by_title(self):
        fixture = case(
            case_id="portuguese_english_reviewer_010",
            source="DataAnnotation",
            title="Bilingual AI Trainer",
            inventory_model="evergreen_application",
            market_count_policy="report_separately",
        )
        resolution = benchmark.resolve_case_opportunity(
            fixture,
            [row(source="DataAnnotation", source_slug="dataannotation", title="Bilingual AI Trainer")],
        )

        self.assertFalse(resolution.used_live_db)
        self.assertEqual(resolution.resolution_status, "fixture_snapshot_missing_identifier")

    def test_meridial_pci_title_variants_do_not_first_row_fallback(self):
        fixture = case(
            case_id="generalist_no_degree_013",
            source="Meridial",
            title="Pavement Condition Index (PCI) Survey & Annotation Specialist - Freelance AI Trainer Project",
        )
        rows = [
            row(
                job_id=132,
                source="Meridial",
                source_slug="meridial",
                title="Pavement Condition Index (PCI) Survey & Annotation Specialist - Freelance AI Trainer Project",
                location="World Wide - Remote",
                canonical_opportunity_id=1032,
            ),
            row(
                job_id=133,
                source="Meridial",
                source_slug="meridial",
                title="Pavement Condition Index (PCI) Survey & Annotation Specialist - Freelance AI Trainer Project",
                location="Poland",
                canonical_opportunity_id=1032,
            ),
        ]

        resolution = benchmark.resolve_case_opportunity(fixture, rows)

        self.assertFalse(resolution.used_live_db)
        self.assertEqual(resolution.resolution_status, "fixture_snapshot_missing_identifier")
        self.assertEqual(resolution.candidate_count, 2)


if __name__ == "__main__":
    unittest.main()
