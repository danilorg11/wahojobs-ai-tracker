import unittest

from tests.persistent_profiles_repository_test_support import canonical_fixture
from wahojobs.matching.taxonomy import CAREER_LEVELS
from wahojobs.profiles.canonical import SENIORITY_LEVELS
from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2
from wahojobs.profiles.preference_model import profile_preference_control_catalog_v1
from wahojobs.profiles.seniority_presentation import (
    CANDIDATE_SENIORITY_DISPLAY_GROUPS,
    candidate_seniority_display_choices,
    candidate_seniority_display_label,
    target_career_level_display_choices,
)


class SeniorityPresentationTests(unittest.TestCase):
    def test_every_persisted_candidate_value_has_exactly_one_display_group(self):
        grouped_values = [
            value
            for group in CANDIDATE_SENIORITY_DISPLAY_GROUPS
            for value in group.values
        ]
        self.assertEqual(set(grouped_values), set(SENIORITY_LEVELS))
        self.assertEqual(len(grouped_values), len(set(grouped_values)))
        for value in SENIORITY_LEVELS:
            profile = canonical_fixture()
            profile["experience"]["seniority"] = value
            self.assertEqual(validate_canonical_profile_v2(profile), profile)

    def test_legacy_aliases_display_once_and_new_choices_use_preferred_values(self):
        choices = candidate_seniority_display_choices()
        self.assertEqual(
            [choice["label"] for choice in choices].count("Entry-level"), 1
        )
        self.assertEqual(
            [choice["label"] for choice in choices].count("Mid-level"), 1
        )
        by_key = {choice["key"]: choice for choice in choices}
        self.assertEqual(by_key["entry_level"]["value"], "entry-level")
        self.assertEqual(by_key["mid_level"]["value"], "mid")

    def test_existing_raw_alias_is_preserved_by_its_display_choice(self):
        for raw_value, key, label in (
            ("entry-level", "entry_level", "Entry-level"),
            ("junior", "entry_level", "Entry-level"),
            ("mid", "mid_level", "Mid-level"),
            ("mid-level", "mid_level", "Mid-level"),
        ):
            choice = next(
                item
                for item in candidate_seniority_display_choices(raw_value)
                if item["key"] == key
            )
            self.assertEqual(choice["value"], raw_value)
            self.assertEqual(choice["label"], label)

    def test_advanced_specialist_is_distinct_from_senior(self):
        self.assertEqual(
            candidate_seniority_display_label("advanced"), "Advanced specialist"
        )
        self.assertEqual(candidate_seniority_display_label("senior"), "Senior")
        self.assertNotEqual(
            candidate_seniority_display_label("advanced"),
            candidate_seniority_display_label("senior"),
        )

    def test_all_target_career_levels_remain_selectable_in_product_order(self):
        choices = target_career_level_display_choices()
        self.assertEqual(
            tuple(choice["code"] for choice in choices),
            ("internship", "entry", "mid", "senior", "lead", "principal", "manager"),
        )
        self.assertEqual({choice["code"] for choice in choices}, set(CAREER_LEVELS))
        self.assertEqual(
            tuple(choice["label"] for choice in choices),
            (
                "Internship",
                "Entry-level",
                "Mid-level",
                "Senior",
                "Lead",
                "Principal",
                "Manager",
            ),
        )
        catalog = profile_preference_control_catalog_v1()
        target = next(
            dimension
            for dimension in catalog["dimensions"]
            if dimension["id"] == "accepted_career_levels"
        )
        self.assertEqual(target["choices"], choices)


if __name__ == "__main__":
    unittest.main()
