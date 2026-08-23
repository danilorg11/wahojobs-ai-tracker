import unittest
from copy import deepcopy
from datetime import datetime, timezone

from scripts import profile_match_digest as matcher
from scripts import profile_to_matches_preview as preview
from tests.test_canonical_profile_v2 import load_cases, ordinal_resolver, persistent_id
from tests.test_profile_preference_model import with_preference_model
from tests.test_typed_match_criteria import matcher_row
from wahojobs.matching.typed_criteria import (
    bridge_existing_matcher_eligibility,
    run_typed_match_criteria_shadow,
)
from wahojobs.profiles.canonical import canonical_to_matcher_profile
from wahojobs.profiles.canonical_v2 import (
    convert_v1_to_v2,
    project_v2_to_matcher_v1,
    validate_canonical_profile_v2,
)
from wahojobs.profiles.preference_model import empty_profile_preferences_v1


EVALUATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def canonical_v2(*, with_preferences=False):
    fixture = load_cases()[0]["expected_canonical_profile"]
    profile = convert_v1_to_v2(
        fixture,
        persistent_profile_id=persistent_id(1),
        source_ordinal_resolver=ordinal_resolver,
    )
    if not with_preferences:
        return profile
    model = empty_profile_preferences_v1()
    model["employment_relationships"] = ["employee"]
    return validate_canonical_profile_v2(with_preference_model(profile, model))


def matcher_profile(profile_v2):
    projected = project_v2_to_matcher_v1(
        profile_v2,
        matcher_profile_id="eligibility-bridge",
    )
    profile = canonical_to_matcher_profile(projected)
    profile["languages"] = ["English"]
    profile["country"] = "Brazil"
    profile["location"] = "Brazil"
    return profile


def authoritative_match(profile, row):
    scored = matcher.score_opportunity(profile, row)
    return preview.apply_preview_guardrails(
        profile,
        row,
        scored,
        evaluated_at=EVALUATED_AT,
    )


def outcomes_by_id(match):
    return {
        item.criterion_id: item
        for item in bridge_existing_matcher_eligibility(match)
    }


