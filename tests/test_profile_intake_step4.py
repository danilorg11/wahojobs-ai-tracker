from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unittest

from tests.ai_profile_import_test_support import confirmed_review
from wahojobs.profile_intake.browser import (
    _REVIEW_STATE_SCRIPT,
    _failure,
    _render_step_four_summary,
    _step_four_summary,
)
from wahojobs.profile_intake.contracts import LanguageValue
from wahojobs.profile_intake.runtime import (
    EditableEducationEntry,
    EditableUserFact,
)
from wahojobs.profiles.preference_model import (
    empty_profile_preferences_v1,
    empty_profile_preferences_v2,
    preference_model_for_v2_editor,
)


def _review_fact(review, field_path, value, *, decision="keep"):
    return replace(
        review.facts[0],
        field_path=field_path,
        review_field=field_path.rsplit(".", 1)[-1],
        value=value,
        suggested=False,
        decision=decision,
        conflict_group=None,
        explicit=True,
        candidate_edited=False,
    )


def _education(reference, *, qualification, field="", institution=""):
    return EditableEducationEntry(
        item_reference=reference,
        origin="user",
        kind="bachelor",
        qualification=qualification,
        field_of_study=field,
        institution=institution,
        status="completed",
        completion_year=2016,
        source_fact_indexes=(),
        source_attributions=(),
        decision="keep",
    )


