import unittest
from scripts.local_product_app import normalize_identity_free_profile_input
from wahojobs.profiles.normalizer import normalize_profile_text,detect_skills,detect_domains


class PrivateBetaManualSkillTests(unittest.TestCase):
    def test_personal_skill_list_is_kept_as_unconfirmed_skill_without_professional_authority(self):
        text='My skills include writing, review, online research and Python.'
        profile=normalize_identity_free_profile_input(text,'short_paragraph',allow_fallbacks=False).to_mapping()
        self.assertEqual(set(profile['skills']['normalized']),{'writing','review','online research','python'})
        self.assertEqual(profile['experience']['professional_domains'],[])
        self.assertEqual(profile['experience']['recent_roles'],[])
        self.assertIsNone(profile['experience']['total_years'])
        self.assertEqual(profile['experience']['years_by_domain'],{})
        self.assertTrue(all(r.get('explicit') is False for r in profile['provenance']['field_sources'].values()))

    def test_interest_negation_other_people_and_future_lists_do_not_create_skills(self):
        for text in ('I want my skills to include Python and writing.',
                     'My skills do not include Python or writing.',
                     'Their skills include Python and writing.',
                     'My skills include no Python or writing.',
                     'I hope to have skills in Python and writing.',
                     'I am learning Python and writing.',
                     'My skills are not Python or writing.',
                     'My skills include writing but not Python.',
                     'My skills are going to include Python and writing.',
                     'My skills include Python and writing only in the future.',
                     'My skills include Python and writing, said the applicant.',
                     'My skills are neither Python nor writing.',
                     'My skills include everything except Python and writing.',
                     'My skills are expected to include Python and writing.'):
            with self.subTest(text=text):
                normalized=normalize_profile_text(text)
                self.assertEqual(detect_skills(normalized,[],input_style='short_paragraph',allow_fallbacks=False),[])
                self.assertEqual(detect_domains(normalized,allow_fallbacks=False),[])

    def test_explicit_skill_does_not_satisfy_required_tool_experience(self):
        from tests.test_candidate_condition_comparisons import prepared,profile
        p=profile();p['skills']['normalized']=['Python'];p['skills']['software_tools']=[]
        p['experience']={'item_details': []}
        packet=prepared('## Requirements\nExperience with Python',p)
        self.assertNotEqual(packet['comparisons'][0]['status'],'supported')
