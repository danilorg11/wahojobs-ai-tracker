"""Anonymous structural regressions; no personal resume or recorded extraction."""
import json
import unittest

from tests import test_persistent_profile_corrections as support
from tests.test_profile_intake_multidocument import _raw_fact, _validated_source, RESUME_REFERENCE
from wahojobs.profile_intake.contracts import DocumentKind
from wahojobs.profile_intake.review_draft import reconcile_profile_extractions
from wahojobs.profile_intake.runtime import (
    editable_profile_review, education_entry_values, review_collection_entries,
    review_value_for_form, update_editable_review,
)
from wahojobs.profiles.review_entries import explicit_degree_kind, employment_records
from wahojobs.profiles.canonical_v2 import project_v2_to_matcher_v1


def entry(**changes):
    value = dict(kind="bachelor", qualification="Bachelor of Business Administration",
                 field="Business Administration", institution="Example University",
                 status="unknown", completion_year=None)
    value.update(changes)
    return value


class ProfileFidelityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = support.PersistentProfileCorrectionTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def current(self):
        return self.case._current().trusted_dict(include_structured_profile=True)["structured_profile"]

    def apply(self, **updates):
        browser = self.case._build_browser()
        before = self.case._profile_counts()
        form = self.case._browser_apply_offer(browser, changes=tuple(updates.items()))
        # Draft/review/confirmation have not yet written a revision.
        self.assertEqual(self.case._profile_counts(), before)
        result, _ = self.case._post_form(browser, form["action"], form["fields"])
        self.assertEqual(result.status, 303)
        after = self.case._profile_counts()
        self.assertEqual(after[1], before[1] + 1)
        return self.current()

    def test_education_associations_add_edit_remove_survive_confirmation(self):
        original_skills = self.current()["skills"]
        entries = [entry(), entry(kind="not_specified", qualification="Exchange study",
                    institution="Visiting College"), entry(kind="not_specified",
                    qualification="Additional study", field="Computing", institution="")]
        saved = self.apply(education_entries=json.dumps(entries), education_level="not_specified",
                           education_fields="", degrees="", institutions="", education_status="unknown",
                           no_degree=None, hard_constraints="")
        self.assertEqual(saved["education"]["education_level"], "bachelor")
        self.assertEqual(len(saved["education"]["entries"]), 3)
        self.assertEqual(saved["skills"], original_skills)
        # A subsequent unrelated edit must retain all associations and their sources.
        again = self.apply(city="Example City")
        self.assertEqual(again["education"], saved["education"])
        education_sources = lambda profile: [s for s in profile["provenance"]["field_sources"]
                                              if s["field_path"].startswith("education.")]
        # Existing revision-source rebasing points unchanged facts to the
        # previous confirmed profile, not to this city's correction source.
        self.assertTrue(all(s["source_ordinals"] == [1] for s in education_sources(again)))
        entries[0]["institution"] = "Corrected University"
        saved = self.apply(education_entries=json.dumps(entries[:1]))
        self.assertEqual(saved["education"]["entries"], [entries[0]])
        removed = self.apply(education_entries="[]")
        self.assertNotIn("entries", removed["education"])
        self.assertEqual(removed["education"]["degrees"], [])

    def test_employment_is_separate_and_title_removal_does_not_delete_distinct_jobs(self):
        records = ["AI Evaluator, Example A, 2024, remote", "AI Evaluator, Example B, 2025, hybrid"]
        saved = self.apply(job_titles="AI Evaluator", recent_roles=json.dumps(records),
                           specialties="Image evaluation, Annotation quality checks")
        self.assertEqual(saved["experience"]["recent_roles"], records)
        changed = self.apply(job_titles="", recent_roles=json.dumps(records))
        self.assertEqual(changed["experience"]["job_titles"], [])
        self.assertEqual(changed["experience"]["recent_roles"], records)
        self.assertEqual(changed["experience"]["specialties"], saved["experience"]["specialties"])
        older_form = self.apply(recent_roles=None, city="Other City")
        self.assertEqual(older_form["experience"]["recent_roles"], records)
        removed = self.apply(recent_roles="[]")
        self.assertEqual(removed["experience"]["recent_roles"], [])
        projection = project_v2_to_matcher_v1(changed, matcher_profile_id="synthetic-recovery")
        self.assertIn("Image evaluation", str(projection))
        self.assertEqual(employment_records(json.dumps(records)), records)

    def test_country_permissions_remain_alternatives_not_residence_or_availability(self):
        saved = self.apply(country="", eligible_countries="Brazil, Argentina", work_authorization="unknown")
        self.assertFalse(saved["location"].get("residence_country"))
        self.assertFalse(saved["location"].get("country"))
        self.assertEqual(saved["location"]["eligible_countries"], ["Argentina", "Brazil"])
        self.assertEqual(saved["location"]["work_authorization"], "unknown")
        self.assertNotIn("available_hours", saved["preferences"])


class IntakeFidelityStructureTests(unittest.TestCase):
    def review(self, *facts):
        return editable_profile_review(reconcile_profile_extractions((
            _validated_source(DocumentKind.RESUME, RESUME_REFERENCE, *facts),)))

    def test_explicit_degree_is_reviewable_without_invented_institution_pairing(self):
        review = self.review(_raw_fact("education.degrees", "Bachelor of Business Administration"),
                             _raw_fact("education.institutions", "College A"),
                             _raw_fact("education.institutions", "College B"))
        values = education_entry_values(review)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["value"]["kind"], "bachelor")
        self.assertEqual(values[0]["value"]["institution"], "")
        self.assertNotIn("kind", review.education_entries[0].extraction_components)
        for text in ("Exchange Student", "Information Technology Management", "Additional Studies",
                     "Bachelor equivalent preferred", "Not a Bachelor degree", "Bachelor degree course"):
            self.assertIsNone(explicit_degree_kind(text))

    def test_titles_and_employment_details_keep_independent_edit_decisions(self):
        review = self.review(_raw_fact("experience.job_titles", "AI Evaluator"),
                             _raw_fact("experience.recent_roles", "AI Evaluator, Example A, 2024, remote"),
                             _raw_fact("experience.recent_roles", "AI Evaluator, Example B, 2025, remote"))
        self.assertEqual([r["value"] for r in review_collection_entries(review, "job_titles")], ["AI Evaluator"])
        changed = update_editable_review(review,
            tuple(review_value_for_form(f.value) for f in review.facts),
            tuple("remove" if f.field_path == "experience.job_titles" else f.decision for f in review.facts),
            {name: "" for name in review.missing_user_fields})
        self.assertEqual([f.decision for f in changed.facts if f.field_path == "experience.recent_roles"], ["keep", "keep"])

    def test_education_topics_are_not_promoted_to_skills(self):
        review = self.review(_raw_fact("education.fields_or_domains", "Web Development"),
                             _raw_fact("experience.specialties", "Image annotation", explicit=False))
        self.assertFalse(any(f.field_path.startswith("skills.") for f in review.facts))
        self.assertTrue(any(f.field_path == "experience.specialties" for f in review.facts))


if __name__ == "__main__":
    unittest.main()
