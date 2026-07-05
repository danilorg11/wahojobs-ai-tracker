import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import profile_match_digest as matcher
import profile_to_matches_preview as preview
from wahojobs.matching.metadata_overlay import (
    DEFAULT_OVERLAY_PATH,
    OpportunityMetadataOverlay,
    apply_overlay_to_row,
    apply_overlay_to_rows,
    load_overlay,
)


class OpportunityMetadataOverlayTests(unittest.TestCase):
    def test_loader_loads_committed_overlay_shape(self):
        overlay = load_overlay(DEFAULT_OVERLAY_PATH, required=True)

        self.assertTrue(overlay.enabled)
        self.assertEqual(55, len(overlay.records_by_key))
        assamese = overlay.records_by_key["job_id:mercor:986"]
        self.assertEqual(["Assamese", "English"], assamese["required_languages"])
        self.assertTrue(assamese["provenance"])

    def test_overlay_matching_uses_stable_keys(self):
        overlay = load_overlay(DEFAULT_OVERLAY_PATH, required=True)
        row = matcher_row(
            job_id="986",
            source_slug="mercor",
            title="Generalist Role",
            required_languages=None,
        )
        enriched = apply_overlay_to_row(row, overlay)

        self.assertTrue(enriched["metadata_overlay_applied"])
        self.assertEqual("job_id:mercor:986", enriched["metadata_overlay_key"])
        self.assertEqual("Assamese and English", enriched["required_languages"])
        self.assertIn("omgr_mercor_ambiguous-metadata_ampersand-language-list_986", enriched["overlay_review_ids"])

    def test_overlay_required_languages_feed_language_eligibility(self):
        overlay = load_overlay(DEFAULT_OVERLAY_PATH, required=True)
        row = apply_overlay_to_row(
            matcher_row(
                job_id="986",
                source_slug="mercor",
                title="Generalist Role",
                required_languages=None,
            ),
            overlay,
        )
        scored = matcher.score_opportunity(english_spanish_profile(), row)

        self.assertFalse(scored["eligible_for_personalized"])
        self.assertIn("assamese", scored["unsupported_languages"])
        self.assertEqual("all_required", scored["language_requirement_mode"])

    def test_overlay_does_not_overwrite_existing_language_locale_with_blank(self):
        overlay = OpportunityMetadataOverlay(
            path=Path("synthetic.json"),
            records_by_key={
                "job_id:example:1": {
                    "stable_opportunity_key": "job_id:example:1",
                    "required_languages": [],
                    "language_locale": [],
                    "location_restriction": [],
                    "metadata_source": "human_reviewed_title_inference",
                    "provenance": [{"review_id": "synthetic"}],
                    "warnings": [],
                }
            },
        )
        row = matcher_row(
            job_id="1",
            source_slug="example",
            language="Spanish",
            language_locale="Spanish Mexico",
        )
        enriched = apply_overlay_to_row(row, overlay)

        self.assertEqual("Spanish", enriched["language"])
        self.assertEqual("Spanish Mexico", enriched["language_locale"])
        self.assertIsNone(enriched["required_languages"])

    def test_apply_overlay_to_rows_preserves_rows_when_disabled(self):
        rows = [matcher_row(job_id="986", source_slug="mercor", required_languages=None)]
        enriched = apply_overlay_to_rows(rows, None)

        self.assertEqual(rows, enriched)

    def test_preview_loads_overlay_by_default_and_can_disable_it(self):
        rows_with_overlay, status = preview.load_preview_rows(use_overlay=True)
        rows_without_overlay, disabled = preview.load_preview_rows(use_overlay=False)

        self.assertTrue(status["enabled"])
        self.assertEqual(55, status["records_loaded"])
        self.assertGreater(status["rows_enriched"], 0)
        self.assertFalse(disabled["enabled"])
        self.assertEqual(0, disabled["rows_enriched"])
        self.assertGreater(
            sum(1 for row in rows_with_overlay if row.get("metadata_overlay_applied")),
            sum(1 for row in rows_without_overlay if row.get("metadata_overlay_applied")),
        )

    def test_overlay_provenance_is_available_in_preview_diagnostics(self):
        context = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            limit=200,
        )
        overlay_matches = [
            match
            for section in preview.SECTION_ORDER
            for match in context["matches"][section]
            if match.get("metadata_overlay_applied")
        ]

        self.assertTrue(overlay_matches)
        self.assertTrue(
            any(
                "Reviewed metadata overlay applied" in diagnostic
                for match in overlay_matches
                for diagnostic in match["preview_diagnostics"]
            )
        )
        self.assertTrue(any(match.get("overlay_review_ids") for match in overlay_matches))

    def test_cli_no_overlay_disables_overlay_status(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "profile_to_matches_preview.py"),
                "--input-text",
                "I speak English and Spanish and want remote AI data tasks.",
                "--input-style",
                "short_paragraph",
                "--format",
                "json",
                "--limit",
                "1",
                "--no-overlay",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)

        self.assertFalse(data["metadata_overlay"]["enabled"])
        self.assertEqual(0, data["metadata_overlay"]["rows_enriched"])

    def test_oneforma_language_pair_overlay_does_not_merge_unrelated_variants(self):
        overlay = load_overlay(DEFAULT_OVERLAY_PATH, required=True)
        records = [
            record
            for record in overlay.records_by_key.values()
            if any(
                "Arabic (Saudi Arabia) - Bengali (India)" in provenance["evidence_text"]
                for provenance in record["provenance"]
            )
        ]

        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("job_id:oneforma:6839", record["stable_opportunity_key"])
        self.assertEqual(["Arabic", "Bengali"], record["required_languages"])
        self.assertEqual(["Arabic (Saudi Arabia)", "Bengali (India)"], record["language_locale"])
        self.assertEqual(["omgr_oneforma_language-requirement_translation-or-translator_6839"], [
            provenance["review_id"] for provenance in record["provenance"]
        ])

    def test_overlay_review_ids_appear_once_across_overlay(self):
        overlay = load_overlay(DEFAULT_OVERLAY_PATH, required=True)
        review_ids = [
            provenance["review_id"]
            for record in overlay.records_by_key.values()
            for provenance in record["provenance"]
        ]

        self.assertEqual(60, len(review_ids))
        self.assertEqual(60, len(set(review_ids)))


def matcher_row(**overrides):
    row = {
        "job_id": "1",
        "title": "Example Role",
        "location": "Remote",
        "url": "https://example.com/role",
        "department": "Language and Audio",
        "expertise": "Language and Audio",
        "commitment": "",
        "opportunity_kind": "live_posting",
        "availability_basis": "active_posting",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": "",
        "source": "Example",
        "source_slug": "example",
        "source_tier": "core",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "canonical_title": "",
        "source_category": "Language and Audio",
        "language": None,
        "language_locale": None,
        "required_languages": None,
    }
    row.update(overrides)
    return row


def english_spanish_profile():
    return {
        "profile_id": "beginner_bilingual_no_degree",
        "display_name": "Beginner bilingual",
        "summary": "English and Spanish beginner profile.",
        "education_level": "no_degree",
        "degrees_or_domains": ["generalist"],
        "languages": ["English", "Spanish"],
        "skills": ["writing", "review"],
        "work_preferences": ["remote", "flexible"],
        "constraints": ["no college degree"],
        "target_opportunity_types": ["AI training", "data annotation"],
        "signals": [("Generalist", ["generalist", "ai training"], 8)],
        "avoid_keywords": [],
    }


if __name__ == "__main__":
    unittest.main()
