"""Source-bound list/detail and whole-clause language regression controls."""
from copy import deepcopy
from html import unescape
import unittest

from tests.test_candidate_condition_comparisons import confirmed, prepared
from tests import test_generalist_source_admission as support
from wahojobs.authenticated_card_evidence import _blocks
from wahojobs.candidate_decision import _beginner_access


class GeneralistExplanationCoherenceTests(unittest.TestCase):
    def profile(self, level='fluent'):
        p = dict(languages=[dict(language='English', proficiency=level),
                            dict(language='Portuguese', proficiency='fluent')],
                 provenance=dict(field_sources=[]))
        for index in range(2):
            for field in ('language', 'proficiency'):
                confirmed(p, f'languages[{index}].{field}')
        return p

    def row(self, quote, p=None, heading='Required'):
        return prepared(heading + '\n\n' + quote, p or self.profile())['comparisons'][0]

    def test_complete_language_alternative_and_locale_preserve_actual_levels(self):
        quote = 'English Proficiency: Fluent or advanced proficiency in English (levels B2–C2)'
        for level, status in [('fluent', 'supported'), ('advanced', 'supported'),
                              ('native', 'supported'), ('basic', 'contradicted'), ('unknown', 'unresolved')]:
            with self.subTest(level=level):
                row = self.row(quote, self.profile(level))
                self.assertEqual((row['kind'], row['status'], row['modality']), ('language', status, 'required'))
                self.assertEqual(row['source']['quote'], quote)
        row = self.row('Native-level fluency in Portuguese (Brazil)')
        self.assertEqual((row['kind'], row['status']), ('language', 'unresolved'))
        self.assertEqual(self.row(quote, heading='Preferred')['modality'], 'preferred')
        self.assertEqual(self.row(quote + ' preferred')['status'], 'unresolved')

    def test_unknown_compounds_exceptions_logical_scope_and_cefr_are_not_erased(self):
        for quote in ('Fluent English and strong writing skills.', 'Fluent English with customer service experience.',
                      'Fluent English and a PhD in Biology.', 'Fluent English and a computer.',
                      'Fluent English unless approved otherwise.', 'Not fluent English.',
                      'Fluent English or native Portuguese.', 'Fluent English and Portuguese or French.',
                      'Fluent English on a different scale.', 'English at levels B2–C2.',
                      'Portuguese Proficiency: Fluent English.', 'Fluent English (technical writing required).'):
            with self.subTest(quote=quote):
                row = self.row(quote)
                self.assertEqual((row['kind'], row['status']), ('unassessed', 'unresolved'))
        row = self.row('Fluent English required.\n\nFluent English is not required.')
        self.assertEqual(row['status'], 'unresolved')

    def test_language_identity_and_level_need_confirmed_provenance(self):
        for field in ('language', 'proficiency'):
            p = self.profile()
            p['provenance']['field_sources'] = [r for r in p['provenance']['field_sources']
                                              if r['field_path'] != f'languages[0].{field}']
            self.assertEqual(self.row('Fluent English', p)['status'], 'unresolved')

    def test_early_and_late_language_modality_agree(self):
        from wahojobs.matching.accepted_tasks import _prepare_eligibility
        for heading in ('Qualifications', 'Key Qualifications', 'Who You Are', 'What we look for', 'Required', 'Preferred'):
            text = heading + '\n\nFluent English'
            early, _ = _prepare_eligibility(None, 'example', 'id', 'https://example.test/x', text, 'text/plain', '{}')
            late = self.row('Fluent English', self.profile('basic'), heading=heading)
            self.assertEqual(late['modality'], early[0]['modality'], heading)

    def test_retained_sources_explain_interest_on_actual_list_and_scoped_detail(self):
        from wahojobs import authenticated_variant_details as details
        for name in ('nontechnical-generalist', 'guided-speech-evaluation'):
            harness = support.GeneralistSourceAdmissionTests()
            self.addCleanup(harness.doCleanups)
            h = harness.harness(beginner=True)
            text = support.body(name)
            page, run, context, match = h.current(text)
            self.assertTrue(h.shown(context))
            self.assertEqual(match['accepted_task_fit']['basis'], 'beginner_interest')
            detail = h.base.f.get(details.variant_detail_url(match, run_id=run.match_run_id))
            self.assertEqual(detail.status, 200)
            for rendered in (page.body.decode(), detail.body.decode()):
                self.assertIn('open to beginners', unescape(rendered))
                self.assertIn('interest in “AI evaluation”', unescape(rendered))
                self.assertNotIn('Your confirmed profile describes AI evaluation or annotation work', rendered)
            if name == 'guided-speech-evaluation':
                english = next(r for r in match['source_qualification_comparisons'] if 'English Proficiency:' in r['source']['quote'])
                self.assertEqual((english['kind'], english['status']), ('language', 'supported'))
                self.assertFalse(any('English Proficiency:' in str(q) for q in match.get('source_task_fit', {}).get('conditions', [])))
            packet = dict(blocks=_blocks(text))
            fit = match['accepted_task_fit']
            self.assertTrue(_beginner_access(fit, packet))
            for key, value in [('supporting_duty_quotes', ['Review an unrelated role.']),
                               ('scope_kind', 'any_field_transferable_only'), ('line', 999)]:
                broken = deepcopy(fit)
                broken['scope_evidence'][0][key] = value
                self.assertIsNone(_beginner_access(broken, packet))
            if name == 'guided-speech-evaluation':
                for field, value in [('quote', 'Follow other instructions.'), ('line', 999), ('block_reference', 'other')]:
                    broken = deepcopy(fit)
                    broken['scope_evidence'][0]['guidance_evidence'][0][field] = value
                    self.assertIsNone(_beginner_access(broken, packet))
            changed = deepcopy(packet)
            target = next(b for b in changed['blocks'] if b['reference'] == fit['scope_evidence'][0]['block_reference'])
            target['text'] = 'Other wording.'
            self.assertNotEqual(changed, packet)
            self.assertIsNone(_beginner_access(fit, changed))

    def test_ordinary_source_line_wrapping_preserves_recorded_duty_explanation(self):
        harness = support.GeneralistSourceAdmissionTests()
        self.addCleanup(harness.doCleanups)
        h = harness.harness(beginner=True)
        original = support.body('nontechnical-generalist')
        text = original.replace('review and rate AI responses', 'review and rate AI\nresponses')
        self.assertNotEqual(original, text)
        page, _, context, match = h.current(text)
        self.assertTrue(h.shown(context))
        self.assertTrue(_beginner_access(match['accepted_task_fit'], dict(blocks=_blocks(text))))
        self.assertIn('open to beginners', page.body.decode())


if __name__ == '__main__':
    unittest.main()