class ProfileIntakeStepFourSummaryTests(unittest.TestCase):
    def test_background_summary_is_current_bounded_deduplicated_and_escaped(self):
        review = confirmed_review()
        facts = tuple(
            fact for fact in review.facts if not fact.field_path.startswith("location.")
        ) + (
            _review_fact(review, "location.country", "Brazil"),
            _review_fact(review, "experience.job_titles", "Customer Support Specialist"),
            _review_fact(review, "experience.recent_roles", "Administrative Assistant"),
            _review_fact(review, "experience.job_titles", "Search Quality Analyst"),
            _review_fact(review, "languages", LanguageValue("Portuguese", "native", "Brazil")),
            _review_fact(review, "languages", LanguageValue("<English>", "professional", None)),
            _review_fact(review, "languages", LanguageValue("Spanish", None, None)),
            _review_fact(review, "languages", LanguageValue("French", None, None)),
            _review_fact(review, "skills.normalized", "python"),
            _review_fact(review, "experience.specialties", "SQL", decision="remove"),
            _review_fact(review, "experience.industries", "Business services"),
            _review_fact(review, "experience.professional_domains", "AI Training"),
            _review_fact(review, "experience.contribution_type", "individual contributor"),
        )
        review = replace(
            review,
            facts=facts,
            user_facts=(
                EditableUserFact(
                    item_reference="uci_skills_000",
                    collection_id="skills",
                    field_path="skills.normalized",
                    value="SEO",
                    decision="keep",
                ),
            ),
            education_entries=(
                _education(
                    "edu_000",
                    qualification="Bachelor of Business Administration",
                    field="Business Administration",
                    institution="Faculdade Horizonte Paulista",
                ),
            ),
        )

        summary = _step_four_summary(review)
        self.assertEqual(
            summary["background"],
            (
                "Brazil",
                "Customer Support Specialist and Search Quality Analyst",
                "Bachelor of Business Administration",
                "Portuguese, <English>, Spanish +1 more language",
                "2 skills and areas of expertise",
            ),
        )
        markup = _render_step_four_summary(review)
        self.assertIn("Portuguese, &lt;English&gt;, Spanish +1 more language", markup)
        self.assertNotIn("Business services", markup)
        self.assertNotIn("AI Training", markup)
        self.assertNotIn("individual contributor", markup)
        self.assertNotIn("senior", markup.lower())

    def test_missing_location_has_no_placeholder_and_multiple_education_is_truthful(self):
        review = confirmed_review()
        review = replace(
            review,
            facts=tuple(
                fact
                for fact in review.facts
                if not fact.field_path.startswith("location.")
            ),
            education_entries=(
                _education("edu_000", qualification="Bachelor of Business Administration"),
                _education("edu_001", qualification="Customer Experience Certificate"),
            ),
        )
        summary = _step_four_summary(review)
        self.assertNotIn("Not specified", summary["background"])
        self.assertNotIn("Location unavailable", summary["background"])
        self.assertIn("2 education entries reviewed", summary["background"])

    def test_preferences_show_only_explicit_choices_and_real_compensation(self):
        unrestricted = confirmed_review(preference_model=empty_profile_preferences_v2())
        self.assertEqual(
            _step_four_summary(unrestricted)["preferences"],
            ("No specific work-condition preferences",),
        )

        model = deepcopy(empty_profile_preferences_v2())
        model["employment_relationships"] = ["employee"]
        model["workloads"] = ["full_time", "part_time"]
        model["schedule"]["flexibility_modes"] = ["flexible"]
        model["accepted_phone_voice_modes"] = ["non_phone"]
        model["job_interests"] = ["customer_support"]
        model["compensation_expectations"] = [
            {
                "minimum_kind": "preferred",
                "amount": "30",
                "currency": "USD",
                "period": "hour",
            }
        ]
        selected = _step_four_summary(confirmed_review(preference_model=model))[
            "preferences"
        ]
        self.assertIn("Employment relationship: Employee", selected)
        self.assertIn("Workload: Full-time and Part-time", selected)
        self.assertIn("Schedule flexibility: Flexible", selected)
        self.assertIn("Phone and voice work: Non-phone / non-voice", selected)
        self.assertIn("Existing job interests: Customer Support", selected)
        self.assertIn("Preferred minimum: USD 30/hour", selected)
        self.assertNotIn("No specific work-condition preferences", selected)

    def test_v1_preferences_use_existing_v2_editor_projection_without_mutation(self):
        legacy = empty_profile_preferences_v1()
        legacy["employment_relationships"] = ["employee"]
        legacy["workloads"] = ["full_time"]
        legacy["schedule"]["time_windows"] = ["business_hours", "weekdays"]
        legacy["job_interests"] = ["customer_support"]
        legacy["compensation"] = {
            "minimum_kind": "preferred",
            "amount": "30",
            "currency": "USD",
            "period": "hour",
        }
        original = deepcopy(legacy)
        compatible = preference_model_for_v2_editor(legacy)

        selected = _step_four_summary(
            confirmed_review(preference_model=legacy)
        )["preferences"]

        self.assertEqual(legacy, original)
        self.assertEqual(compatible["schedule"]["working_days"], ["weekdays"])
        self.assertEqual(
            compatible["schedule"]["time_of_day"], ["business_hours"]
        )
        self.assertIn("Employment relationship: Employee", selected)
        self.assertIn("Workload: Full-time", selected)
        self.assertIn("Days: Weekdays", selected)
        self.assertIn("Time of day: Business hours", selected)
        self.assertIn("Existing job interests: Customer Support", selected)
        self.assertIn("Preferred minimum: USD 30/hour", selected)

    def test_important_details_are_optional_bounded_and_do_not_expose_internal_inputs(self):
        review = confirmed_review()
        empty = _step_four_summary(review)
        self.assertEqual(empty["important"], ())

        values = dict(review.user_inputs)
        values.update(
            {
                "work_authorization": "Authorized to work in Brazil",
                "eligible_countries": "Brazil",
                "hard_constraints": "No overnight shifts " + "x" * 200,
                "accessibility_constraints": "Candidate-private detail",
                "avoid_keywords": "internal exclusion text",
                "excluded_domains": "hidden domain text",
            }
        )
        review = replace(review, user_inputs=tuple(sorted(values.items())))
        important = _step_four_summary(review)["important"]
        self.assertIn("Work authorization: Authorized to work in Brazil", important)
        self.assertIn("Can work in: Brazil", important)
        self.assertIn("Accessibility needs added", important)
        self.assertTrue(any(item.startswith("Firm limits: No overnight shifts") for item in important))
        self.assertTrue(all(len(item) <= 133 for item in important))
        self.assertNotIn("internal exclusion text", repr(important))
        self.assertNotIn("hidden domain text", repr(important))
        self.assertNotIn("Candidate-private detail", repr(important))

    def test_rendering_has_section_level_navigation_and_confirmation_copy(self):
        markup = _render_step_four_summary(confirmed_review())
        self.assertIn("<h3 id='step-four-background-title'>Your background</h3>", markup)
        self.assertIn("href='#review-found'>Edit profile basics</a>", markup)
        self.assertIn("href='#review-suggestions'>Edit skills &amp; experience</a>", markup)
        self.assertIn("href='#review-preferences'>Edit work preferences</a>", markup)
        self.assertEqual(markup.count("step-four-edit-actions"), 2)

    def test_returning_to_step_four_flushes_then_refreshes_server_summary(self):
        self.assertIn(
            "function openStepFour(){setStep('review-finish')",
            _REVIEW_STATE_SCRIPT,
        )
        start = _REVIEW_STATE_SCRIPT.index("function openStepFour()")
        end = _REVIEW_STATE_SCRIPT.index("function rememberSection", start)
        handler = _REVIEW_STATE_SCRIPT[start:end]
        self.assertLess(handler.index("flush()"), handler.index("window.location.reload()"))
        self.assertIn("if(target==='review-finish')", _REVIEW_STATE_SCRIPT)
        self.assertIn(
            "if(window.location.hash==='#review-finish')",
            _REVIEW_STATE_SCRIPT,
        )

    def test_classified_final_save_failure_uses_candidate_facing_copy(self):
        response = _failure("save_unavailable")
        self.assertEqual(response.status, 503)
        self.assertIn(b"We couldn&#x27;t create your profile just now", response.body)
        self.assertIn(b"Your progress is saved. Please try again.", response.body)


if __name__ == "__main__":
    unittest.main()
