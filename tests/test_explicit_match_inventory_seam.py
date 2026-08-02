from copy import deepcopy
from datetime import datetime, timezone
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import profile_to_matches_preview as preview
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer


EVALUATED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def canonical_profile():
    return BaselineHeuristicProfileNormalizer().normalize(
        (
            "I live in Brazil and work as a software engineer. I use Python, "
            "APIs, SQL, testing, and technical documentation. I want remote AI "
            "coding and software review work."
        ),
        "long_paragraph",
        {
            "profile_id": "explicit_inventory_profile",
            "display_name": "Explicit Inventory Profile",
        },
    ).canonical_profile


def inventory_row(job_id=1, *, source_tier="core", title="Python Software Engineer"):
    observed_at = EVALUATED_AT.isoformat()
    return {
        "job_id": job_id,
        "title": title,
        "canonical_title": title,
        "source": "Explicit Inventory Fixture",
        "source_slug": "explicit-inventory-fixture",
        "source_tier": source_tier,
        "location": "Remote",
        "url": f"https://example.test/jobs/{job_id}",
        "department": "Engineering",
        "expertise": "Engineering",
        "source_category": "Engineering",
        "commitment": "Freelance",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": job_id,
        "canonical_is_active": True,
        "job_is_active": True,
        "job_last_seen_at": observed_at,
        "latest_successful_source_run_at": observed_at,
        "source_run_started_at": observed_at,
        "source_run_id": job_id,
        "source_run_qualifies": True,
        "language": None,
        "language_locale": None,
        "required_languages": None,
    }


class ExplicitMatchInventorySeamTests(unittest.TestCase):
    def test_explicit_rows_context_has_no_default_database_or_overlay_fallback(self):
        canonical = canonical_profile()
        rows = [inventory_row()]
        rows_before = deepcopy(rows)
        overlay_status = {
            "enabled": True,
            "path": "explicit-overlay.json",
            "records_loaded": 3,
            "rows_enriched": 1,
        }

        with (
            mock.patch.object(
                preview,
                "load_preview_rows",
                side_effect=AssertionError("default inventory loader used"),
            ),
            mock.patch.object(
                preview,
                "get_connection",
                side_effect=AssertionError("default database used"),
            ),
            mock.patch.object(
                preview,
                "load_overlay",
                side_effect=AssertionError("default overlay used"),
            ),
        ):
            context = preview.build_preview_context_from_canonical_rows(
                canonical,
                inventory_rows=rows,
                metadata_overlay_status=overlay_status,
                evaluated_at=EVALUATED_AT,
            )

        matches = [
            match
            for section in preview.SECTION_ORDER
            for match in context["matches"][section]
        ]
        self.assertEqual([match["display_title"] for match in matches], ["Python Software Engineer"])
        self.assertEqual(context["metadata_overlay"], overlay_status)
        self.assertIsNot(context["metadata_overlay"], overlay_status)
        self.assertEqual(rows, rows_before)

    def test_explicit_query_uses_only_supplied_connection_and_preserves_inventory_order(self):
        connection = object()
        live = inventory_row(1, title="Live role")
        experimental = inventory_row(
            2,
            source_tier=preview.SOURCE_TIER_EXPERIMENTAL,
            title="Experimental role",
        )
        public = inventory_row(3, title="Public role")

        with mock.patch.object(
            preview.matcher,
            "get_active_rows",
            side_effect=([live], [experimental], [public]),
        ) as get_active_rows:
            rows = preview.query_preview_rows(connection)

        self.assertEqual([row["job_id"] for row in rows], [1, 3])
        self.assertEqual(
            get_active_rows.call_args_list,
            [
                mock.call(
                    connection,
                    policy=preview.MARKET_COUNT_POLICY_COUNT_LIVE,
                ),
                mock.call(
                    connection,
                    policy_not=preview.MARKET_COUNT_POLICY_COUNT_LIVE,
                    inventory_models=(preview.INVENTORY_MODEL_EVERGREEN_APPLICATION,),
                ),
                mock.call(
                    connection,
                    policy_not=preview.MARKET_COUNT_POLICY_COUNT_LIVE,
                    inventory_models=(
                        preview.INVENTORY_MODEL_PUBLIC_INVENTORY,
                        preview.INVENTORY_MODEL_MIXED,
                    ),
                ),
            ],
        )

    def test_local_canonical_wrapper_keeps_existing_loader_contract(self):
        rows = [inventory_row()]
        overlay_status = {
            "enabled": False,
            "path": "local-wrapper",
            "records_loaded": 0,
            "rows_enriched": 0,
        }
        with mock.patch.object(
            preview,
            "load_preview_rows",
            return_value=(rows, overlay_status),
        ) as load_preview_rows:
            context = preview.build_preview_context_from_canonical(
                canonical_profile(),
                use_overlay=False,
            )

        load_preview_rows.assert_called_once_with(use_overlay=False)
        self.assertEqual(context["metadata_overlay"], overlay_status)


if __name__ == "__main__":
    unittest.main()
