import unittest

import scripts.profile_to_matches_preview as preview
from wahojobs.matching.specializations import (
    evaluate_specialization_requirements,
    specialization_requirements,
)


def profile(*, skills=(), domains=(), languages=(), summary=""):
    return {
        "skills": list(skills),
        "degrees_or_domains": list(domains),
        "languages": list(languages),
        "summary": summary,
    }


def projected(title, candidate_profile, section="do_these_first"):
    profile_languages = preview.matcher.profile_language_set(candidate_profile)
    matched_languages = preview.supported_profile_languages_in_title(
        preview.normalize_text(title),
        profile_languages,
    )
    row = {
        "title": title,
        "canonical_title": title,
        "source_category": "",
        "department": "",
        "expertise": "",
        "description": "",
        "location": "Remote",
    }
    match = {
        "display_title": title,
        "effective_product_section": section,
        "eligible_for_personalized": True,
        "unsupported_languages": [],
        "detected_languages": matched_languages,
        "matched_languages": matched_languages,
        "language_requirement_mode": "single" if len(matched_languages) == 1 else "ambiguous",
    }
    return preview.apply_preview_guardrails(candidate_profile, row, match)


class PreviewSpecializationActionabilityTests(unittest.TestCase):
    def test_python_profile_keeps_python_and_caps_java(self):
        candidate_profile = profile(skills=("Python",), domains=("software engineering",))

        self.assertEqual(projected("Python Specialist", candidate_profile)["preview_section"], "do_these_first")
        java = projected("Java Coding Specialist", candidate_profile)
        self.assertEqual(java["preview_section"], "also_worth_reviewing")
        self.assertIn("Java", preview.user_caution_note(java))

    def test_javascript_does_not_satisfy_java(self):
        result = evaluate_specialization_requirements(
            "Java Specialist",
            profile(skills=("JavaScript",)),
        )
        self.assertEqual([group["label"] for group in result["missing_groups"]], ["Java"])

    def test_all_of_requirements_need_every_group(self):
        python_only = projected("3D Modeling & Python Specialist", profile(skills=("Python",)))
        both = projected(
            "3D Modeling & Python Specialist",
            profile(skills=("Python", "3D modeling")),
        )

        self.assertEqual(python_only["preview_section"], "also_worth_reviewing")
        self.assertEqual(
            [group["label"] for group in python_only["missing_specialization_groups"]],
            ["3D Modeling"],
        )
        self.assertEqual(both["preview_section"], "do_these_first")

    def test_any_of_slash_and_or_groups_accept_one_alternative(self):
        slash = specialization_requirements("SWE Infrastructure Specialist (JS/TS/Python)")
        either = specialization_requirements("Java or Kotlin Developer")

        self.assertEqual(len(slash), 1)
        self.assertEqual(slash[0]["mode"], "any_of")
        self.assertEqual(len(either), 1)
        self.assertEqual(either[0]["mode"], "any_of")
        self.assertEqual(
            projected(
                "SWE Infrastructure Specialist (JS/TS/Python)",
                profile(skills=("TypeScript", "Python")),
            )["preview_section"],
            "do_these_first",
        )

    def test_kotlin_and_c_sharp_are_capped_but_generic_engineers_are_not(self):
        candidate_profile = profile(
            skills=("TypeScript", "Python", "React"),
            domains=("software engineering",),
        )
        for title in ("Kotlin Coding Specialist", "C# Developer"):
            with self.subTest(title=title):
                self.assertEqual(projected(title, candidate_profile)["preview_section"], "also_worth_reviewing")
        for title in ("Backend Engineer", "Frontend Engineer", "Coding Expert", "Software Engineer"):
            with self.subTest(title=title):
                self.assertEqual(projected(title, candidate_profile)["preview_section"], "do_these_first")

    def test_programming_tokens_remain_distinct(self):
        requirements = {
            title: specialization_requirements(title)[0]["concepts"]
            for title in (
                "Java Developer",
                "JavaScript Developer",
                "C Developer",
                "C# Developer",
                "C++ Developer",
                "R Developer",
                "Go Developer",
            )
        }
        self.assertEqual(requirements["Java Developer"], ["java"])
        self.assertEqual(requirements["JavaScript Developer"], ["javascript"])
        self.assertEqual(requirements["C Developer"], ["c"])
        self.assertEqual(requirements["C# Developer"], ["c_sharp"])
        self.assertEqual(requirements["C++ Developer"], ["c_plus_plus"])
        self.assertEqual(requirements["R Developer"], ["r"])
        self.assertEqual(requirements["Go Developer"], ["go"])

    def test_ambiguous_go_and_r_words_need_programming_context_in_raw_profile(self):
        unsupported_go = evaluate_specialization_requirements(
            "Go Developer",
            profile(summary="I am ready to go into remote software work."),
        )
        supported_go = evaluate_specialization_requirements(
            "Go Developer",
            profile(summary="I have professional Go programming experience."),
        )
        unsupported_r = evaluate_specialization_requirements(
            "R Developer",
            profile(summary="I work in research and development."),
        )

        self.assertTrue(unsupported_go["missing_groups"])
        self.assertFalse(supported_go["missing_groups"])
        self.assertTrue(unsupported_r["missing_groups"])

    def test_biology_roles_distinguish_exact_and_adjacent_specialties(self):
        biology = profile(domains=("biology", "microbiology"), skills=("research",))
        for title in ("Biology Expert", "Biology Research Scientist"):
            with self.subTest(title=title):
                self.assertEqual(projected(title, biology)["preview_section"], "do_these_first")
        for title in (
            "Drug Discovery Specialist",
            "Environmental Science Specialist",
            "Radiological Health",
            "Medical Specialist",
            "Biologist with Python Experience",
        ):
            with self.subTest(title=title):
                self.assertEqual(projected(title, biology)["preview_section"], "also_worth_reviewing")

    def test_biology_and_python_remains_eligible_when_both_are_declared(self):
        match = projected(
            "Biology & Python Expert",
            profile(domains=("biology",), skills=("Python",)),
        )
        self.assertEqual(match["preview_section"], "do_these_first")

    def test_language_roles_distinguish_generic_work_from_professional_audio(self):
        language_profile = profile(domains=("language",), skills=("English", "Spanish"))
        for title in ("English Language Expert", "Spanish Language Data Contributor", "Spanish Audio Evaluator"):
            with self.subTest(title=title):
                self.assertEqual(projected(title, language_profile)["preview_section"], "do_these_first")
        for title in ("Spanish Voice Actor", "English Voice Coach", "Audio Engineer"):
            with self.subTest(title=title):
                match = projected(title, language_profile)
                self.assertEqual(match["preview_section"], "also_worth_reviewing")
                self.assertIn("not listed in your profile", preview.user_caution_note(match))

    def test_negated_raw_specialization_does_not_satisfy_requirement(self):
        match = projected(
            "Java Coding Specialist",
            profile(domains=("software engineering",), summary="I have no experience in Java."),
        )
        self.assertEqual(match["preview_section"], "also_worth_reviewing")

    def test_cap_is_monotonic_and_preserves_stronger_sections(self):
        candidate_profile = profile(skills=("Python",), domains=("software engineering",))
        for section in ("also_worth_reviewing", "explore_only", "excluded"):
            with self.subTest(section=section):
                self.assertEqual(
                    projected("Java Specialist", candidate_profile, section=section)["preview_section"],
                    section,
                )

    def test_specialization_caution_blocks_safe_fallback(self):
        candidate_profile = profile(
            skills=("English", "Spanish"),
            domains=("language",),
            languages=("English", "Spanish"),
        )
        specialized = projected("Spanish Voice Actor", candidate_profile, section="best_matches")
        specialized.update(
            {
                "source": "Fixture",
                "title": "Spanish Voice Actor",
                "url": "https://example.test/voice",
                "score": 24,
            }
        )
        generic = projected("English Language Data Contributor", candidate_profile, section="best_matches")
        generic.update(
            {
                "source": "Fixture",
                "title": "English Language Data Contributor",
                "url": "https://example.test/data",
                "score": 24,
            }
        )

        promoted = preview.ensure_safe_do_these_first([specialized, generic], candidate_profile)
        sections = {match["display_title"]: match["preview_section"] for match in promoted}
        self.assertEqual(sections["Spanish Voice Actor"], "also_worth_reviewing")
        self.assertEqual(sections["English Language Data Contributor"], "do_these_first")


if __name__ == "__main__":
    unittest.main()
