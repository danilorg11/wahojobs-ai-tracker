import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scripts.run_daily as run_daily
import wahojobs.crawler.pipeline as pipeline
import wahojobs.db.repository as repository
from wahojobs.crawler.companies.meridial import MERIDIAL_GREENHOUSE_CONFIG
from wahojobs.crawler.source_registry import (
    REGISTRY_PATH,
    assert_production_dispatch_allowed,
    dry_run_entries,
    load_source_registry,
    production_entries,
)


class SourceRegistryTests(unittest.TestCase):
    def payload(self):
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def test_approved_registry_has_explicit_safe_enablement(self):
        entries = load_source_registry()
        by_company = {entry.company_id: entry for entry in entries}

        self.assertEqual(
            set(by_company),
            {"meridial", "gitlab", "customerio", "testlio", "invisible"},
        )
        self.assertEqual(
            [entry.company_id for entry in dry_run_entries(entries)],
            ["meridial", "gitlab", "customerio", "testlio"],
        )
        self.assertEqual(
            [entry.company_id for entry in production_entries(entries)],
            ["meridial"],
        )
        for company_id in ("gitlab", "customerio", "testlio"):
            entry = by_company[company_id]
            self.assertTrue(entry.connector_enabled_for_dry_run)
            self.assertFalse(entry.product_enabled)
            self.assertFalse(entry.production_crawl_enabled)
            self.assertFalse(entry.terms_approved)
            self.assertEqual(entry.consecutive_complete_snapshots, 0)
        invisible = by_company["invisible"]
        self.assertFalse(invisible.connector_enabled_for_dry_run)
        self.assertFalse(invisible.product_enabled)
        self.assertFalse(invisible.production_crawl_enabled)

    def test_meridial_registry_config_matches_existing_control(self):
        meridial = next(
            entry for entry in load_source_registry() if entry.company_id == "meridial"
        )
        self.assertEqual(meridial.greenhouse_config(), MERIDIAL_GREENHOUSE_CONFIG)
        self.assertEqual(meridial.consecutive_complete_snapshots, 3)

    def test_pilot_boards_are_not_registered_with_normal_crawler_or_db_seeds(self):
        for company_id in ("gitlab", "customerio", "testlio"):
            self.assertNotIn(company_id, pipeline.CRAWLERS)
            self.assertFalse(hasattr(repository, f"{company_id.upper()}_SEED"))

    def test_registry_rejects_unknown_enums_vocabularies_and_types(self):
        cases = {
            "ats_provider": "other",
            "source_family": "arbitrary",
            "priority_tier": "production",
            "terms_review_status": "looks_good",
            "acceptance_review_status": "reviewed",
            "temporary_closure_status": "healthy",
            "persona_coverage_status": "go",
            "parser_version": "future-parser",
            "target_families": ["made_up_family"],
            "target_countries": ["Atlantis"],
            "target_languages": ["klingon"],
            "crawl_cadence_hours": True,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                payload = self.payload()
                payload["sources"][1][field] = value
                with self.temporary_registry(payload) as path:
                    with self.assertRaises(ValueError):
                        load_source_registry(path)

        payload = self.payload()
        payload["registry_version"] = 1
        with self.temporary_registry(payload) as path:
            with self.assertRaises(ValueError):
                load_source_registry(path)

    def test_registry_rejects_unknown_nested_fields_and_unsafe_policy(self):
        payload = self.payload()
        payload["sources"][1]["count_drop_policy"]["unknown"] = 1
        with self.temporary_registry(payload) as path:
            with self.assertRaises(ValueError):
                load_source_registry(path)

        for fraction in (0.01, 1, True, 1.1):
            with self.subTest(fraction=fraction):
                payload = self.payload()
                payload["sources"][1]["count_drop_policy"][
                    "minimum_retained_fraction"
                ] = fraction
                with self.temporary_registry(payload) as path:
                    with self.assertRaises(ValueError):
                        load_source_registry(path)

    def test_registry_rejects_malformed_urls_hosts_and_duplicate_identities(self):
        payload = self.payload()
        payload["sources"][1]["careers_url"] = "http://job-boards.greenhouse.io/gitlab"
        with self.temporary_registry(payload) as path:
            with self.assertRaises(ValueError):
                load_source_registry(path)

        payload = self.payload()
        payload["sources"][1]["allowed_job_hosts"] = ["evil.example"]
        payload["sources"][1]["careers_url"] = "https://evil.example/gitlab"
        with self.temporary_registry(payload) as path:
            with self.assertRaises(ValueError):
                load_source_registry(path)

        payload = self.payload()
        duplicate = copy.deepcopy(payload["sources"][1])
        duplicate["registry_id"] = "greenhouse_gitlab_duplicate"
        duplicate["company_id"] = "gitlab_duplicate"
        payload["sources"].append(duplicate)
        with self.temporary_registry(payload) as path:
            with self.assertRaises(ValueError):
                load_source_registry(path)

    def test_registry_rejects_generic_or_contradictory_enablement(self):
        payload = self.payload()
        payload["sources"][1]["enabled"] = True
        with self.temporary_registry(payload) as path:
            with self.assertRaises(ValueError):
                load_source_registry(path)

        for updates in (
            {"product_enabled": True},
            {"production_crawl_enabled": True},
        ):
            payload = self.payload()
            payload["sources"][1].update(updates)
            with self.temporary_registry(payload) as path:
                with self.assertRaises(ValueError):
                    load_source_registry(path)

    def test_production_enablement_requires_valid_historical_gate_evidence(self):
        payload = self.payload()
        source = payload["sources"][1]
        source.update(
            {
                "priority_tier": "control",
                "product_enabled": True,
                "production_crawl_enabled": True,
                "terms_review_status": "approved",
                "acceptance_review_status": "approved",
                "temporary_closure_status": "passed",
                "persona_coverage_status": "passed",
            }
        )
        with self.temporary_registry(payload) as path:
            with self.assertRaises(ValueError):
                load_source_registry(path)

        source["readiness_observations"] = self.complete_observations()
        with self.temporary_registry(payload) as path:
            entries = load_source_registry(path)
        gitlab = next(entry for entry in entries if entry.company_id == "gitlab")
        self.assertTrue(gitlab.production_crawl_enabled)
        self.assertEqual(gitlab.consecutive_complete_snapshots, 3)

    def test_readiness_requires_distinct_runs_24_hours_and_unbroken_streak(self):
        payload = self.payload()
        source = payload["sources"][1]
        source["readiness_observations"] = self.complete_observations(
            timestamps=(
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T06:00:00+00:00",
                "2026-07-15T12:00:00+00:00",
            )
        )
        with self.temporary_registry(payload) as path:
            entry = load_source_registry(path)[1]
        self.assertEqual(entry.consecutive_complete_snapshots, 0)

        source["readiness_observations"] = self.complete_observations()
        source["readiness_observations"][1]["outcome"] = "partial"
        with self.temporary_registry(payload) as path:
            entry = load_source_registry(path)[1]
        self.assertEqual(entry.consecutive_complete_snapshots, 1)

    def test_registry_only_expansion_requires_no_company_module(self):
        payload = self.payload()
        prototype = copy.deepcopy(payload["sources"][1])
        prototype.update(
            {
                "registry_id": "greenhouse_example",
                "company_id": "example",
                "company_name": "Example",
                "board_identifier": "example",
                "careers_url": "https://job-boards.greenhouse.io/example",
            }
        )
        payload["sources"].append(prototype)
        with self.temporary_registry(payload) as path:
            entries = load_source_registry(path)
        example = next(entry for entry in entries if entry.company_id == "example")
        self.assertEqual(example.greenhouse_config().board_token, "example")
        self.assertNotIn("example", pipeline.CRAWLERS)

    def test_registry_is_authoritative_before_ordinary_dispatch_opens_database(self):
        for source in (
            "invisible",
            "invisibletech",
            "invisible-technologies",
            "gitlab",
            "greenhouse_gitlab",
            "customerio",
            "testlio",
        ):
            with self.subTest(source=source), patch.object(pipeline, "get_connection") as connect:
                with self.assertRaises(PermissionError):
                    pipeline.run_crawl(source)
                connect.assert_not_called()
        self.assertIsNotNone(assert_production_dispatch_allowed("meridial"))

    def test_daily_selection_cannot_bypass_disabled_invisible(self):
        sources, blocked = run_daily.select_daily_sources(include_experimental=True)
        self.assertNotIn("invisible", sources)
        self.assertEqual(blocked, ["invisible"])
        self.assertIn("meridial", sources)

    def complete_observations(self, timestamps=None):
        timestamps = timestamps or (
            "2026-07-13T12:00:00+00:00",
            "2026-07-14T12:00:00+00:00",
            "2026-07-15T12:00:00+00:00",
        )
        return [
            {
                "run_id": f"run-{index}",
                "observed_at": timestamp,
                "outcome": "complete",
                "parser_version": "greenhouse-job-board-v1",
                "accepted_record_count": 100,
            }
            for index, timestamp in enumerate(timestamps, start=1)
        ]

    def temporary_registry(self, payload):
        class RegistryContext:
            def __enter__(inner):
                inner.temp = tempfile.TemporaryDirectory()
                inner.path = Path(inner.temp.name) / "registry.json"
                inner.path.write_text(json.dumps(payload), encoding="utf-8")
                return inner.path

            def __exit__(inner, *_args):
                inner.temp.cleanup()

        return RegistryContext()


if __name__ == "__main__":
    unittest.main()
