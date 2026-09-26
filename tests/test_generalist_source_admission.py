"""Retained public sources + synthetic profiles; no owner data or ID rules."""
from copy import deepcopy
from pathlib import Path
import unittest

from tests import test_beginner_access_policy as beginner_support
from tests import test_transferable_task_matching as transferable_support
from wahojobs.authenticated_card_evidence import _blocks
from wahojobs.candidate_condition_comparisons import _condition_lines, _lines, _modality
from wahojobs.matching.accepted_tasks import _prepare, _prepare_eligibility
from wahojobs.matching.beginner_access import source_scope as beginner_scope
from wahojobs.matching.languages import compare_language_condition, prepare_language_conditions
from wahojobs.matching.transferable_tasks import activity_families, source_scope

FIXTURES = Path(__file__).parent / 'fixtures/generalist_admission'


def body(name):
    return (FIXTURES / (name + '.txt')).read_text(encoding='utf-8')


def source(text):
    return dict(body=text, body_format='text/plain', source_slug='different-provider',
                external_id='different-posting', url='https://example.test/another-role', metadata_json='{}')


class GeneralistSourceAdmissionTests(unittest.TestCase):
    def harness(self, beginner=False):
        h = beginner_support.BeginnerAccessPolicyTests() if beginner else transferable_support.TransferableTaskMatchingTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        return h

    def test_retained_any_field_scope_requires_actual_transferable_activities(self):
        text = body('any-field-generalist').replace('Mercor', 'Example Network')
        self.assertEqual(source_scope(source(text))[0]['scope_kind'], 'any_field_transferable_only')
        self.assertEqual(beginner_scope(source(text)), [])
        h = self.harness()
        saved = deepcopy(h.base.f.profile)
        _, run, context, match = h.current(text)
        self.assertTrue(h.shown(context))
        self.assertEqual(match['accepted_task_fit']['basis'], 'transferable_activity')
        self.assertTrue(match['conditional_task_fit'])  # reading/background/writing still assessed separately
        self.assertEqual(h.base.f.profile, saved)
        self.assertFalse(match.get('confirmed_ai_work_evidence'))
        self.assertTrue(any('Grading model output' in f['quote'] for f in match['accepted_task_fit']['facts']))
        h.install(['organize spreadsheet records'])
        _, _, context, match = h.current(text)
        self.assertFalse(h.shown(context))
        h = self.harness(beginner=True)
        _, _, context, match = h.current(text)
        self.assertFalse(h.shown(context))  # interest is not background/review history
        self.assertIsNone(match['accepted_task_fit'])

    def test_retained_nontechnical_duties_use_accepted_beginner_interest_route(self):
        text = body('nontechnical-generalist').replace('DataAnnotation', 'Example Platform')
        h = self.harness(beginner=True)
        _, run, context, match = h.current(text)
        self.assertTrue(h.shown(context))
        self.assertEqual(match['accepted_task_fit']['basis'], 'beginner_interest')
        self.assertTrue(match['conditional_task_fit'])
        self.assertEqual(h.detail(run, match)['accepted_task_fit'], match['accepted_task_fit'])
        self.assertTrue(any('Rank and rate outputs' in f['quote'] for f in match['accepted_task_fit']['facts']))
        self.assertTrue(any('written English' in c['source']['quote'] for c in match['source_qualification_comparisons']))
        h.install(interests=('customer support',))
        _, _, context, match = h.current(text)
        self.assertFalse(h.shown(context))

    def test_source_bound_transfer_is_not_generic_title_only_relevance(self):
        h = self.harness()
        h.base.role('Generalist Expert', 'Remote')
        h.base.f.update_inventory("UPDATE jobs SET department='Miscellaneous',expertise='Miscellaneous',commitment='part-time'")
        h.base.f.update_inventory("UPDATE canonical_opportunities SET source_category='Miscellaneous'")
        text = body('any-field-generalist')
        _, _, context, match = h.current(text)
        self.assertEqual(match['accepted_task_fit']['basis'], 'transferable_activity')
        self.assertEqual(match['score_components']['quality_gate_penalty'], 0)
        self.assertTrue(h.shown(context))
        self.assertTrue(match['conditional_task_fit'])
        self.assertFalse(match.get('confirmed_ai_work_evidence'))
        for activities in ([], ['organize spreadsheet records'], ['Interested in reviewing written responses']):
            with self.subTest(activities=activities):
                h.install(activities)
                _, _, context, match = h.current(text)
                self.assertIsNone(match['accepted_task_fit'])
                self.assertEqual(match['score_components']['quality_gate_penalty'], 10)
                self.assertFalse(h.shown(context))

        # Removing review provenance must not qualify an otherwise related task.
        c = h.install(['review written responses'])
        c['provenance']['field_sources']['experience.specialties[0]']['explicit'] = False
        from tests.test_confirmed_activity_matching import v2
        h.base.f.profile = v2(c)
        _, _, context, match = h.current(text)
        self.assertIsNone(match['accepted_task_fit'])
        self.assertFalse(h.shown(context))

        h.install(['review written responses'])
        h.base.role('Software Engineering Expert', 'Remote')
        _, _, context, match = h.current(text)
        self.assertEqual(match['accepted_task_fit']['basis'], 'transferable_activity')
        self.assertGreaterEqual(match['score_components']['quality_gate_penalty'], 28)
        self.assertFalse(h.shown(context))

    def test_retained_speech_conditions_separate_required_languages_from_preferred_experience(self):
        text = body('guided-speech-evaluation')
        langs, _ = _prepare_eligibility(None, 'another-source', 'other-id', 'https://example.test/x', text, 'text/plain', '{}')
        self.assertEqual([(x['languages'], x['levels'], x['modality']) for x in langs],
                         [(['portuguese'], ['native'], 'required'), (['english'], ['fluent', 'advanced'], 'required')])
        qualifications = next(b for b in _blocks(text) if 'looking for' in b['heading'])
        rows = list(_condition_lines(qualifications))
        self.assertEqual(len(rows), 3)
        self.assertEqual(_modality(qualifications['heading'], rows[2][1]), 'preferred')
        self.assertNotIn('Preferred', langs[0]['quote'])
        self.assertTrue(any('Are you a student' in x['quote'] for x in source_scope(source(text))))

    def test_speech_interest_keeps_unknown_language_conditional_and_basic_conflict_excluded(self):
        h = self.harness(beginner=True)
        h.base.role('Speech AI Evaluation Portuguese Brazil', 'Remote - Brazil')
        text = body('guided-speech-evaluation')
        _, _, context, match = h.current(text)
        self.assertEqual(match['score'], 17)  # unchanged scoring; existing task-supported route
        self.assertTrue(h.shown(context))
        self.assertTrue(match['conditional_task_fit'])
        self.assertEqual(match['source_language_checks'][0]['status'], 'unresolved')
        h.install()
        from tests.test_confirmed_activity_matching import v2
        from wahojobs.profiles.canonical import field_sources_for_profile
        c = h.install()
        for entry in c['languages']:
            if entry['language'] == 'Portuguese':
                entry['proficiency'] = 'basic'
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        h.base.f.profile = v2(c)
        _, _, context, match = h.current(text)
        self.assertFalse(h.shown(context))
        self.assertEqual(match['source_language_checks'][0]['status'], 'contradicted')

    def test_written_review_does_not_supply_spoken_task_competence(self):
        h = self.harness()
        _, _, context, match = h.current(body('guided-speech-evaluation'))
        self.assertIsNone(match['accepted_task_fit'])
        self.assertFalse(h.shown(context))

    def test_positive_scope_never_waives_separate_central_prerequisites(self):
        for name in ('any-field-generalist', 'nontechnical-generalist', 'guided-speech-evaluation'):
            for restriction in ('PhD in Biology required.', 'Prior AI evaluation experience required.',
                                'Experience with Python required.', 'For licensed physicians only.',
                                'Important: for experienced reviewers only.'):
                with self.subTest(name=name, restriction=restriction):
                    text = body(name)
                    addition = '\n\nRequirements\n\n' + restriction + '\n\n'
                    text = text.replace('Similar roles', addition + 'Similar roles') if 'Similar roles' in text else text + addition
                    self.assertEqual(source_scope(source(text)), [])

    def test_complete_paragraph_and_unknown_labeled_restrictions_stay_attached(self):
        for separator in (' ', '\n', '\n\n'):
            text = 'This is an entry-level role.' + separator + 'Important: for experienced reviewers only.'
            self.assertEqual(source_scope(source(text)), [])
        self.assertEqual(list(_lines(dict(text='This is an entry-level role.\nImportant: for experienced reviewers only.'))),
                         [(1, 'This is an entry-level role. Important: for experienced reviewers only.')])
        self.assertEqual(list(_condition_lines(dict(text='- Native-level fluency in\n  Portuguese (Brazil)\nEnglish Proficiency: Fluent English'))),
                         [(1, 'Native-level fluency in Portuguese (Brazil)'), (3, 'English Proficiency: Fluent English')])

    def test_waivers_and_other_role_text_alone_do_not_establish_accessible_scope(self):
        for waiver in ('No degree required.', 'No prior AI experience required.', 'No technical background required.'):
            self.assertEqual(source_scope(source('Responsibilities\n\nEvaluate AI responses.\n\nRequirements\n\n'+waiver)), [])
        text = body('nontechnical-generalist')
        for changed in (text.replace('As a Generalist', 'In another role, as a Generalist'),
                        text.replace('No technical background needed', 'Technical background required')):
            self.assertEqual(source_scope(source(changed)), [])

    def test_paired_scope_needs_affirmative_current_role_evidence_on_both_sides(self):
        for duty in ('Review AI responses, not everyday topics.', 'Review AI responses, not on everyday topics.',
                     'In another role: review AI responses on everyday topics.'):
            text = 'Responsibilities\n\n' + duty + '\n\nQualifications\n\nNo technical background required.'
            self.assertEqual(beginner_scope(source(text)), [])
        text = 'Responsibilities\n\nReview AI responses on everyday topics.\n\nSimilar roles\n\nNo technical background required.'
        self.assertEqual(beginner_scope(source(text)), [])
        text = body('guided-speech-evaluation')
        for replacement in ('Do not Follow provided scenarios and prompts during interactions.',
                            'In another role: Follow provided scenarios and prompts during interactions.'):
            self.assertEqual(beginner_scope(source(text.replace(
                'Follow provided scenarios and prompts during interactions.', replacement))), [])

    def test_equivalent_source_actions_keep_objects_and_qualifications_separate(self):
        for text in ('Evaluate AI responses.', 'Review and rate AI responses.', 'Grading model output on general reasoning.'):
            self.assertIn('written_content_review', activity_families(text, source=True))
        for text in ('Grade student essays.', 'Check the printer, then send information.',
                     'Do not review AI responses.', 'Interested in evaluating written responses.'):
            self.assertEqual(activity_families(text), ())
        text = "What you'll actually do\n\nReview AI responses.\n\nWhat we look for\n\nExperience grading AI outputs."
        facts = _prepare(None, 'different-provider', 'different-id', 'https://example.test/x', text, 'text/plain', '{}')
        self.assertEqual([q for q, _, _ in facts], ['Review AI responses.'])

    def test_related_roles_supply_neither_positive_duties_nor_speech_modality(self):
        invitation = 'Are you a student, recent graduate, stay-at-home parent, gig worker, or professional seeking flexible remote work?'
        text = invitation + '\n\nResponsibilities\n\nFollow provided scenarios and prompts during interactions.\n\nSimilar roles\n\nResponsibilities\n\nReview AI responses.'
        self.assertEqual(beginner_scope(source(text)), [])
        current = 'Responsibilities\n\nReview AI responses on everyday topics.\n\nQualifications\n\nNo technical background required.'
        scopes = source_scope(source(current + '\n\nSimilar roles\n\nSpeech evaluation of spoken responses.'))
        self.assertTrue(scopes)
        self.assertFalse(any(p.get('task_modality') == 'spoken' for p in scopes))
        facts = _prepare(None, 'source', 'id', 'https://example.test/x', text, 'text/plain', '{}')
        self.assertEqual(facts, ())

    def test_explicit_level_alternatives_do_not_infer_cefr_or_native_proficiency(self):
        quote = 'English Proficiency: Fluent or advanced proficiency in English (levels B2–C2)'
        required = prepare_language_conditions(quote, 'required')[0]
        for level, expected in (('fluent', 'supported'), ('advanced', 'supported'), ('native', 'supported'),
                                ('basic', 'contradicted'), ('unknown', 'unresolved')):
            result = compare_language_condition({'language_proficiency': {'English': level}}, required)
            self.assertEqual(result['status'], expected)
        self.assertEqual(prepare_language_conditions('English at levels B2–C2', 'required'), [])
        native = prepare_language_conditions('Native Portuguese required', 'required')[0]
        self.assertEqual(compare_language_condition({'language_proficiency': {'Portuguese': 'fluent'}}, native)['status'], 'unresolved')
        uncertain = prepare_language_conditions('Fluent or advanced English unless approved otherwise', 'required')[0]
        self.assertEqual(uncertain['modality'], 'unresolved')


if __name__ == '__main__':
    unittest.main()
