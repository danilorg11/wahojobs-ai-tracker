import unittest

from wahojobs.matching.locations import (
    LOCATION_ELIGIBLE,
    LOCATION_INCOMPATIBLE,
    LOCATION_NOT_APPLICABLE,
    LOCATION_UNKNOWN,
    location_eligibility,
)


class GreenhouseRegionalLocationTests(unittest.TestCase):
    def check(self, country, requirement):
        return location_eligibility(
            {"country": country},
            {
                "location": "Remote",
                "applicant_location_requirements": requirement,
            },
        )

    def test_required_region_compatibility(self):
        cases = (
            ("Germany", "Americas Remote", LOCATION_INCOMPATIBLE),
            ("Germany", "EMEA Remote", LOCATION_ELIGIBLE),
            ("Brazil", "Americas Remote", LOCATION_ELIGIBLE),
            ("Brazil", "Remote in AMER", LOCATION_ELIGIBLE),
            ("India", "Remote APAC", LOCATION_ELIGIBLE),
        )
        for country, requirement, expected in cases:
            with self.subTest(country=country, requirement=requirement):
                self.assertEqual(self.check(country, requirement).status, expected)

    def test_country_specific_remote_is_exact(self):
        self.assertEqual(
            self.check("Germany", "Remote Germany").status,
            LOCATION_ELIGIBLE,
        )
        self.assertEqual(
            self.check("Brazil", "Remote US").status,
            LOCATION_INCOMPATIBLE,
        )
        self.assertEqual(
            self.check("Germany", "United States Remote").status,
            LOCATION_INCOMPATIBLE,
        )

    def test_generic_remote_is_unknown_not_worldwide(self):
        result = self.check("Germany", "Remote")
        self.assertEqual(result.status, LOCATION_UNKNOWN)
        self.assertFalse(result.actionability_cap_required)

    def test_multiple_regions_are_a_union(self):
        result = self.check("Germany", "Remote, Americas or EMEA")
        self.assertEqual(result.status, LOCATION_ELIGIBLE)

    def test_explicit_global_location_in_a_union_is_worldwide(self):
        result = self.check("Germany", "Global | Remote US")
        self.assertEqual(result.status, LOCATION_NOT_APPLICABLE)

    def test_structured_applicant_requirement_wins_over_generic_location(self):
        result = location_eligibility(
            {"country": "Germany"},
            {
                "location": "Remote",
                "applicant_location_requirements": "Remote in AMER",
            },
        )
        self.assertEqual(result.status, LOCATION_INCOMPATIBLE)


if __name__ == "__main__":
    unittest.main()
