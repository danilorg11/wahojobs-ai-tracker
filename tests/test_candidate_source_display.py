"""Presentation tests. Synthetic edge wording is not live-source validation."""
from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
import json
import unittest

from tests.test_authenticated_card_evidence import SOURCES, PROFILE, card
from tests.test_provider_detail_recovery import CASES, candidate, response
from wahojobs.authenticated_card_evidence import prepare_card_evidence, render_card_evidence
from wahojobs.authenticated_source_detail import prepare_detail_display, render_authenticated_job_page
from wahojobs.candidate_source_display import markdown, pay_facts
from wahojobs.crawler.provider_details import recover_detail, DETAIL_KEY


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.tags = []; self.feed(text)
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def detail(source, match=None):
    return dict(job_id=source['job_id'], canonical_opportunity_id=source['canonical_opportunity_id'],
                external_id=source['external_id'], official_url=source['url'], company_slug=source['source_slug'],
                source_commitment=source.get('commitment'), source_location=source.get('location'),
                rich_external_id=source['content_external_id'], rich_source_url=source['source_url'],
                rich_provider=source['source_slug'], rich_body=source['body'], rich_body_format=source['body_format'],
                rich_metadata_json=source['metadata_json'], last_captured_at=source['last_captured_at'],
                material_content_sha256=source['material_content_sha256'], public_state='live',
                _authenticated_local_checks={'match': match or card(source), 'passes': True},
                _authenticated_recommendation=None, _authenticated_membership_known=False,
                source_title='Example role', company_name='Example company', job_is_active=1, canonical_is_active=1)


