"""Compact recommendation UI from recorded evidence; no admission or browser claim.

The owner superseded prominent main/conditional categories and repeated generic
qualification audits. Complete source wording and mandatory conflicts remain.
"""
from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
import unittest

from tests.test_transferable_task_presentation import explanation_fixture
from tests.test_candidate_condition_comparisons import confirmed, prepared
from tests.test_candidate_source_display import detail
from wahojobs.authenticated_card_evidence import prepare_card_evidence, render_card_evidence
from wahojobs.authenticated_source_detail import render_authenticated_job_page
from wahojobs.candidate_decision import (
    attach_decision, material_warnings, render_application_guidance, render_fit_support,
    render_material_warnings, render_reasons,
)
from wahojobs.profile_opportunity_navigation import render_profile_update


class DisclosureText(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.depth = 0
        self.main = []
        self.disclosures = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'details':
            self.depth += 1
            self.disclosures.append(dict(attrs))

    def handle_endtag(self, tag):
        if tag == 'details':
            self.depth -= 1

    def handle_data(self, text):
        if not self.depth:
            self.main.append(text)


class RecommendationPresentationTests(unittest.TestCase):
    def test_card_has_one_grounded_fit_and_no_repeated_qualification_audit(self):
        source, match, profile = explanation_fixture()
        packet = prepare_card_evidence(match, source, profile)
        comparisons = deepcopy(packet['comparisons'])
        body = unescape(render_card_evidence(packet, 'card'))
        self.assertEqual(body.count("class='decision-fit'"), 1)
        self.assertIn('Your experience reviewing written responses is relevant to this work.', body)
        self.assertIn('Review and evaluate AI-generated content', body)
        self.assertNotIn('Comparison not established', body)
        self.assertNotIn('not independent verification', body)
        self.assertNotIn('Qualifications & conditions', body)
        self.assertNotIn('Strong attention to detail', body)
        self.assertIn('Eligibility from Brazil needs confirmation.', body)
        self.assertEqual(packet['comparisons'], comparisons)
        self.assertTrue(any(row['status'] == 'unresolved' for row in comparisons))

    def test_detail_keeps_complete_source_and_pair_evidence_in_closed_disclosures(self):
        source, match, profile = explanation_fixture()
        body = render_authenticated_job_page(detail(source, match), profile=profile, navigation='')
        parsed = DisclosureText(body)
        main = ''.join(parsed.main)
        self.assertIn('Before you apply', main)
        self.assertIn('Your experience reviewing written responses', main)
        self.assertNotIn('Comparison not established', main)
        self.assertNotIn('Employer task:', main)
        self.assertIn('Employer description and requirements', body)
        self.assertIn('About this recommendation', body)
        self.assertIn('Employer task:', body)
        self.assertIn('No prior AI, tech, or content moderation experience required', body)
        self.assertIn('Assess content for accuracy', body)
        self.assertTrue(parsed.disclosures)
        self.assertTrue(all('open' not in attrs for attrs in parsed.disclosures))

    def test_generic_quality_is_conditional_application_advice_not_candidate_fact(self):
        packet = prepared('## Requirements\nStrong attention to detail.\n\nSelf-motivated and reliable when working independently.')
        original = deepcopy(packet['comparisons'])
        advice = unescape(render_application_guidance(packet))
        self.assertIn('The employer asks for “Strong attention to detail.”', advice)
        self.assertIn('use a genuine example if you have one', advice)
        self.assertNotIn('You have strong attention', advice)
        self.assertFalse(any(w['kind'] == 'requirement' for w in material_warnings(packet)))
        self.assertEqual(packet['comparisons'], original)

    def test_missing_essential_credential_remains_visible(self):
        packet = prepared('## Requirements\nA medical license is required.')
        body = unescape(render_card_evidence(packet, 'medical'))
        self.assertIn('Check the requirement', body)
        self.assertIn('A medical license is required.', body)
        self.assertNotIn('Your experience', body)

    def test_recorded_mandatory_conflict_is_prominent_and_stops_encouraging_advice(self):
        packet = prepared('## Requirements\nPhD in Molecular Biology required.')
        row = next(row for row in packet['comparisons'] if row['kind'] == 'education')
        # A recorded comparator outcome, not a claim that this synthetic profile
        # actually contradicts the fixture. Consumer tests establish that path.
        row.update(status='contradicted', modality='required', message='Confirmed contradiction.')
        packet['decision_short_reason'] = 'Your writing experience relates to the tasks.'
        warning = unescape(render_material_warnings(packet, compact=True))
        advice = unescape(render_application_guidance(packet))
        self.assertIn('Requirement conflict', warning)
        self.assertIn('PhD in Molecular Biology required.', warning)
        self.assertNotIn('Describe a genuine example', advice)
        self.assertIn('does not resolve a conflicting requirement', advice)

    def test_partial_education_support_does_not_claim_complete_eligibility(self):
        packet = prepared()
        partial = next(row for row in packet['comparisons'] if row['kind'] == 'education')
        self.assertTrue(partial['supported_parts'])
        warnings = material_warnings(packet)
        self.assertTrue(any(partial['source']['quote'] in warning['text'] for warning in warnings))
        self.assertNotIn('You meet', render_material_warnings(packet))

    def test_country_unknown_is_neither_conflict_nor_worldwide_permission(self):
        source, match, profile = explanation_fixture()
        packet = prepare_card_evidence(match, source, profile)
        warnings = material_warnings(packet)
        location = next(warning for warning in warnings if warning['kind'] == 'location')
        self.assertEqual(location['text'], 'Eligibility from Brazil needs confirmation.')
        self.assertFalse(any(w['kind'] == 'conflict' for w in warnings))
        self.assertNotIn('worldwide eligibility', render_application_guidance(packet))

    def test_general_profile_link_is_honest_and_targeted_link_uses_existing_field(self):
        general = render_profile_update({}, '/job/opportunity-1', general_fallback=True)
        self.assertIn("href='/account/profile'", general)
        self.assertIn('Review profile &amp; preferences', general)
        self.assertIn('(optional)', general)
        self.assertNotIn('focus=', general)
        packet = prepared('## Requirements\nPhD in biology required.', {'provenance': {'field_sources': []}})
        focused = render_profile_update(packet, '/job/opportunity-3809?variant=11242', general_fallback=True)
        self.assertIn('focus=education', focused)
        self.assertIn('Review education details', focused)

    def test_full_source_and_support_escape_public_and_candidate_text(self):
        source, match, profile = explanation_fixture()
        packet = {'decision_short_reason': '<img src=x onerror=alert(1)>',
                  'transferable_task_links': [{'profile_fact': {'text': '<img src=x>'},
                                              'quote': '<script>alert(2)</script>'}]}
        rendered = render_reasons(packet) + render_fit_support(packet)
        self.assertNotIn('<img ', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;img ', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        source['body'] += '\n\n## Other information\n<script>alert(3)</script>'
        body = render_authenticated_job_page(detail(source, match), profile=profile, navigation='')
        self.assertNotIn('<script>alert(3)</script>', body)
        self.assertIn('&lt;script&gt;alert(3)&lt;/script&gt;', body)

    def test_compact_warning_limit_never_hides_a_recorded_conflict(self):
        packet = prepared('## Requirements\nPhD in biology required.\n\nA license is required.\n\nA computer is required.')
        for row in packet['comparisons']:
            row.update(status='contradicted', modality='required')
        warnings = material_warnings(packet)
        rendered = unescape(render_material_warnings(packet, compact=True))
        conflicts = [warning for warning in warnings if warning['kind'] == 'conflict']
        self.assertGreaterEqual(len(conflicts), 3)
        for warning in conflicts:
            self.assertIn(warning['text'], rendered)

    def test_recorded_hard_workload_conflict_is_not_positive_application_advice(self):
        packet = {'comparisons': [], 'decision_short_reason': 'Your writing relates to this work.',
                  'decision_profile_context': {'workload': dict(state='hard_conflict', outcome='fail',
                      guidance='Your part-time-only requirement conflicts with this posting’s workload.')}}
        self.assertIn('Requirement conflict', render_material_warnings(packet, compact=True))
        self.assertIn('part-time-only', render_material_warnings(packet))
        self.assertNotIn('Describe a genuine example', render_application_guidance(packet))

    def test_soft_workload_difference_is_visible_once_without_hard_rejection(self):
        text = 'You prefer part-time work; this posting lists full-time work. Confirm whether your preferred schedule is possible.'
        packet = {'comparisons': [], 'decision_profile_context': {'workload':
                  dict(state='soft_difference', outcome='fail', guidance=text)}}
        combined = render_material_warnings(packet) + render_application_guidance(packet)
        self.assertEqual(combined.count(text), 1)
        self.assertNotIn('Requirement conflict', combined)

    def test_preferred_part_time_is_guidance_without_invented_hours_or_contract_type(self):
        text = 'You prefer part-time work. Explain your availability and confirm the schedule with the employer.'
        packet = {'comparisons': [], 'decision_profile_context': {'workload':
                  dict(state='preference', outcome='unknown', guidance=text)}}
        self.assertEqual(render_material_warnings(packet), '')
        advice = render_application_guidance(packet)
        self.assertIn(text, advice)
        self.assertNotIn('20 hours', advice)
        self.assertNotIn('contractor', advice)

    def test_compact_warning_keeps_country_credentials_and_hard_schedule_after_other_unknowns(self):
        packet = prepared('## Requirements\nA computer is required.\n\nA quiet room is required.\n\nA medical license is required.')
        packet['geography'] = 'Eligibility from Brazil needs confirmation.'
        packet['decision_profile_context'] = {'workload': dict(state='hard_unresolved', outcome='unknown',
            guidance='Confirm whether this posting meets your part-time-only requirement.')}
        rendered = unescape(render_material_warnings(packet, compact=True))
        for text in ('A medical license is required.', packet['geography'], 'part-time-only requirement'):
            self.assertIn(text, rendered)

    def test_source_location_conflict_uses_bound_quote_and_stops_positive_advice(self):
        source, match, profile = explanation_fixture()
        source['body'] += '\n\nApplicant Location: Canada'
        match['source_task_location_checks'] = [dict(status='incompatible', reason='country_restriction',
            quote='Applicant Location: Canada', source_reference='source location field',
            job_id=source['job_id'], source_hash=source['material_content_sha256'])]
        packet = prepare_card_evidence(match, source, profile)
        rendered = unescape(render_material_warnings(packet, compact=True))
        self.assertIn('location requirement conflicts with your profile', rendered)
        self.assertIn('Applicant Location: Canada', rendered)
        self.assertNotIn('Eligibility from Brazil needs confirmation', rendered)
        self.assertNotIn('Describe a genuine example', render_application_guidance(packet))

    def test_stale_foreign_or_unquoted_location_checks_cannot_create_a_conflict(self):
        source, match, profile = explanation_fixture()
        source['body'] += '\n\nApplicant Location: Canada'
        recorded = dict(status='incompatible', quote='Applicant Location: Canada',
            source_reference='source location field', job_id=source['job_id'],
            source_hash=source['material_content_sha256'])
        for key, value in (('job_id', -1), ('source_hash', 'old-body'), ('quote', 'Applicant Location: France'),
                           ('source_reference', 'source block 999')):
            with self.subTest(key=key):
                match['source_task_location_checks'] = [dict(recorded, **{key: value})]
                packet = prepare_card_evidence(match, source, profile)
                self.assertEqual(packet['decision_source_location_checks'], [])
                self.assertFalse(any(w['kind'] == 'conflict' for w in material_warnings(packet)))

    def test_published_location_warning_requires_exact_binding_and_non_generic_wording(self):
        source, match, profile = explanation_fixture()
        packet = prepare_card_evidence(match, source, profile)
        reference = dict(value='US citizens only', generic_country_tag=False,
            source_field='props.pageProps.job.location', job_id=packet['job_id'],
            external_id=packet['external_id'], source_url=packet['url'],
            material_content_sha256=packet['source_hash'])
        packet['location_context']['published_field'] = reference
        before = deepcopy(packet)
        warning = render_material_warnings(packet, compact=True)
        self.assertIn('Check the source’s location information: “US citizens only”.', unescape(warning))
        self.assertFalse(any(w['kind'] == 'conflict' for w in material_warnings(packet)))
        self.assertEqual(packet, before)
        for field, value in (('job_id', -1), ('external_id', 'another-posting'),
                             ('source_url', 'https://other.example.test/job'),
                             ('material_content_sha256', 'stale'), ('source_field', 'company.address'),
                             ('generic_country_tag', True), ('generic_country_tag', None)):
            with self.subTest(field=field, value=value):
                packet['location_context']['published_field'] = dict(reference, **{field: value})
                self.assertNotIn('US citizens only', render_material_warnings(packet, compact=True))
        for value in ('Vancouver', 'Boston', 'Vancouver, Canada', 'Remote — Boston'):
            with self.subTest(place=value):
                packet['location_context']['published_field'] = dict(reference, value=value)
                self.assertNotIn(value, render_material_warnings(packet, compact=True))
        for value in ('US citizens only', 'Must reside in Canada', 'US residency required',
                      'Applicants in Canada only', 'Not available in Germany'):
            with self.subTest(restriction=value):
                packet['location_context']['published_field'] = dict(reference, value=value)
                self.assertIn(value, unescape(render_material_warnings(packet, compact=True)))
                self.assertFalse(any(w['kind'] == 'conflict' for w in material_warnings(packet)))

    def test_complete_language_support_is_reused_only_for_exact_simple_clause(self):
        def packet_for(quote):
            packet = prepared('## Requirements\n' + quote)
            row = packet['comparisons'][0]
            source = row['source']
            check = dict(status='supported', modality='required', quote=quote,
                languages=['english'], levels=['fluent'], operator='all_of',
                profile_facts=[dict(path='language_proficiency.English', language='English', proficiency='native')],
                heading=source['heading'], source_field=f"{source['block_reference']}:line {source['line']}",
                source_reference=dict(job_id=packet['job_id'], external_id=packet['external_id'],
                    source_url=packet['url'], material_content_sha256=packet['source_hash']))
            attach_decision(packet, dict(source_language_checks=[check]))
            return packet, check
        packet, check = packet_for('Fluent in English')
        original = deepcopy(packet)
        self.assertNotIn('Fluent in English', render_material_warnings(packet))
        self.assertEqual(packet, original)
        for changes in ({'status': 'unresolved'}, {'levels': ['native']}, {'languages': ['french']},
                        {'operator': 'any_of'}, {'source_field': 'source block 999:line 1'},
                        {'heading': 'Another heading'}, {'profile_facts': []}):
            with self.subTest(changes=changes):
                packet['language_comparisons'] = [dict(check, **changes)]
                self.assertIn('Fluent in English', render_material_warnings(packet))
        for field in ('job_id', 'external_id', 'source_url', 'material_content_sha256'):
            with self.subTest(binding=field):
                packet['language_comparisons'] = [dict(check, source_reference=dict(check['source_reference'], **{field: 'foreign'}))]
                self.assertIn('Fluent in English', render_material_warnings(packet))
        for quote in ('Clear written communication skills in English',
                      'Fluent in English with excellent written communication skills',
                      'Fluent in English and French', 'Native or near-native English'):
            with self.subTest(compound=quote):
                packet, _ = packet_for(quote)
                self.assertIn(quote, unescape(render_material_warnings(packet)))
        packet, check = packet_for('Native English')
        packet['language_comparisons'] = [dict(check, levels=['native'], status='contradicted')]
        self.assertIn('conflicts with your profile', render_material_warnings(packet))

    def test_specialist_uncertainty_note_requires_current_exact_task_paragraph(self):
        source, match, profile = explanation_fixture()
        packet = prepare_card_evidence(match, source, profile)
        quote = 'Review examples of phonetics and syntax through linguistic analysis.'
        packet['text'] += '\n\n' + quote
        note = 'Practical experience with these specialist tasks still needs confirmation.'
        task = dict(kind='linguistic_analysis', status='uncertain', quote=quote, candidate_note=note,
            source_reference=dict(job_id=packet['job_id'], external_id=packet['external_id'],
                source_url=packet['url'], material_content_sha256=packet['source_hash']))
        attach_decision(packet, dict(source_task_fit=task))
        self.assertIn(note, render_material_warnings(packet, compact=True))
        for field, value in (('kind', 'accepted_task_conditions'), ('status', 'supported'),
                             ('quote', 'phonetics and syntax')):
            with self.subTest(field=field):
                attach_decision(packet, dict(source_task_fit=dict(task, **{field: value})))
                self.assertNotIn(note, render_material_warnings(packet, compact=True))
        for field in ('job_id', 'external_id', 'source_url', 'material_content_sha256'):
            with self.subTest(binding=field):
                changed = dict(task, source_reference=dict(task['source_reference'], **{field: 'other'}))
                attach_decision(packet, dict(source_task_fit=changed))
                self.assertNotIn(note, render_material_warnings(packet, compact=True))

    def test_preference_only_interest_does_not_imply_existing_experience_in_advice(self):
        source, match, profile = explanation_fixture()
        match.pop('accepted_task_fit')
        match['affirmative_fit'] = {'supported_evidence': [dict(source='preference',
            requirement='writing', profile_evidence='interested in writing')]}
        packet = prepare_card_evidence(match, source, profile)
        self.assertIn('interest does not establish experience', packet['decision_short_reason'])
        self.assertFalse(packet['decision_has_reported_support'])
        advice = render_application_guidance(packet)
        self.assertNotIn('experience or skills in your profile', advice)
        self.assertIn('If you have relevant experience', advice)

    def test_consumed_modality_does_not_replace_current_supported_item_experience(self):
        from tests.candidate_decision_support import demo_profile
        from tests.test_authenticated_card_evidence import card, SOURCES
        from tests.test_professional_background_components import confirmed as confirm_path
        from wahojobs.profiles.canonical_v2 import _material_field_paths
        source = dict(deepcopy(SOURCES[11242]), body='## Requirements\nExperience with Python', metadata_json='{}')
        p = demo_profile()
        earlier = prepare_card_evidence(card(source), source, p)
        prior = next(row for row in earlier['comparisons'] if row['kind'] == 'tools')
        self.assertEqual(prior['status'], 'not_established')
        p['experience']['item_details'] = [dict(item_id='a'*32, field='skills', label='Python',
            contexts=['professional'], autonomy='independent', months=None, basis='self_reported')]
        p['provenance']['field_sources'] = []
        for path in _material_field_paths(p):
            confirm_path(p, path)
        reference = dict(job_id=source['job_id'], external_id=source['external_id'],
            source_url=source['url'], material_content_sha256=source['material_content_sha256'])
        match = dict(card(source), source_task_fit=dict(source_reference=reference,
                                                       conditions=[dict(prior, modality='required')]))
        packet = prepare_card_evidence(match, source, p, include_item_experience=True)
        current = next(row for row in packet['comparisons'] if row['kind'] == 'tools')
        self.assertEqual(current['status'], 'supported')
        self.assertEqual(packet['decision_consumed_conditions'][0]['status'], 'not_established')
        original = deepcopy(packet)
        self.assertNotIn('Experience with Python', render_material_warnings(packet))
        self.assertEqual(packet, original)

    def test_term_headings_never_hide_required_or_contradicted_unparsed_conditions(self):
        for heading in ('Engagement', 'Role details', 'Commitment', 'More about the opportunity'):
            with self.subTest(heading=heading):
                packet = prepared('## ' + heading + '\nA quiet room is required.')
                row = packet['comparisons'][0]
                self.assertEqual((row['kind'], row['modality'], row['status']),
                                 ('unassessed', 'required', 'unresolved'))
                before = deepcopy(packet)
                self.assertIn('A quiet room is required.', render_material_warnings(packet, compact=True))
                self.assertEqual(packet, before)
                row['status'] = 'contradicted'
                self.assertIn('Requirement conflict', render_material_warnings(packet, compact=True))
                self.assertNotIn('Describe a genuine example', render_application_guidance(packet))
        ordinary = prepared('## Engagement\nPayments are made monthly.')
        self.assertFalse(any(w['kind'] == 'requirement' for w in material_warnings(ordinary)))


class RecommendationDecisionIntegrationTests(unittest.TestCase):
    """Actual disposable consumer and exact-detail rendering, with no listener."""
    def test_source_only_country_rejection_is_visible_on_exact_detail(self):
        from tests.test_transferable_task_matching import TransferableTaskMatchingTests, ENTRY
        from wahojobs.authenticated_variant_details import variant_detail_url
        base = TransferableTaskMatchingTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        _, run, context, match = base.current(ENTRY + '\n\nApplicant Location: Canada')
        self.assertFalse(base.shown(context))
        self.assertEqual(match['source_task_location_checks'][0]['status'], 'incompatible')
        response = base.base.f.get(variant_detail_url(match, run_id=run.match_run_id))
        self.assertEqual(response.status, 200)
        body = unescape(response.body.decode())
        main = ''.join(DisclosureText(body).main)
        self.assertIn('location requirement conflicts with your profile', main)
        self.assertIn('Applicant Location: Canada', main)
        self.assertNotIn('Eligibility from Brazil needs confirmation', main)
        self.assertNotIn('Describe a genuine example', main)

    def test_complete_language_check_agrees_across_actual_card_and_exact_detail(self):
        from tests.test_transferable_task_matching import TransferableTaskMatchingTests, ENTRY
        from wahojobs.authenticated_variant_details import variant_detail_url
        base = TransferableTaskMatchingTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        before = deepcopy(base.base.f.profile)
        for quote, warning in (('Fluent in English', False),
                               ('Fluent in English with excellent written communication skills', True)):
            with self.subTest(quote=quote):
                response, run, context, match = base.current(ENTRY + '\n\n' + quote)
                self.assertTrue(base.shown(context))
                check = next(c for c in match['source_language_checks'] if c['quote'] == quote)
                self.assertEqual(check['status'], 'supported')
                snapshot = deepcopy(match)
                exact = base.base.f.get(variant_detail_url(match, run_id=run.match_run_id))
                self.assertEqual(exact.status, 200)
                for page in (response, exact):
                    main = ''.join(DisclosureText(page.body.decode()).main)
                    self.assertEqual('Check the requirement: “' + quote + '”.' in main, warning)
                self.assertIn(quote, exact.body.decode())
                self.assertEqual(match, snapshot)
                self.assertEqual(base.base.f.profile, before)

    def test_exact_detail_uses_actual_soft_and_hard_workload_outcomes(self):
        from tests.test_transferable_task_matching import TransferableTaskMatchingTests
        from wahojobs.authenticated_variant_details import variant_detail_url
        from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2
        from unittest.mock import patch
        from wahojobs import authenticated_variant_details as variants
        base = TransferableTaskMatchingTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        base.base.f.update_inventory("UPDATE jobs SET commitment='Full-time'")
        base.base.f.set_preferences('part_time')
        _, run, context, match = base.current()
        self.assertTrue(base.shown(context), 'A soft workload preference does not ban a different schedule')
        target = variant_detail_url(match, run_id=run.match_run_id)
        with patch.object(variants, 'prepare_variant_notice', wraps=variants.prepare_variant_notice) as notice:
            response = base.base.f.get(target)
        local = notice.call_args.args[0]['_authenticated_local_checks']['match']
        self.assertEqual(local['_workload_preference_outcomes'][0]['outcome'], 'fail')
        self.assertIn('this posting lists full-time work', response.body.decode())
        self.assertNotIn('Requirement conflict', response.body.decode())
        p = deepcopy(base.base.f.profile)
        p['constraints']['hard_constraints'] = ['part-time only']
        confirmed(p, 'constraints.hard_constraints[0]')
        base.base.f.profile = validate_canonical_profile_v2(p)
        _, run, context, match = base.current()
        self.assertFalse(base.shown(context))
        with patch.object(variants, 'prepare_variant_notice', wraps=variants.prepare_variant_notice) as notice:
            response = base.base.f.get(variant_detail_url(match, run_id=run.match_run_id))
        local = notice.call_args.args[0]['_authenticated_local_checks']['match']
        self.assertEqual(local['_workload_preference_outcomes'][0]['outcome'], 'fail')
        self.assertEqual(response.status, 200)
        self.assertIn('part-time-only requirement conflicts', response.body.decode())
        self.assertIn('Requirement conflict', response.body.decode())
        self.assertNotIn('Describe a genuine example', ''.join(DisclosureText(response.body.decode()).main))

    def test_unparsed_required_engagement_condition_survives_actual_consumer_and_detail(self):
        from tests.test_transferable_task_matching import TransferableTaskMatchingTests, ENTRY
        from wahojobs.authenticated_variant_details import variant_detail_url
        base = TransferableTaskMatchingTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        response, run, context, match = base.current(ENTRY + '\n\n## Engagement\nA quiet room is required.')
        self.assertTrue(base.shown(context))
        self.assertTrue(match['conditional_task_fit'])
        self.assertTrue(any(row['source']['quote'] == 'A quiet room is required.'
                            and row['modality'] == 'required' and row['status'] == 'unresolved'
                            for row in match['source_task_fit']['conditions']))
        exact = base.base.f.get(variant_detail_url(match, run_id=run.match_run_id))
        self.assertEqual(exact.status, 200)
        for page in (response, exact):
            main = ''.join(DisclosureText(page.body.decode()).main)
            self.assertIn('Check the requirement: “A quiet room is required.”', main)
            self.assertNotIn('You have a quiet room', main)


if __name__ == '__main__':
    unittest.main()
