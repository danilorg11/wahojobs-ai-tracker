"""Source range bounds survive the shared exact-variant card/detail formatter."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests.test_candidate_source_display import detail
from tests.test_authenticated_card_evidence import PROFILE, card
from wahojobs.authenticated_card_evidence import prepare_card_evidence, _source_text
from wahojobs.authenticated_source_detail import prepare_detail_display
from wahojobs.candidate_source_display import pay_facts


class CompensationRangeWordingTests(unittest.TestCase):
    def test_both_captured_variants_keep_six_and_sixty_five_on_card_and_detail(self):
        sources = json.loads((Path(__file__).parent / 'fixtures/language_task_source_examples.json').read_text(encoding='utf-8'))
        for source in sources:
            self.assertIn('$6-to- $65 per hour', _source_text(source))
            a = prepare_card_evidence(card(source), source, PROFILE)
            b = prepare_detail_display(detail(source), PROFILE)
            self.assertEqual(a['pay'], b['pay'])
            self.assertEqual(a['pay']['label'], '$6-to- $65 per hour')
            self.assertEqual(a['pay']['currency_note'], 'Currency not specified in the listing.')

    def test_word_separators_qualifiers_units_and_currencies_are_not_discarded(self):
        for wording in ('$6-to-\u202f$65 per hour', '$6 to $65 per hour',
                        'From EUR 6 to 65 per task', 'up to $65/hour',
                        '$75-90/hr', '6 to 65 per hour', 'GBP 10–20 per project',
                        'From $6 per accepted task'):
            with self.subTest(wording=wording):
                self.assertEqual(pay_facts({'pay': wording}, '')['label'], wording)

    def test_variants_and_conflicting_pay_statements_are_not_merged(self):
        a = pay_facts({}, 'The pay range is $6-to-$65 per hour.')
        b = pay_facts({}, 'From EUR 20 per task.')
        self.assertNotIn('EUR', a['label'])
        self.assertNotIn('65', b['label'])
        conflict = pay_facts({'pay': '$6-to-$65 per hour'}, 'EUR 20 per task')
        self.assertEqual(len(conflict['wording']), 2)
        self.assertIn('The source uses more than one pay description; confirm the applicable rate.', conflict['notes'])


if __name__ == '__main__': unittest.main()