class CandidateSourceDisplayTests(unittest.TestCase):
    def test_workload_qualifiers_alternatives_and_negation_are_not_lost(self):
        for wording in ('Up to 20 hours per week.', 'Not 40 hours per week.',
                        '10 hours per week OR a minimum weekly submission.'):
            source = dict(deepcopy(SOURCES[11242]), body='**Engagement**\n' + wording)
            packet = prepare_card_evidence(card(source), source, PROFILE)
            self.assertEqual(dict(packet['facts'])['Workload'], wording)

    def test_pay_qualifiers_units_symbols_and_missing_currency_remain_literal(self):
        for wording in ('$75-90/hr', 'up to $90/hr', 'From EUR 50 per task',
                        'GBP 800 per project', '$35+ per hour', '90 per hour'):
            with self.subTest(wording=wording):
                pay = pay_facts({'pay': wording}, '')
                self.assertEqual(pay['label'], wording)
        self.assertEqual(pay_facts({}, 'Competitive pay!')['label'], '')
        self.assertEqual(pay_facts({}, 'What is your expected hourly rate in USD?')['label'], '')
        self.assertEqual(pay_facts({}, 'You are not paid per accepted task.')['label'], '')
        self.assertEqual(pay_facts({}, 'Compensation is output-based.')['label'], 'Output-based pay')

    def test_upper_bound_teaser_does_not_replace_source_range_or_become_conflict(self):
        pay = pay_facts({'pay': '$75-90/hr', DETAIL_KEY: {'record': {
            'shortDescription': 'Help build AI, fully remote, and up to $90/hr.'}}}, '')
        self.assertEqual(pay['label'], '$75-90/hr')
        self.assertEqual(pay['wording'], ['$75-90/hr', 'up to $90/hr'])
        self.assertEqual(pay['notes'], [])
        self.assertEqual(pay['currency_note'], 'Currency not specified in the listing.')
        self.assertNotIn('Help build', str(pay))

    def test_explicit_iso_dollar_currency_stays_attached_to_its_rate(self):
        for code in ('NZD', 'SGD', 'CAD', 'AUD', 'USD'):
            pay = pay_facts({'pay': code + ' $75 per hour'}, '')
            self.assertEqual(pay['label'], code + ' $75 per hour')
            self.assertEqual(pay['currency_note'], '')

    def test_different_rate_currency_does_not_denominate_dollar_amount(self):
        pay = pay_facts({'pay': '$6-to-$65 per hour'}, 'EUR 20 per task')
        self.assertEqual(pay['label'], '$6-to-$65 per hour')
        self.assertEqual(pay['currency_note'], 'Currency not specified in the listing.')
        self.assertTrue(pay['notes'])

    def test_typed_salary_currency_is_shown_without_unspecified_note(self):
        from wahojobs.crawler.provider_details import DETAIL_KEY
        pay = pay_facts({DETAIL_KEY: {'record': {'salaryType': 'HOURLY', 'lowerBoundHourlyRate': 20,
            'upperBoundHourlyRate': 30, 'salaryCurrency': 'USD'}}}, '')
        self.assertIn('USD', pay['label'])
        self.assertNotIn('currency not specified', pay['label'])
        self.assertEqual(pay['currency_note'], '')

    def test_dollar_symbol_does_not_establish_a_currency_denomination(self):
        self.assertEqual(pay_facts({'pay': '$75-90/hr'}, '')['currency_note'], 'Currency not specified in the listing.')
        self.assertEqual(pay_facts({'pay': '90 per hour'}, '')['currency_note'], 'Currency not specified in the listing.')
        for wording in ('USD 75-90/hr', 'CAD 75-90/hr', '75-90 USD/hr', 'EUR 50 per task'):
            self.assertNotIn('Confirm which dollar currency applies.', pay_facts({'pay': wording}, '')['notes'])

    def test_typed_hourly_bounds_need_no_teaser_but_do_not_invent_currency(self):
        for lo, hi, expected in ((35, 55, '35–55'), (35, None, 'From 35'), (None, 55, 'Up to 55')):
            record = {'salaryType': 'HOURLY', 'lowerBoundHourlyRate': lo, 'upperBoundHourlyRate': hi}
            pay = pay_facts({DETAIL_KEY: {'record': record}}, '')
            self.assertEqual(pay['label'], expected + ' per hour (currency not specified)')
            record['salaryType'] = None
            self.assertEqual(pay_facts({DETAIL_KEY: {'record': record}}, '')['label'], '')

    def test_workload_question_is_not_displayed_as_a_settled_term(self):
        wording = 'Are you able to commit 10–15 hours+ per week? OR a minimum weekly submission'
        source = dict(deepcopy(SOURCES[11242]), body='**Screening questions**\n' + wording)
        packet = prepare_card_evidence(card(source), source, PROFILE)
        self.assertEqual(dict(packet['facts'])['Workload'], 'Weekly commitment to confirm')
        self.assertIn(wording, unescape(render_card_evidence(packet, 'match-1')))

    def test_return_anchor_and_qualification_jump_preserve_context_without_membership_claim(self):
        source = deepcopy(SOURCES[11242])
        job = detail(source)
        html = render_authenticated_job_page(job, profile=PROFILE, navigation='', return_run_id='valid-run')
        self.assertIn("href='/find-matches?run=valid-run#opportunity-11242'", html)
        self.assertIn("<details class='employer-description'>", html)
        self.assertIn('<summary>Employer description and requirements</summary>', html)
        self.assertIn("id='employer-qualifications'", html)
        self.assertLess(html.index('Before you apply'), html.index('View source listing</a>'))
        self.assertNotIn('Apply on company site</a>', html)
        without_run = render_authenticated_job_page(job, profile=PROFILE, navigation='')
        self.assertIn("href='/find-matches#opportunity-11242'", without_run)

    def test_contradictory_qualifiers_and_task_hourly_terms_are_not_silently_resolved(self):
        pay = pay_facts({'pay': '$35-55/hr'}, 'Earn up to $35+/hour.')
        self.assertTrue(pay['notes']); self.assertIn('up to $35+/hour', pay['wording'])
        pay = pay_facts({DETAIL_KEY: {'record': {'ideal_hourly_rate': {'min':80, 'max':90}}}},
                        'Compensation is output-based; experts are paid per task that meets specifications.')
        self.assertEqual(pay['label'], 'Per accepted task')
        self.assertIn('80–90 per hour (currency not specified)', pay['wording'])
        self.assertTrue(pay['notes']); self.assertNotIn('USD', str(pay))

    def test_markdown_headings_paragraphs_nested_lists_and_qualification_meaning(self):
        text = '# Role\n\nAn overview.\n\n**Required**\n\n- PhD OR equivalent.\n  - No license required.\n- **Preferred**: R.\n\n1. Apply\n2. Interview'
        html = markdown(text)
        self.assertIn('<h3>Required</h3>', html)
        self.assertIn('<ul><li>PhD OR equivalent.<ul><li>No license required.', html)
        self.assertIn('<strong>Preferred</strong>: R.', html)
        self.assertIn('<ol><li>Apply</li><li>Interview</li></ol>', html)
        self.assertIn('<p>An overview.</p>', html)

    def test_markdown_never_executes_source_html_links_images_or_scripts(self):
        html = markdown('# X\n<script>alert(1)</script>\n\n<img src="https://remote.test/pixel">\n\n[x](javascript:alert(1))\n\n![photo](https://remote.test/pixel)')
        tags = Tags(html).tags
        self.assertFalse(any(tag in {'a','script','img','iframe','style'} for tag, _ in tags))
        self.assertIn('&lt;script&gt;', html)
        self.assertIn("href='https://example.test/job?a=1&amp;b=2'", markdown('[Source](https://example.test/job?a=1&b=2)'))

    def test_captured_card_and_detail_facts_are_identical_for_all_recovered_fixtures(self):
        for case in CASES:
            recovered = recover_detail(case['provider'], candidate(case), response(case))
            source = dict(deepcopy(SOURCES[11242]), external_id=case['external_id'],
                          content_external_id=case['external_id'], url=case['url'], source_url=case['url'],
                          source_slug=case['provider'], body=recovered.source_body,
                          metadata_json=json.dumps(recovered.source_metadata), location='Remote')
            a = prepare_card_evidence(card(source), source, PROFILE)
            b = prepare_detail_display(detail(source), PROFILE)
            self.assertEqual(a['facts'], b['facts'])
            self.assertEqual(a['pay'], b['pay'])
            html = render_authenticated_job_page(detail(source), profile=PROFILE, navigation='')
            self.assertIn('Employer description', html)
            for label, value in a['facts']:
                self.assertIn(value, unescape(html))
            self.assertNotIn('Published hourly-rate fields', html)
            self.assertNotIn('Source page&#x27;s structured', html)
            self.assertNotIn('Other published fields', html)
            self.assertNotIn('Hourly range minimum:', html)
            self.assertNotIn('Hourly range maximum:', html)

    def test_missing_or_mismatched_source_does_not_invent_facts_or_personalization(self):
        source = deepcopy(SOURCES[11242]); source['body'] = ''
        self.assertIsNone(prepare_detail_display(detail(source), PROFILE))
        html = render_authenticated_job_page(detail(source), profile=PROFILE, navigation='')
        self.assertNotIn('90', html.split('<body')[1])
        self.assertNotIn('Your profile lists', html)
        source = deepcopy(SOURCES[11242]); job = detail(source); job['rich_external_id'] = 'another'
        self.assertIsNone(prepare_detail_display(job, PROFILE))

    def test_normal_detail_actions_type_unknowns_and_internal_diagnostics(self):
        source = deepcopy(SOURCES[1039]); job = detail(source)
        profile = dict(PROFILE, location={'country':'Portugal'})
        html = render_authenticated_job_page(job, profile=profile, navigation='<nav>Matches</nav>')
        self.assertIn('Talent network — future consideration', html)
        self.assertIn('Eligibility from Portugal needs confirmation.', html)
        self.assertIn('View source listing</a>', html)
        self.assertNotIn('Apply on company site</a>', html)
        self.assertNotIn('workflow-card\' data-action-card', html)  # no empty panel
        for forbidden in ('Source record:', 'source block', 'Qualifying observation:',
                          'recommendation-list membership', 'existing comparison', source['last_captured_at']):
            self.assertNotIn(forbidden, html)
        job['_authenticated_recommendation'] = card(source)
        html = render_authenticated_job_page(job, profile=profile, navigation='',
                                             workflow_controls='<form action="/action"></form>')
        self.assertIn('Apply on company site</a>', html)
        self.assertIn('action="/action"', html)
        job['public_state'] = 'temporarily_unavailable'
        html = render_authenticated_job_page(job, profile=profile, navigation='')
        self.assertIn('Availability not established', html)
        self.assertNotIn('Apply on company site</a>', html)

    def test_card_omits_generic_overlap_without_hiding_original_requirements(self):
        source = deepcopy(SOURCES[11242]); a = prepare_card_evidence(card(source), source, PROFILE)
        body = render_card_evidence(a, 'match-1')
        self.assertTrue(a['reason'])  # evidence remains available to diagnostics
        self.assertNotIn(a['reason'], unescape(body))
        self.assertIn('Docker', body)
        detail_body = render_authenticated_job_page(detail(source), profile=PROFILE, navigation='')
        self.assertIn('Preferred', detail_body)
        self.assertIn('Publications in peer-reviewed journals', detail_body)
        self.assertNotIn('Source excerpt:', body)
        self.assertIn('<summary', detail_body)
        self.assertNotIn('Opportunity type not established', body)


if __name__ == '__main__':
    unittest.main()
