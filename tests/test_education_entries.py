from copy import deepcopy
import unittest

from wahojobs.profiles.education_entries import (
    EducationEntryContractError,
    canonicalize_education_entries_v1,
    project_education_entries_to_legacy,
)


def entry(**changes):
    value = {
        "kind": "bachelor",
        "qualification": "Bachelor of Business Administration",
        "field": "Business Administration",
        "institution": "Faculdade Horizonte Paulista",
        "status": "completed",
        "completion_year": 2016,
    }
    value.update(changes)
    return value


class EducationEntryContractTests(unittest.TestCase):
    def test_closed_entries_are_normalized_sorted_and_defensive(self):
        source = [
            entry(
                kind="technical",
                qualification=" Data Analytics Certificate ",
                field="Data  Analytics",
                institution="Instituto Futuro",
                status="in_progress",
                completion_year=2027,
            ),
            entry(),
        ]
        canonical = canonicalize_education_entries_v1(source)
        self.assertEqual(
            [item["completion_year"] for item in canonical],
            [2016, 2027],
        )
        self.assertEqual(canonical[1]["qualification"], "Data Analytics Certificate")
        self.assertEqual(canonical[1]["field"], "Data Analytics")
        source[0]["institution"] = "Changed later"
        self.assertEqual(canonical[1]["institution"], "Instituto Futuro")

    def test_duplicate_invalid_and_oversize_entries_fail_closed(self):
        invalid = deepcopy(entry())
        invalid["unexpected"] = "not allowed"
        cases = (
            [entry(), entry(qualification="bachelor of business administration")],
            [invalid],
            [entry(completion_year=1899)],
            [entry(kind="no_degree")],
            [entry(kind="not_specified", qualification="", field="", institution="")],
            [entry(qualification=f"Qualification {index}") for index in range(25)],
        )
        for value in cases:
            with self.subTest(value=value[0].get("kind")):
                with self.assertRaises(EducationEntryContractError):
                    canonicalize_education_entries_v1(value)

    def test_legacy_projection_is_deterministic_and_never_overstates_mixed_entries(self):
        values = [
            entry(),
            entry(
                kind="technical",
                qualification="Data Analytics Certificate",
                field="Data Analytics",
                institution="Instituto Futuro",
                status="in_progress",
                completion_year=2027,
            ),
        ]
        projected = project_education_entries_to_legacy(
            values,
            {
                "degrees": ["Unpaired short course"],
                "institutions": ["Legacy Institute"],
            },
        )
        self.assertEqual(projected["education_level"], "not_specified")
        self.assertEqual(projected["completion_status"], "unknown")
        self.assertEqual(projected["graduation_years"], [2016, 2027])
        self.assertEqual(
            projected["degrees"],
            [
                "Bachelor of Business Administration",
                "Data Analytics Certificate",
                "Unpaired short course",
            ],
        )
        self.assertIn("Legacy Institute", projected["institutions"])


if __name__ == "__main__":
    unittest.main()
