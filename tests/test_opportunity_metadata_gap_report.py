import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import opportunity_metadata_gap_report as gap_report
import profile_match_digest as matcher
from wahojobs.classification import MARKET_COUNT_POLICY_COUNT_LIVE, SOURCE_TIER_CORE
from wahojobs.matching.languages import row_language_text


class OpportunityMetadataGapReportTests(unittest.TestCase):
    def test_matcher_rows_include_existing_canonical_language_and_locale(self):
        conn = build_memory_db()
        rows = matcher.get_active_rows(conn, policy=MARKET_COUNT_POLICY_COUNT_LIVE)

        self.assertEqual(len(rows), 2)
        by_title = {row["title"]: row for row in rows}
        spanish = by_title["Audio Evaluator"]
        generic = by_title["General AI Reviewer"]

        self.assertEqual(spanish["language"], "Spanish")
        self.assertEqual(spanish["language_locale"], "Spanish Mexico")
        self.assertIsNone(spanish["required_languages"])
        self.assertIsNone(generic["language"])
        self.assertIsNone(generic["language_locale"])

    def test_row_language_text_uses_canonical_language_metadata(self):
        row = {
            "title": "Audio Evaluator",
            "canonical_title": "Audio Evaluator",
            "department": "Audio",
            "expertise": "Audio",
            "source_category": "Audio",
            "commitment": "Freelance",
            "language": "Spanish",
            "language_locale": "Spanish Mexico",
            "required_languages": None,
        }

        text = row_language_text(row)

        self.assertIn("Spanish", text)
        self.assertIn("Spanish Mexico", text)

    def test_unsupported_canonical_language_uses_existing_language_gate(self):
        profile = {
            "profile_id": "english_only",
            "display_name": "English Only",
            "summary": "English language reviewer",
            "education_level": "not_specified",
            "degrees_or_domains": ["language"],
            "languages": ["English"],
            "skills": ["review"],
            "work_preferences": ["remote"],
            "constraints": [],
            "target_opportunity_types": ["language review"],
            "notes": "",
            "avoid_keywords": [],
            "signals": [("Language work signal", ["language", "audio"], 20)],
        }
        row = base_matcher_row(
            title="Audio Evaluator",
            language="Spanish",
            language_locale="Spanish Mexico",
        )

        scored = matcher.score_opportunity(profile, row)

        self.assertFalse(scored["eligible_for_personalized"])
        self.assertEqual(scored["unsupported_languages"], ["spanish"])

    def test_report_classifies_high_confidence_and_ambiguous_patterns(self):
        rows = [
            report_row("English (US) Audio Generalist Evaluator Expert", source_slug="mercor"),
            report_row("Generalist - English & Assamese", source_slug="mercor"),
            report_row("English Language Specialist (US Only)", source_slug="meridial", language="English"),
            report_row("Remote reviewer - United States", source_slug="example", location="Remote"),
        ]

        report = gap_report.analyze_rows(rows)
        rendered = gap_report.render_report(report)

        totals = report["totals"]
        self.assertEqual(totals["rows_inspected"], 4)
        self.assertGreaterEqual(totals["title_locale_pattern_missing_canonical_locale"], 1)
        self.assertGreaterEqual(totals["title_language_pattern_missing_canonical_language"], 1)
        self.assertGreaterEqual(totals["ambiguous_or_risky_pattern"], 1)
        self.assertGreaterEqual(totals["title_location_restriction_needs_review"], 1)
        self.assertIn("Opportunity Metadata Gap Report", rendered)
        self.assertIn("mercor", rendered)


def build_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE companies (
          id INTEGER PRIMARY KEY,
          name TEXT,
          slug TEXT,
          source_tier TEXT,
          inventory_model TEXT,
          market_count_policy TEXT
        );
        CREATE TABLE jobs (
          id INTEGER PRIMARY KEY,
          company_id INTEGER,
          title TEXT,
          location TEXT,
          url TEXT,
          department TEXT,
          expertise TEXT,
          commitment TEXT,
          opportunity_kind TEXT,
          availability_basis TEXT,
          include_in_live_market_estimate INTEGER,
          canonical_opportunity_id INTEGER,
          is_active INTEGER
        );
        CREATE TABLE canonical_opportunities (
          id INTEGER PRIMARY KEY,
          canonical_title TEXT,
          source_category TEXT,
          language TEXT,
          language_locale TEXT
        );
        INSERT INTO companies VALUES (1, 'Example', 'example', 'core', 'live_feed', 'count_live');
        INSERT INTO canonical_opportunities VALUES (1, 'Audio Evaluator', 'Audio', 'Spanish', 'Spanish Mexico');
        INSERT INTO canonical_opportunities VALUES (2, 'General AI Reviewer', 'Generalist', NULL, NULL);
        INSERT INTO jobs VALUES (
          1, 1, 'Audio Evaluator', 'Remote', 'https://example.com/1', 'Audio', 'Audio',
          'Freelance', 'live_posting', 'api_feed', 1, 1, 1
        );
        INSERT INTO jobs VALUES (
          2, 1, 'General AI Reviewer', 'Remote', 'https://example.com/2', 'Generalist', 'Generalist',
          'Freelance', 'live_posting', 'api_feed', 1, 2, 1
        );
        """
    )
    return conn


def base_matcher_row(title, language=None, language_locale=None):
    return {
        "job_id": 1,
        "title": title,
        "location": "Remote",
        "url": "https://example.com/1",
        "department": "Audio",
        "expertise": "Audio",
        "commitment": "Freelance",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": 1,
        "source": "Example",
        "source_slug": "example",
        "source_tier": SOURCE_TIER_CORE,
        "inventory_model": "live_feed",
        "market_count_policy": MARKET_COUNT_POLICY_COUNT_LIVE,
        "canonical_title": "Audio Evaluator",
        "source_category": "Audio",
        "language": language,
        "language_locale": language_locale,
        "required_languages": None,
    }


def report_row(title, source_slug="example", location="Remote", language=None, language_locale=None):
    return {
        "job_id": 1,
        "title": title,
        "location": location,
        "department": "Unknown",
        "expertise": "Unknown",
        "commitment": "",
        "canonical_opportunity_id": None,
        "source": source_slug.title(),
        "source_slug": source_slug,
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "canonical_title": None,
        "source_category": "Unknown",
        "language": language,
        "language_locale": language_locale,
    }


if __name__ == "__main__":
    unittest.main()
