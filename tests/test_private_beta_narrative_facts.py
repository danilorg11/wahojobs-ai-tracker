"""Manual draft fidelity, including negative and scoped-evidence contrasts."""
import unittest

from scripts import local_product_app as app
from wahojobs.profiles import normalizer


BACKGROUND = (
    'I live in Brazil. My name is Alex. I have two years of customer support experience. '
    'I answer customer questions, review written responses, check information against instructions, '
    'and organize spreadsheet records. I speak Portuguese at native level and English fluently. '
    'I have completed high school and do not have a university degree. '
    'My skills include customer support, data entry, writing, attention to detail and Python. '
    'I want remote AI evaluation, annotation, language review and customer support work. '
    'I am interested in reviewing AI-generated responses. I prefer part-time work.'
)


def draft(text):
    return app.normalize_identity_free_profile_input(text, 'short_paragraph', allow_fallbacks=False).to_mapping()


class PrivateBetaNarrativeFactTests(unittest.TestCase):
    def test_explicit_candidate_facts_survive_as_reviewable_draft(self):
        p = draft(BACKGROUND)
        self.assertEqual(p['identity']['display_name'], 'Alex')
        self.assertEqual(p['location']['country'], 'Brazil')
        self.assertEqual({l['language']: l['proficiency'] for l in p['languages']},
                         {'Portuguese': 'native', 'English': 'fluent'})
        self.assertEqual(p['education']['education_level'], 'high_school')
        self.assertEqual(p['education']['completion_status'], 'completed')
        self.assertIn('no college degree', p['constraints']['hard_constraints'])
        self.assertIsNone(p['experience']['total_years'])
        self.assertEqual(p['experience']['years_by_domain'], {'customer support': 2})
        self.assertEqual(p['experience']['professional_domains'], ['customer service'])
        self.assertEqual(p['experience']['specialties'], ['answer customer questions', 'review written responses',
            'check information against instructions', 'organize spreadsheet records'])
        self.assertFalse(p['experience']['recent_roles'])  # no fabricated employer/job title
        self.assertTrue({'customer support', 'data entry', 'writing', 'attention to detail', 'python',
                         'content review', 'review', 'spreadsheets'} <= set(p['skills']['normalized']))
        self.assertFalse(p['skills']['software_tools'])  # listing Python does not establish its use at work
        self.assertEqual(set(p['preferences']['target_opportunity_types']),
                         {'AI evaluation', 'data annotation', 'language review', 'customer support'})
        self.assertEqual(p['preferences']['employment_types'], ['part-time'])
        self.assertTrue(p['preferences']['remote'])
        self.assertEqual(p['provenance']['original_text'], BACKGROUND)
        self.assertTrue(all(not f['explicit'] for f in p['provenance']['field_sources'].values()))

    def test_language_levels_do_not_bleed_across_languages_or_claims(self):
        cases = (
            ('I speak Portuguese at native level and English fluently.', {'Portuguese': 'native', 'English': 'fluent'}),
            ('I speak Spanish fluently and French at intermediate level.', {'Spanish': 'fluent', 'French': 'intermediate'}),
            ('I speak English. I am a native Portuguese speaker.', {'English': 'unknown', 'Portuguese': 'native'}),
            ('I hope to speak English fluently.', {'English': 'unknown'}),
            ('My friend speaks English fluently.', {'English': 'unknown'}),
            ('I am not fluent in English.', {'English': 'unknown'}),
            ('English fluently, said the applicant.', {'English': 'unknown'}),
            ('Alex speaks English fluently.', {'English': 'unknown'}),
            ('The job requires English at native level.', {'English': 'unknown'}),
            ('Alex, my former manager from the customer service department of the company, speaks English fluently.', {'English': 'unknown'}),
            ('Portuguese native and Alex speaks English fluently.', {'Portuguese': 'unknown', 'English': 'unknown'}),
            ('Languages: native Spanish and advanced English.', {'Spanish': 'native', 'English': 'advanced'}),
            ('I live in Brazil and I am fluent in English.', {'English': 'fluent'}),
            ('Alex speaks English fluently and I am native in Portuguese.', {'English': 'unknown', 'Portuguese': 'native'}),
            ('Alex fluent English and I am native in Portuguese.', {'English': 'unknown', 'Portuguese': 'native'}),
            ('English reading.', {'English': 'reading'}),
            ('Alex has English reading skills.', {'English': 'unknown'}),
            ('I want English reading skills.', {'English': 'unknown'}),
            ('The job requires English reading skills.', {'English': 'unknown'}),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual({l['language']: l['proficiency'] for l in draft(text)['languages']}, expected)
        self.assertEqual(draft('I live in Brazil. I speak Portuguese fluently.')['languages'][0]['locale'], '')
        self.assertEqual(draft('I speak Brazilian Portuguese fluently.')['languages'][0]['locale'], 'Brazil')

    def test_scoped_duration_is_not_total_career_or_unrelated_skill_duration(self):
        p = draft('I have three years of customer service experience. My skills include Python. I have eight years of work experience.')
        self.assertEqual(p['experience']['total_years'], 8)
        self.assertEqual(p['experience']['years_by_domain'], {'customer service': 3})
        self.assertNotIn('python', p['experience']['years_by_domain'])
        p = draft('I have 4 years of experience in biology. I have 2 years of customer support experience.')
        self.assertIsNone(p['experience']['total_years'])
        self.assertEqual(p['experience']['years_by_domain'], {'biology': 4, 'customer support': 2})

    def test_uncertain_duration_is_retained_without_numeric_authority(self):
        for text in ('I want two years of customer support experience.',
                     'My colleague has two years of customer support experience.',
                     'I have at least two years of customer support experience.',
                     'I have two years of customer support and Python experience.',
                     'I worked in customer support for two years.',
                     'I have two years of customer support experience, said the applicant.',
                     'I have two years of customer support experience in the future.',
                     'I have two years of customer support experience. I have five years of customer support experience.'):
            with self.subTest(text=text):
                p = draft(text)
                self.assertIsNone(p['experience']['total_years'])
                self.assertFalse(p['experience']['years_by_domain'])
                self.assertEqual(p['provenance']['original_text'], text)

    def test_support_history_requires_personal_present_or_past_background(self):
        for text in ('My colleague has two years of customer support experience.',
                     'Alex has two years of customer support experience.',
                     'I want two years of customer support experience.',
                     'I will have two years of customer support experience.',
                     'I have two years of customer support experience, said the applicant.',
                     'My skills include customer support.'):
            with self.subTest(text=text):
                self.assertFalse(draft(text)['experience']['professional_domains'])
        for text in ('I have experience in customer support.', 'I worked in customer support.', 'Customer support experience.'):
            self.assertEqual(draft(text)['experience']['professional_domains'], ['customer service'])

    def test_completed_high_school_and_no_university_are_separate_from_missing(self):
        p = draft('I have completed high school. I do not have a university degree.')
        self.assertEqual(p['education']['education_level'], 'high_school')
        self.assertEqual(p['education']['completion_status'], 'completed')
        self.assertIn('no college degree', p['constraints']['hard_constraints'])
        for text in ('I enjoy reading.', 'I want to complete high school.',
                     'My friend completed high school.', 'I have completed high school, said the applicant.'):
            p = draft(text)
            self.assertEqual(p['education']['education_level'], 'not_specified')
            self.assertEqual(p['education']['completion_status'], 'unknown')
            self.assertNotIn('no college degree', p['constraints']['hard_constraints'])

    def test_skills_and_interests_do_not_create_background_or_each_other(self):
        p = draft('My skills include customer support, data entry, attention to detail and Python.')
        self.assertEqual(set(p['skills']['normalized']), {'customer support', 'data entry', 'attention to detail', 'python'})
        self.assertFalse(p['preferences']['target_opportunity_types'])
        self.assertFalse(p['experience']['professional_domains'])
        p = draft('I want remote AI evaluation, data annotation and customer support work.')
        self.assertFalse(p['skills']['normalized'])
        self.assertFalse(p['experience']['professional_domains'])
        self.assertFalse(p['experience']['years_by_domain'])
        for text in ('I want to answer customer questions and organize spreadsheet records.',
                     'They review written responses.', 'I do not review written responses.',
                     'I review written responses, said the applicant.'):
            self.assertFalse(normalizer.detect_personal_work_activities(normalizer.normalize_profile_text(text)))

    def test_activity_lists_preserve_own_wording_without_inventing_jobs(self):
        p = draft('I maintain customer records, write help articles and process support tickets. I review correctness and clarity.')
        self.assertEqual(p['experience']['specialties'], ['maintain customer records', 'write help articles',
            'process support tickets', 'review correctness and clarity'])
        self.assertFalse(p['experience']['recent_roles'])
        self.assertFalse(p['experience']['job_titles'])
        self.assertIsNone(p['experience']['total_years'])
        for text in ('I want to maintain customer records.', 'They maintain customer records.',
                     'I maintain no customer records.', 'I maintain customer records, said the applicant.',
                     'I review correctness, accuracy and clarity.'):
            self.assertFalse(draft(text)['experience']['specialties'])
            self.assertEqual(draft(text)['provenance']['original_text'], text)

    def test_display_identity_cannot_supply_matching_evidence(self):
        from wahojobs.profiles.canonical import canonical_to_matcher_profile
        from scripts import profile_match_digest as matcher
        from wahojobs.matching.fit_evidence import build_profile_fit_evidence
        for name in ('Ruby', 'Rust Lee'):
            with self.subTest(name=name):
                original = app.normalize_identity_free_profile_input('My name is ' + name + '.', 'short_paragraph', allow_fallbacks=False)
                p = original.to_mapping()
                self.assertEqual(p['identity']['display_name'], name)
                self.assertFalse(p['skills']['normalized'])
                fields = app.profile_review_form_fields(original, 'synthetic-run', 'synthetic-review')
                updates = app.profile_review_updates_from_form({k: [v] for k, v in fields.items()}, app.profile_review_language_slots(p))
                reviewed = app.apply_identity_free_profile_review(original, updates)
                for projection in (app._identity_free_matcher_projection(p), canonical_to_matcher_profile(reviewed.bind_durable_profile_id('prf_0123456789abcdef0123456789abcdef'))):
                    self.assertEqual(projection['display_name'], name)
                    self.assertNotIn(name.casefold().split()[0], matcher.profile_match_text(projection))
                    self.assertFalse(matcher.detect_profile_match_features(projection)['professional_domains'])
                    evidence = build_profile_fit_evidence(projection)
                    self.assertNotIn(name.casefold().split()[0], str(evidence))

    def test_explicit_name_requires_a_personal_unambiguous_introduction(self):
        self.assertEqual(normalizer.detect_declared_name('My name is Sam Lee. I live in Brazil.'), 'Sam Lee')
        for text in ('My colleague is Alex.', 'My name is not Alex.', 'My name is Alex, said the applicant.',
                     'My name is Alex. My name is Sam.', 'I want my name to be Alex.'):
            self.assertEqual(normalizer.detect_declared_name(text), '')

    def test_review_roundtrip_preserves_scoped_years_education_and_negative_facts(self):
        from wahojobs.profiles.canonical_v2 import convert_v1_to_v2, project_v2_to_matcher_v1
        from wahojobs.professional_background_duration import compare_duration
        original = app.normalize_identity_free_profile_input(BACKGROUND, 'short_paragraph', allow_fallbacks=False)
        fields = app.profile_review_form_fields(original, 'synthetic-run', 'synthetic-review')
        form = {key: [value] for key, value in fields.items()}
        updates = app.profile_review_updates_from_form(form, app.profile_review_language_slots(original.to_mapping()))
        reviewed = app.apply_identity_free_profile_review(original, updates)
        p = reviewed.to_mapping()
        self.assertIsNone(p['experience']['total_years'])
        self.assertEqual(p['experience']['years_by_domain'], {'customer support': 2})
        self.assertEqual(p['education']['education_level'], 'high_school')
        self.assertEqual(p['education']['completion_status'], 'completed')
        self.assertIn('no college degree', p['constraints']['hard_constraints'])
        self.assertEqual(p['identity']['display_name'], 'Alex')
        self.assertEqual(p['experience']['specialties'], original.to_mapping()['experience']['specialties'])
        pid = 'prf_0123456789abcdef0123456789abcdef'
        v2 = convert_v1_to_v2(reviewed.bind_durable_profile_id(pid), persistent_profile_id=pid,
                             source_ordinal_resolver=lambda *_: (1,))
        self.assertEqual(v2['experience']['years_by_domain'], [{'domain': 'customer support', 'years': 2}])
        comparison = compare_duration('5 years of relevant professional experience in customer support', v2)
        self.assertEqual(comparison['status'], 'contradicted')
        self.assertEqual(compare_duration('5 years of relevant professional experience in Python', v2)['status'], 'unresolved')
        projected = project_v2_to_matcher_v1(v2, matcher_profile_id='synthetic-review')
        self.assertFalse(projected['experience']['years_by_domain'])
        self.assertIsNone(projected['experience']['total_years'])