class TypedEligibilityBridgeTests(unittest.TestCase):
    def test_language_bridge_agrees_with_existing_personalized_gate(self):
        profile = matcher_profile(canonical_v2())
        incompatible_row = matcher_row()
        incompatible_row["required_languages"] = "Spanish"
        incompatible = authoritative_match(profile, incompatible_row)
        self.assertFalse(incompatible["eligible_for_personalized"])
        self.assertIn(
            "personalized_eligibility_failed",
            incompatible["actionability_cap_reasons"],
        )
        outcome = outcomes_by_id(incompatible)["eligibility.required_languages"]
        self.assertEqual(
            (outcome.outcome, outcome.reason_code),
            ("fail", "required_language_incompatible"),
        )
        self.assertFalse(outcome.potentially_relaxable)

        compatible_row = matcher_row(job_id=7003)
        compatible_row["required_languages"] = "English"
        compatible = authoritative_match(profile, compatible_row)
        self.assertTrue(compatible["eligible_for_personalized"])
        self.assertEqual(
            outcomes_by_id(compatible)["eligibility.required_languages"].outcome,
            "pass",
        )

        not_applicable = authoritative_match(profile, matcher_row(job_id=7004))
        self.assertEqual(
            outcomes_by_id(not_applicable)["eligibility.required_languages"].outcome,
            "not_applicable",
        )

    def test_location_bridge_preserves_pass_fail_unknown_and_not_applicable(self):
        profile = matcher_profile(canonical_v2())
        incompatible_row = matcher_row()
        incompatible_row["location"] = "Remote - United States"
        incompatible = authoritative_match(profile, incompatible_row)
        self.assertEqual(incompatible["location_eligibility_status"], "incompatible")
        outcome = outcomes_by_id(incompatible)["eligibility.location"]
        self.assertEqual(
            (outcome.outcome, outcome.reason_code),
            ("fail", "location_eligibility_incompatible"),
        )

        compatible_row = matcher_row(job_id=7003)
        compatible_row["location"] = "Remote - Brazil"
        compatible = authoritative_match(profile, compatible_row)
        self.assertEqual(
            outcomes_by_id(compatible)["eligibility.location"].outcome,
            "pass",
        )

        unknown_profile = dict(profile)
        unknown_profile["country"] = ""
        unknown_profile["location"] = ""
        unknown = authoritative_match(unknown_profile, incompatible_row)
        self.assertEqual(
            outcomes_by_id(unknown)["eligibility.location"].outcome,
            "unknown",
        )

        worldwide_row = matcher_row(job_id=7004)
        worldwide_row["location"] = "Remote - Worldwide"
        worldwide = authoritative_match(profile, worldwide_row)
        self.assertEqual(
            outcomes_by_id(worldwide)["eligibility.location"].outcome,
            "not_applicable",
        )

    def test_credential_bridge_uses_existing_conflict_and_fit_evidence(self):
        absent = matcher_profile(canonical_v2())
        absent["education_level"] = "no_degree"
        absent["credential_status"] = "absent"
        absent["licenses"] = []
        absent["constraints"] = ["no degree", "no specialized credentials"]
        row = matcher_row(title="PhD Biology Expert")
        conflict = authoritative_match(absent, row)
        self.assertIn(
            "explicit_credential_incompatibility",
            conflict["actionability_cap_reasons"],
        )
        outcome = outcomes_by_id(conflict)["eligibility.credentials_licenses"]
        self.assertEqual(
            (outcome.outcome, outcome.reason_code),
            ("fail", "credential_requirement_incompatible"),
        )
        self.assertFalse(outcome.potentially_relaxable)

        confirmed = dict(absent)
        confirmed["education_level"] = "doctorate"
        confirmed["credential_status"] = "explicit"
        confirmed["constraints"] = []
        confirmed_match = authoritative_match(
            confirmed,
            matcher_row(job_id=7003, title="PhD or Master's Biology Expert"),
        )
        self.assertEqual(
            outcomes_by_id(confirmed_match)[
                "eligibility.credentials_licenses"
            ].outcome,
            "pass",
        )

        unconfirmed = matcher_profile(canonical_v2())
        unconfirmed["education_level"] = "unknown"
        unconfirmed["credential_status"] = "unknown"
        unconfirmed["constraints"] = []
        unconfirmed["negative_constraints"] = []
        unconfirmed["summary"] = ""
        unconfirmed["notes"] = ""
        unconfirmed_match = authoritative_match(unconfirmed, row)
        self.assertEqual(
            outcomes_by_id(unconfirmed_match)[
                "eligibility.credentials_licenses"
            ].outcome,
            "unknown",
        )

    def test_professional_domain_bridge_uses_existing_decisive_hard_gate(self):
        profile = matcher_profile(canonical_v2())
        profile["degrees_or_domains"] = []
        profile["skills"] = []
        profile["target_opportunity_types"] = []
        row = matcher_row(title="Finance Expert")
        row["source_category"] = "finance"
        conflict = authoritative_match(profile, row)
        self.assertTrue(conflict["professional_domain_hard_gate_applied"])
        outcome = outcomes_by_id(conflict)["eligibility.professional_domain"]
        self.assertEqual(
            (outcome.outcome, outcome.reason_code),
            ("fail", "professional_domain_incompatible"),
        )

        aligned = dict(profile)
        aligned["degrees_or_domains"] = ["finance"]
        aligned_match = authoritative_match(
            aligned,
            matcher_row(job_id=7003, title="Finance Expert"),
        )
        self.assertFalse(aligned_match["professional_domain_hard_gate_applied"])
        self.assertEqual(
            outcomes_by_id(aligned_match)[
                "eligibility.professional_domain"
            ].outcome,
            "pass",
        )

    def test_soft_relaxation_cannot_hide_eligibility_failure(self):
        profile_v2 = canonical_v2(with_preferences=True)
        profile = matcher_profile(profile_v2)
        row = matcher_row()
        row["commitment"] = "Freelance"
        row["required_languages"] = "Spanish"
        authoritative = authoritative_match(profile, row)
        before_profile = deepcopy(profile_v2)
        before_row = deepcopy(row)
        before_match = deepcopy(authoritative)

        records = run_typed_match_criteria_shadow(
            profile_v2,
            [row],
            {},
            authoritative_matches=[authoritative],
        )
        outcomes = {
            item["criterion_id"]: item
            for item in records[0]["outcomes"]
        }
        soft = outcomes["preferences.employment_relationships"]
        eligibility = outcomes["eligibility.required_languages"]
        self.assertEqual(soft["outcome"], "fail")
        self.assertTrue(soft["potentially_relaxable"])
        self.assertEqual(eligibility["outcome"], "fail")
        self.assertFalse(eligibility["potentially_relaxable"])
        self.assertEqual(profile_v2, before_profile)
        self.assertEqual(row, before_row)
        self.assertEqual(authoritative, before_match)

    def test_legacy_profile_emits_eligibility_without_preference_criteria(self):
        profile_v2 = canonical_v2()
        profile = matcher_profile(profile_v2)
        row = matcher_row()
        authoritative = authoritative_match(profile, row)
        records = run_typed_match_criteria_shadow(
            profile_v2,
            [row],
            authoritative_matches=[authoritative],
        )
        self.assertEqual(records[0]["criteria_source_status"], "absent")
        self.assertEqual(len(records[0]["outcomes"]), 4)
        self.assertTrue(
            all(
                outcome["criterion_class"] == "eligibility"
                and not outcome["potentially_relaxable"]
                for outcome in records[0]["outcomes"]
            )
        )

    def test_eligibility_context_is_bounded_and_does_not_copy_authority_text(self):
        marker = "private-profile-or-opportunity-content"
        profile = matcher_profile(canonical_v2())
        row = matcher_row()
        row["required_languages"] = "Spanish"
        match = authoritative_match(profile, row)
        match["language_eligibility_reason"] = marker
        match["location_eligibility_reason"] = marker
        match["profile_location"] = marker
        match["applicant_location_requirements"] = marker
        match["preview_credential_requirement"] = marker
        match["professional_domain_hard_gate_reason"] = marker

        rendered = repr(
            [
                outcome.as_dict()
                for outcome in bridge_existing_matcher_eligibility(match)
            ]
        )
        self.assertNotIn(marker, rendered)
        self.assertLess(len(rendered), 4_096)

    def test_post_guardrail_sink_does_not_change_visible_match_output(self):
        profile_v2 = canonical_v2()
        projected = project_v2_to_matcher_v1(
            profile_v2,
            matcher_profile_id="eligibility-visible-regression",
        )
        rows = [matcher_row(), matcher_row(job_id=7003, title="Finance Expert")]
        before_rows = deepcopy(rows)
        metadata = {
            "enabled": False,
            "records_loaded": 0,
            "rows_enriched": 0,
        }
        baseline = preview.build_preview_context_from_canonical_rows(
            projected,
            inventory_rows=rows,
            metadata_overlay_status=metadata,
            evaluated_at=EVALUATED_AT,
        )
        captured = []
        shadowed = preview.build_preview_context_from_canonical_rows(
            projected,
            inventory_rows=rows,
            metadata_overlay_status=metadata,
            evaluated_at=EVALUATED_AT,
            evaluated_match_sink=captured.append,
        )
        failing_sink = preview.build_preview_context_from_canonical_rows(
            projected,
            inventory_rows=rows,
            metadata_overlay_status=metadata,
            evaluated_at=EVALUATED_AT,
            evaluated_match_sink=lambda _match: (_ for _ in ()).throw(RuntimeError()),
        )

        self.assertEqual(len(captured), len(rows))
        self.assertEqual(shadowed["matches"], baseline["matches"])
        self.assertEqual(failing_sink["matches"], baseline["matches"])
        self.assertEqual(shadowed["match_summary"], baseline["match_summary"])
        self.assertEqual(rows, before_rows)


if __name__ == "__main__":
    unittest.main()
