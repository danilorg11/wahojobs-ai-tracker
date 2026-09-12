"""Offline labelled semantic fixtures prove contracts, not classifier quality."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import patch

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_accepted_title_uncertainty import profile, BODY
from tests import test_accepted_task_matching as task_cases
from tests import test_generic_behavior_admission as generic_cases
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_card_evidence import load_card_sources, prepare_card_evidence
from wahojobs.candidate_condition_comparisons import compare_conditions, _professional_background
from wahojobs.matching.source_task_fit import apply_source_task_fit
from wahojobs.professional_background_duration import compare_duration
from wahojobs.professional_background_semantics import (
    ComparisonContext, ProfessionalBackgroundEvidence, accepted_source_binding,
    build_request, digest, output_schema, validate_response,
)


CLAUSE = '5+ years of relevant professional experience in marketing'
COMPOUND = '5+ years of relevant professional experience in Customer success / support operations'


def confirmed(p, path):
    p['provenance']['field_sources'].append(dict(field_path=path,
        path_version='canonical_profile_v2_path_v1', source_ordinals=[1],
        source_kind='user_confirmation', explicit=True))


def duration_profile(years=None, *, domain='marketing', constraint=None, role='Marketing specialist'):
    p = profile(role, 6)
    if years is not None:
        p['experience']['years_by_domain'] = [dict(domain=domain, years=years)]
        confirmed(p, 'experience.years_by_domain[0].domain')
        confirmed(p, 'experience.years_by_domain[0].years')
    if constraint:
        p['constraints']['hard_constraints'] = [constraint]
        confirmed(p, 'constraints.hard_constraints[0]')
    return p


def packet(quote=CLAUSE, heading='Requirements'):
    return dict(job_id=1, external_id='contract', url='https://example.test/contract', source_hash='material',
        captured_at='2026-09-10', caveats=[], kind='Advertised role', text=quote,
        conditions=[dict(heading=heading, reference='source block 1', text=quote)])


class DurationComponentTests(unittest.TestCase):
    def test_legacy_profile_provenance_is_not_reinterpreted_as_bound_duration(self):
        from tests.test_confirmed_activity_matching import candidate
        p = candidate(['Model output evaluation'])
        p['experience']['total_years'] = 6
        p['experience']['years_by_domain'] = {'marketing': 2}
        before = deepcopy(p)
        result = _professional_background(CLAUSE, p)
        self.assertEqual(result[0], 'not_established')
        self.assertFalse(result[3])
        self.assertEqual(compare_conditions(packet(), p)[0]['components']['required_duration']['status'], 'unresolved')
        self.assertEqual(p, before)

    def test_unknown_duration_preserves_partial_role_and_whole_requirement_unknown(self):
        row = compare_conditions(packet(), duration_profile())[0]
        self.assertEqual(row['status'], 'unresolved')
        self.assertEqual(row['components']['occupational_relevance']['status'], 'supported_partial')
        self.assertEqual(row['components']['required_duration']['status'], 'unresolved')
        self.assertEqual(row['components']['responsibilities_and_depth']['status'], 'unresolved')

    def test_explicit_insufficient_equal_and_sufficient_relevant_totals(self):
        for years, status in [(0, 'contradicted'), (2, 'contradicted'), (4.99, 'contradicted'), (5, 'supported'), (6, 'supported')]:
            with self.subTest(years=years):
                p = duration_profile(years)
                row = compare_conditions(packet(), p)[0]
                self.assertEqual(row['components']['required_duration']['status'], status)
                self.assertEqual(row['status'], 'contradicted' if status == 'contradicted' else 'unresolved')
                self.assertTrue(row['supported_parts'])

    def test_same_scope_compatible_units_and_no_interval_summing(self):
        p = duration_profile(5)
        self.assertEqual(compare_duration('60 months of relevant professional experience in marketing', p)['status'], 'supported')
        self.assertEqual(compare_duration('61 months of relevant professional experience in marketing', p)['status'], 'contradicted')
        p = duration_profile(role='Marketing specialist: 2 years at Acme; 2 years at Beta')
        self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')

    def test_lower_upper_and_overlapping_bounds_are_not_exhaustive_history(self):
        for statement, status in [
            ('I have at least 2 years of professional experience in marketing', 'unresolved'),
            ('I have at least 5 years of professional experience in marketing', 'supported'),
            ('I have at most 2 years of professional experience in marketing', 'contradicted'),
            ('I have at most 4.999999999999999999 years of professional experience in marketing', 'contradicted'),
            ('I have at most 6 years of professional experience in marketing', 'unresolved'),
            ('I have exactly 2 years of professional experience in marketing', 'contradicted'),
            ('I have a total of 2 years of professional experience in marketing', 'contradicted'),
            ('I worked 2 years in marketing at Acme', 'unresolved'),
            ('Partial history: 2 years of professional experience in marketing', 'unresolved'),
        ]:
            with self.subTest(statement=statement):
                self.assertEqual(compare_duration(CLAUSE, duration_profile(constraint=statement))['status'], status)
        p = duration_profile(2, constraint='I have at least 2 years of professional experience in marketing')
        self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')

    def test_conflicting_bounds_are_unresolved(self):
        p = duration_profile(2, constraint='I have at least 5 years of professional experience in marketing')
        self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')

    def test_explicit_total_is_not_weakened_by_a_compatible_lower_bound(self):
        p = duration_profile(constraint='I have exactly 2 years of professional experience in marketing')
        p['constraints']['hard_constraints'].append('I have at least 2 years of professional experience in marketing')
        confirmed(p, 'constraints.hard_constraints[1]')
        self.assertEqual(compare_duration(CLAUSE, p)['status'], 'contradicted')

    def test_career_years_unrelated_scope_and_unconfirmed_values_cannot_prove_duration(self):
        for p in [duration_profile(), duration_profile(2, domain='biology'), duration_profile(2, domain='marketing at Acme')]:
            self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')
        p = duration_profile(2)
        p['provenance']['field_sources'] = [r for r in p['provenance']['field_sources'] if not r['field_path'].startswith('experience.years_by_domain')]
        self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')
        p = duration_profile(2)
        for r in p['provenance']['field_sources']:
            if r['field_path'].startswith('experience.years_by_domain'):
                r['source_kind'] = 'resume'
        self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')

    def test_preferred_and_unspecified_modalities_preserve_shortfall_without_mandatory_label(self):
        for heading, mode in [('Preferred qualifications', 'preferred'), ('Source wording', 'unspecified'), ('Requirements', 'required')]:
            row = compare_conditions(packet(heading=heading), duration_profile(2))[0]
            self.assertEqual(row['modality'], mode)
            self.assertEqual(row['status'], 'contradicted')

    def test_alternative_failure_requires_every_option_and_slashes_stay_unresolved(self):
        quote = '5+ years of relevant professional experience in marketing or sales'
        p = duration_profile(2)
        self.assertEqual(compare_duration(quote, p)['status'], 'unresolved')
        p['experience']['years_by_domain'].append(dict(domain='sales', years=3))
        confirmed(p, 'experience.years_by_domain[1].domain'); confirmed(p, 'experience.years_by_domain[1].years')
        self.assertEqual(compare_duration(quote, p)['status'], 'contradicted')
        p['experience']['years_by_domain'][1]['years'] = 5
        self.assertEqual(compare_duration(quote, p)['status'], 'supported')
        for scope in ['marketing / sales', 'marketing and sales', 'marketing, sales']:
            self.assertEqual(compare_duration('5+ years of relevant professional experience in ' + scope, p)['status'], 'unresolved')

    def test_profile_and_packet_are_immutable_and_other_qualifications_are_separate(self):
        p = duration_profile(2); q = packet()
        q['conditions'].append(dict(heading='Requirements', reference='source block 2', text='Native English required.'))
        before = deepcopy((p, q))
        row = compare_conditions(q, p)[0]
        self.assertEqual((p, q), before)
        self.assertEqual(row['components']['other_qualifications'][0]['kind'], 'unassessed')
        self.assertEqual(row['components']['other_qualifications'][0]['modality'], 'required')


class BackgroundIntegrationTests(unittest.TestCase):
    role = task_cases.AcceptedTaskMatchingTests.role
    source = generic_cases.GenericBehaviorAdmissionTests.source

    def setUp(self):
        self.f = SyntheticMatcherFixture(); self.addCleanup(self.f.close)
        self.f.profile = duration_profile()
        self.role('Uncatalogued assessment position', 'Remote')
        self.revision = 'offline-revision-1'
        self.store = ProfessionalBackgroundEvidence(recipe='offline-contract-v1', model='labelled-fixture', basis='offline_labelled_stub')
        self.f.integration._professional_background_evidence = self.store
        def authority():
            return browser.MatchesAuthorityResult('profile', browser._AuthorizedMatchesState(
                'profile', draft_binding=self.f.owner * 64, account_id='account-' + self.f.owner,
                environment_namespace='synthetic', principal_id='principal-' + self.f.owner,
                session_id='session-' + self.f.owner, profile_id=self.f.profile['identity']['profile_id'],
                profile_v2=self.f.profile, revision_id=self.revision))
        self.f.authority = authority
        self.source([CLAUSE], heading='Requirements')

    def current(self, target='/find-matches'):
        r = self.f.get(target); self.assertEqual(r.status, 200, r.body)
        ctx = self.f.last_run().recommendation_context
        m = next(m for v in ctx['matches'].values() for m in v if m['job_id'] == 7003)
        return ctx, m

    def inputs(self):
        ctx, match = self.current()
        with self.f.provider() as c:
            sources = accepted_source_binding(c, load_card_sources(c, [match]))
        source = sources[7003]
        context = self.f.authority().authorized_state().professional_background_context(self.store)
        card = prepare_card_evidence(match, source, self.f.profile)
        request = build_request(card, card['comparisons'][0], self.f.profile, context)
        self.assertIsNotNone(request)
        return source, card, context, request

    def publish(self, relation='supported_partial'):
        source, card, context, request = self.inputs()
        output = dict(request_id=request['request_id'], relation=relation,
            candidate_fact_ids=list(request['candidate_facts']), source_span=request['occupational_span'],
            rationale='Manually labelled contract fixture: occupational relation only; depth and qualifications unassessed.')
        self.store.publish(request, output)
        return source, card, context, request, output

    def test_required_shortfall_is_rejected_by_consumer_not_section_barrier(self):
        self.f.integration._professional_background_evidence = None
        self.f.profile = duration_profile(2)
        ctx, match = self.current()
        self.assertEqual(match['affirmative_fit_status'], 'conflicting')
        self.assertFalse(match['conditional_task_fit'])
        self.assertTrue(match['affirmative_fit']['conflicting_requirements'])
        self.assertEqual(match['source_qualification_comparisons'][0]['components']['required_duration']['status'], 'contradicted')
        # Test-only downstream relocation exposes eligibility; never membership.
        probe = deepcopy(match); probe['preview_section'] = 'also_worth_reviewing'
        moved = dict(matches={'also_worth_reviewing': [probe]})
        self.assertEqual(browser._primary_presentation_matches(moved), [])
        self.assertEqual(browser._conditional_presentation_matches(moved), [])
        self.assertEqual(match['score'], 12)

    def test_unknown_duration_keeps_existing_conditional_fit_without_section_promotion(self):
        _, match = self.current()
        self.assertTrue(match['conditional_task_fit'])
        self.assertEqual(match['affirmative_fit_status'], 'uncertain')
        self.assertEqual(match['preview_section'], 'explore_only')
        context = self.f.last_run().recommendation_context
        self.assertEqual([m['job_id'] for m in browser._conditional_presentation_matches(context)], [7003])
        self.assertEqual(browser._primary_presentation_matches(context), [])

    def test_sufficient_equal_and_unknown_alternative_duration_remain_conditions(self):
        for years in (5, 6):
            self.f.profile = duration_profile(years)
            _, m = self.current()
            self.assertEqual(m['affirmative_fit_status'], 'uncertain')
            self.assertTrue(m['conditional_task_fit'])
            self.assertEqual(m['preview_section'], 'explore_only')
        self.f.profile = duration_profile(2)
        self.source(['5+ years of relevant professional experience in marketing or sales'], heading='Requirements')
        _, m = self.current()
        self.assertTrue(m['conditional_task_fit'])
        self.assertFalse(m['affirmative_fit']['conflicting_requirements'])

    def test_required_contradiction_precedes_another_unsupported_background(self):
        self.f.profile = duration_profile(2)
        self.source([CLAUSE, 'Hands-on biology experience'], heading='Requirements')
        _, m = self.current()
        self.assertEqual(m['affirmative_fit_status'], 'conflicting')
        self.assertFalse(m['conditional_task_fit'])
        self.assertTrue(m['affirmative_fit']['conflicting_requirements'])

    def test_positive_semantic_support_cannot_mask_objective_shortfall(self):
        self.f.profile = duration_profile(2)
        self.publish()
        _, match = self.current()
        row = match['source_qualification_comparisons'][0]
        self.assertIn('semantic', row['components']['occupational_relevance'])
        self.assertTrue(row['supported_parts'])
        self.assertEqual(row['status'], 'contradicted')
        self.assertEqual(match['affirmative_fit_status'], 'conflicting')
        self.assertFalse(match['conditional_task_fit'])

    def test_preferred_shortfall_is_not_a_mandatory_veto(self):
        self.f.profile = duration_profile(2)
        self.source(['Hands-on experience in a marketing role'], heading='Requirements',
                    extra='\n\n## Preferred qualifications\n\n' + CLAUSE)
        _, match = self.current()
        self.assertEqual(match['affirmative_fit_status'], 'uncertain')
        self.assertFalse(match['affirmative_fit']['conflicting_requirements'])
        self.assertTrue(match['conditional_task_fit'])

    def test_p02_automatic_versus_stubbed_relation_and_p01_contrast(self):
        self.source([COMPOUND], heading='Requirements')
        self.f.profile = profile('Customer support specialist', 6)
        automatic_context, automatic = self.current()
        self.assertFalse(automatic['source_qualification_comparisons'][0]['supported_parts'])
        self.assertEqual(browser._conditional_presentation_matches(automatic_context), [])
        self.publish()
        ctx, stubbed = self.current()
        self.assertTrue(stubbed['conditional_task_fit'])
        self.assertEqual(stubbed['preview_section'], 'explore_only')
        self.assertEqual([m['job_id'] for m in browser._conditional_presentation_matches(ctx)], [7003])
        self.assertEqual(browser._primary_presentation_matches(ctx), [])
        row = stubbed['source_qualification_comparisons'][0]
        self.assertEqual(row['status'], 'unresolved')
        self.assertEqual(row['components']['required_duration']['status'], 'unresolved')
        self.assertEqual(row['components']['occupational_relevance']['semantic']['basis'], 'offline_labelled_stub')
        self.f.profile = profile()
        p01_context, p01 = self.current()
        self.assertEqual(p01['source_qualification_comparisons'][0]['status'], 'not_established')
        self.assertFalse(p01['conditional_task_fit'])
        self.assertEqual(browser._conditional_presentation_matches(p01_context), [])

    def test_ambiguous_unrelated_and_invalid_output_preserve_independent_evidence(self):
        for relation in ['ambiguous', 'not_established', 'contradicted']:
            with self.subTest(relation=relation):
                self.publish(relation)
                _, m = self.current()
                self.assertTrue(m['source_qualification_comparisons'][0]['supported_parts'])
                self.assertTrue(m['conditional_task_fit'])
        self.source([COMPOUND], heading='Requirements')
        self.f.profile = profile('Biology researcher', 6)
        self.publish('ambiguous')
        _, m = self.current()
        self.assertFalse(m['source_qualification_comparisons'][0]['supported_parts'])
        self.assertFalse(m['conditional_task_fit'])

    def test_semantic_schema_rejects_forged_facts_spans_and_qualification_claims(self):
        _, _, _, request, output = self.publish()
        self.assertFalse(output_schema(request)['additionalProperties'])
        changes = [dict(request_id='other'), dict(candidate_fact_ids=['fact:other']),
            dict(candidate_fact_ids=[]), dict(source_span={'start':0, 'end':1}),
            dict(relation='supported'), dict(duration_status='supported'), dict(rationale='')]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.publish(request, dict(output, **change))

    def test_changed_profile_revision_owner_and_source_prevent_reuse(self):
        source, card, context, request, output = self.publish()
        row = card['comparisons'][0]
        for changed in [replace(context, owner=('other', 'synthetic', 'principal-a')),
                        replace(context, revision_id='offline-revision-2'),
                        replace(context, profile_id='other')]:
            q = build_request(card, row, self.f.profile, changed)
            self.assertIsNone(self.store.lookup(q))
        p = deepcopy(self.f.profile); p['experience']['total_years'] = 7
        self.assertIsNone(build_request(card, row, p, context))
        q = build_request(card, row, p, replace(context, profile_digest=digest(p)))
        self.assertIsNone(self.store.lookup(q))
        for field, value in [('accepted_capture_id', 900), ('material_content_sha256', 'changed'),
                             ('job_id', 7006), ('promotion_policy_version', 'changed')]:
            c = deepcopy(card); c['professional_source_binding'][field] = value
            self.assertIsNone(self.store.lookup(build_request(c, row, self.f.profile, context)))
        c = deepcopy(card); c['text'] += '\nChanged source responsibilities.'
        self.assertIsNone(self.store.lookup(build_request(c, row, self.f.profile, context)))

    def test_capture_binding_requires_exact_current_material_and_candidate_identity(self):
        source, card, context, request = self.inputs()
        altered = deepcopy(source); altered['body'] += ' changed'
        with self.f.provider() as c:
            rebound = accepted_source_binding(c, {7003: altered})
        self.assertNotIn('professional_source_binding', rebound[7003])
        context = replace(context, profile_id='different-owner-profile')
        self.assertIsNone(build_request(card, card['comparisons'][0], self.f.profile, context))

    def test_stale_attached_binding_and_changed_interpretation_cannot_be_consumed(self):
        source, card, context, request, output = self.publish()
        _, m = self.current()
        for field in ('body', 'metadata_json'):
            changed = deepcopy(source)
            changed[field] += ' '
            prepared = prepare_card_evidence(m, changed, self.f.profile, background_context=context)
            self.assertNotIn('semantic', prepared['comparisons'][0]['components']['occupational_relevance'])
        changed_request = deepcopy(request)
        changed_request['binding']['component_version'] = 'future-version'
        changed_request['request_id'] = digest({k:v for k,v in changed_request.items() if k != 'request_id'})
        with self.assertRaises(ValueError):
            self.store.publish(changed_request, dict(output, request_id=changed_request['request_id']))

    def test_recommendation_reuse_invalidates_on_publish_and_profile_revision(self):
        authority = self.f.authority().authorized_state()
        key = self.f.integration._recommendation_input_key(self.f.profile, authority)
        self.publish()
        other = self.f.integration._recommendation_input_key(self.f.profile, authority)
        self.assertNotEqual(key, other)
        self.revision = 'offline-revision-2'
        self.assertNotEqual(other, self.f.integration._recommendation_input_key(self.f.profile, self.f.authority().authorized_state()))

    def test_required_shortfall_cannot_be_waived_by_source_materiality(self):
        self.f.profile = duration_profile(2)
        from wahojobs import source_clause_materiality
        with patch.object(source_clause_materiality, 'generic_annotation', return_value={'classification': 'generic_behavior_only'}) as classifier:
            _, m = self.current()
        self.assertEqual(m['affirmative_fit_status'], 'conflicting')
        self.assertFalse(m['conditional_task_fit'])
        classifier.assert_not_called()

    def test_actual_list_and_exact_detail_share_stubbed_comparison_without_model_calls(self):
        self.source([COMPOUND], heading='Requirements')
        self.f.profile = profile('Customer support specialist', 6)
        self.publish()
        _, match = self.current()
        from wahojobs import authenticated_source_detail as detail
        from wahojobs.authenticated_variant_details import variant_detail_url
        observed = []
        original = detail.prepare_detail_display
        def capture(job, p):
            result = original(job, p); observed.append(result); return result
        with patch.object(detail, 'prepare_detail_display', side_effect=capture):
            response = self.f.get(variant_detail_url(match))
        self.assertEqual(response.status, 200)
        self.assertEqual(observed[0]['comparisons'][0]['supported_parts'], match['source_qualification_comparisons'][0]['supported_parts'])
        self.assertEqual(observed[0]['comparisons'][0]['components']['occupational_relevance']['semantic']['basis'], 'offline_labelled_stub')

    def test_optional_item_context_and_missing_authority_cannot_create_semantic_support(self):
        self.source([COMPOUND], heading='Requirements')
        self.f.profile = profile()
        ctx, m = self.current()
        self.assertFalse(m['conditional_task_fit'])
        self.f.profile = profile('Customer support specialist', 6)
        self.publish()
        self.revision = None
        _, m = self.current()
        self.assertFalse(m['source_qualification_comparisons'][0]['supported_parts'])

    def test_professional_item_context_is_explanation_only(self):
        from tests.test_background_item_explanation import base, reported
        self.f.profile = reported(base(), ['professional'])
        self.source([CLAUSE], heading='Requirements')
        _, m = self.current()
        self.assertFalse(m['source_qualification_comparisons'][0]['supported_parts'])
        self.assertFalse(m['conditional_task_fit'])
        self.assertEqual(m['source_qualification_comparisons'][0]['components']['required_duration']['status'], 'unresolved')


class ProfessionalAlternativeTests(unittest.TestCase):
    """Accepted source groups, not manually assigned admission outcomes."""
    setUp = BackgroundIntegrationTests.setUp
    source = BackgroundIntegrationTests.source
    role = BackgroundIntegrationTests.role
    current = BackgroundIntegrationTests.current
    inputs = BackgroundIntegrationTests.inputs
    publish = BackgroundIntegrationTests.publish
    alternative = 'Alternatively, a degree in marketing is sufficient.'

    def candidate(self, years='2', degree=None):
        from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2, _material_field_paths
        from wahojobs.profiles.education_entries import project_education_entries_to_legacy
        p = duration_profile(constraint=f'I have a total of {years} years of professional experience in marketing')
        if degree:
            if degree == 'no_degree':
                p['education']['education_level'] = 'no_degree'
            else:
                entries = [dict(kind='bachelor', qualification='Bachelor degree in ' + degree, field=degree,
                                institution='', status='completed', completion_year=None)]
                p['education'] = dict(project_education_entries_to_legacy(entries), entries=entries)
            # This synthetic declaration confirms every material fixture field,
            # using the canonical contract's own coverage paths.
            p['provenance']['field_sources'] = []
            for path in _material_field_paths(p):
                confirmed(p, path)
        self.f.profile = validate_canonical_profile_v2(p)
        return self.f.profile

    def group(self, match):
        return match['source_qualification_comparisons'][0]['components']['qualifying_routes']

    def assert_rejected_after_section_probe(self, match):
        self.assertEqual(match['affirmative_fit_status'], 'conflicting')
        self.assertFalse(match['conditional_task_fit'])
        moved = deepcopy(match); moved['preview_section'] = 'also_worth_reviewing'
        context = dict(matches={'also_worth_reviewing': [moved]})
        self.assertEqual(browser._conditional_presentation_matches(context), [])
        self.assertEqual(browser._primary_presentation_matches(context), [])

    def test_reported_alternative_across_bullets_paragraphs_and_sentences(self):
        self.candidate()
        for clauses, extra in [([CLAUSE, self.alternative], ''),
                               ([CLAUSE], '\n\n' + self.alternative),
                               ([CLAUSE + '. ' + self.alternative], '')]:
            with self.subTest(clauses=clauses, extra=extra):
                body = self.source(clauses, heading='Requirements', extra=extra)
                _, match = self.current()
                group = self.group(match)
                self.assertEqual(group['status'], 'unresolved')
                self.assertEqual([r['status'] for r in group['routes']], ['contradicted', 'not_established'])
                self.assertEqual(match['affirmative_fit_status'], 'uncertain')
                self.assertTrue(match['conditional_task_fit'])
                self.assertEqual(match['preview_section'], 'explore_only')
                for row in match['source_qualification_comparisons']:
                    self.assertEqual(row['status'], 'unresolved')
                    self.assertIn(row['source']['quote'], body)
                    self.assertEqual(row['source']['heading'], 'Requirements')
                self.assertEqual(match['source_qualification_comparisons'][0]['components']['required_duration']['status'], 'contradicted')

    def test_reported_separate_experience_waiver_remains_unknown(self):
        self.candidate()
        self.source([CLAUSE, 'The experience requirement is optional for applicants with a marketing degree.'], heading='Requirements')
        _, match = self.current()
        self.assertEqual(self.group(match)['status'], 'unresolved')
        self.assertTrue(match['conditional_task_fit'])
        self.assertFalse(match['affirmative_fit']['conflicting_requirements'])

    def test_supported_degree_satisfies_only_the_group(self):
        self.candidate(degree='marketing')
        self.source([CLAUSE, self.alternative, 'Proficiency in Python'], heading='Requirements')
        _, match = self.current()
        rows = match['source_qualification_comparisons']
        self.assertEqual([r['status'] for r in rows[:2]], ['supported', 'supported'])
        self.assertEqual(self.group(match)['routes'][0]['status'], 'contradicted')
        self.assertEqual(rows[0]['components']['required_duration']['status'], 'contradicted')
        self.assertEqual(rows[0]['components']['other_qualifications'][0]['kind'], 'tools')
        self.assertNotEqual(rows[2]['status'], 'supported')
        self.assertTrue(match['conditional_task_fit'])
        self.assertFalse(match['primary_recommendation_eligible'])
        self.assertEqual(match['preview_section'], 'explore_only')

    def test_every_route_contradicted_is_rejected_by_the_existing_consumer(self):
        self.candidate(degree='no_degree')
        self.source([CLAUSE, self.alternative], heading='Requirements')
        _, match = self.current()
        self.assertEqual([r['status'] for r in self.group(match)['routes']], ['contradicted', 'contradicted'])
        self.assert_rejected_after_section_probe(match)

    def test_degree_denial_is_not_an_independent_conjunct_when_experience_is_unknown(self):
        self.candidate(years='5', degree='no_degree')
        self.source([CLAUSE, self.alternative], heading='Requirements')
        _, match = self.current()
        self.assertEqual([r['status'] for r in self.group(match)['routes']], ['unresolved', 'contradicted'])
        self.assertTrue(match['conditional_task_fit'])
        self.assertFalse(match['affirmative_fit']['conflicting_requirements'])

    def test_unrelated_alternative_and_preferred_degree_do_not_waive_required_years(self):
        self.candidate(degree='marketing')
        for clauses, extra in [([CLAUSE, 'Proficiency in Python', self.alternative], ''),
                               ([CLAUSE], '\n\n## Preferred qualifications\n\n' + self.alternative),
                               ([CLAUSE, 'A degree in marketing is preferred.'], ''),
                               ([CLAUSE, 'Alternatively, a degree in marketing is preferred by the employer.'], ''),
                               ([CLAUSE, 'Alternatively, a degree in marketing is not sufficient.'], ''),
                               ([CLAUSE, 'The experience requirement is mandatory for applicants with a marketing degree.'], ''),
                               ([CLAUSE], '\n\n## Equipment\n\n' + self.alternative)]:
            with self.subTest(clauses=clauses, extra=extra):
                self.source(clauses, heading='Requirements', extra=extra)
                _, match = self.current()
                self.assertNotIn('qualifying_routes', match['source_qualification_comparisons'][0]['components'])
                self.assert_rejected_after_section_probe(match)

    def test_independent_mandatory_education_is_still_decisive(self):
        self.candidate(years='5', degree='no_degree')
        self.source([CLAUSE, self.alternative, 'PhD in biology'], heading='Requirements')
        _, match = self.current()
        self.assertEqual(self.group(match)['status'], 'unresolved')
        self.assertEqual(match['source_qualification_comparisons'][2]['status'], 'contradicted')
        self.assert_rejected_after_section_probe(match)

    def test_independent_mandatory_tool_conflict_survives_supported_alternative(self):
        from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2
        p = self.candidate(degree='marketing')
        p['constraints']['hard_constraints'].append('I have no experience with Python')
        confirmed(p, 'constraints.hard_constraints[1]')
        self.f.profile = validate_canonical_profile_v2(p)
        self.source([CLAUSE, self.alternative, 'Experience with Python'], heading='Requirements')
        _, match = self.current()
        self.assertEqual(self.group(match)['status'], 'supported')
        self.assert_rejected_after_section_probe(match)

    def test_ambiguous_local_route_and_waiver_target_create_no_support(self):
        self.candidate(degree='marketing')
        for clauses in ([CLAUSE, 'Alternatively, a degree in marketing may be sufficient.'],
                        [CLAUSE, 'Alternatively, a doctorate in marketing is sufficient.'],
                        [CLAUSE, 'Alternatively, a degree in marketing or biology is sufficient.'],
                        [CLAUSE, 'Two years managing sales teams.',
                         'The experience requirement is optional for applicants with a marketing degree.'],
                        [CLAUSE, '5+ years of relevant professional experience in sales',
                         'The experience requirement is optional for applicants with a marketing degree.']):
            with self.subTest(clauses=clauses):
                self.source(clauses, heading='Requirements')
                _, match = self.current()
                self.assertEqual(self.group(match)['operator'], 'unresolved')
                self.assertEqual(self.group(match)['status'], 'unresolved')
                self.assertFalse(match['affirmative_fit']['conflicting_requirements'])

    def test_source_context_change_invalidates_preparation_and_saved_recommendation(self):
        self.candidate(degree='marketing')
        source, card, context, request, output = self.publish()
        _, match = self.current(); run = self.f.last_run()
        self.assert_rejected_after_section_probe(match)
        old_inventory = self.f.integration._inventory_commit_token()
        self.source([CLAUSE, self.alternative], heading='Requirements')
        self.assertNotEqual(old_inventory, self.f.integration._inventory_commit_token())
        with patch.object(browser.profile_preview, 'query_preview_rows', wraps=browser.profile_preview.query_preview_rows) as query:
            _, match = self.current('/find-matches?run=' + run.match_run_id)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.group(match)['status'], 'supported')
        _, other_card, _, other_request = self.inputs()
        self.assertEqual(card['comparisons'][0]['source']['quote'], other_card['comparisons'][0]['source']['quote'])
        self.assertNotEqual(request['request_id'], other_request['request_id'])
        self.assertIsNone(self.store.lookup(other_request))
        self.publish(); _, _ = self.current(); run = self.f.last_run()
        self.source([CLAUSE, 'Alternatively, a degree in sales is sufficient.'], heading='Requirements')
        _, match = self.current('/find-matches?run=' + run.match_run_id)
        self.assertEqual(self.group(match)['status'], 'unresolved')
        self.assertNotIn('semantic', match['source_qualification_comparisons'][0]['components']['occupational_relevance'])

    def test_saved_result_replacement_still_invalidates_without_inventory_change(self):
        self.source([COMPOUND], heading='Requirements')
        self.f.profile = profile('Customer support specialist', 6)
        _, _, _, request, output = self.publish()
        _, match = self.current(); run = self.f.last_run()
        token = self.f.integration._inventory_commit_token()
        for relation, expected in [('not_established', False), ('supported_partial', True)]:
            self.store.publish(request, dict(output, relation=relation))
            with patch.object(browser.profile_preview, 'query_preview_rows', wraps=browser.profile_preview.query_preview_rows) as query:
                ctx, match = self.current('/find-matches?run=' + run.match_run_id)
            self.assertEqual(query.call_count, 1)
            self.assertEqual(match['conditional_task_fit'], expected)
            self.assertEqual(self.f.integration._inventory_commit_token(), token)
            self.assertEqual([m['job_id'] for m in browser._conditional_presentation_matches(ctx)],
                             [7003] if expected else [])
            self.assertEqual(browser._primary_presentation_matches(ctx), [])
            run = self.f.last_run()


class ExactDurationTests(unittest.TestCase):
    def test_reported_year_and_month_bounds_remain_exact_under_ambient_contexts(self):
        from decimal import localcontext, Inexact, Rounded
        from fractions import Fraction
        for precision in (1, 2, 28, 80):
            with localcontext() as context:
                context.prec = precision; context.traps[Inexact] = context.traps[Rounded] = True
                for amount, unit in [('4.999999999999999999999999999999', 'years'),
                                     ('59.999999999999999999999999999999', 'months'), ('4.99', 'years')]:
                    with self.subTest(precision=precision, amount=amount, unit=unit):
                        p = duration_profile(constraint=f'I have at most {amount} {unit} of professional experience in marketing')
                        result = compare_duration(CLAUSE, p)
                        self.assertEqual(result['status'], 'contradicted')
                        bound = result['scope_results'][0]['upper_months']
                        self.assertEqual(Fraction(bound), Fraction(amount) * (12 if unit == 'years' else 1))
                        self.assertLess(Fraction(bound), 60)

    def test_exact_totals_lower_and_upper_thresholds_with_compatible_units(self):
        from decimal import localcontext
        for precision in (1, 28):
            with localcontext() as context:
                context.prec = precision
                for unit, values in [('years', ('4.999999999999999999999999999999', '5', '5.000000000000000000000000000001')),
                                     ('months', ('59.99999999999999999999999999999', '60', '60.00000000000000000000000000001'))]:
                    for bound, statuses in [('a total of', ('contradicted', 'supported', 'supported')),
                                            ('exactly', ('contradicted', 'supported', 'supported')),
                                            ('at least', ('unresolved', 'supported', 'supported')),
                                            ('at most', ('contradicted', 'unresolved', 'unresolved'))]:
                        for amount, expected in zip(values, statuses):
                            with self.subTest(precision=precision, unit=unit, bound=bound, amount=amount):
                                p = duration_profile(constraint=f'I have {bound} {amount} {unit} of professional experience in marketing')
                                for clause in (CLAUSE, '60 months of relevant professional experience in marketing'):
                                    self.assertEqual(compare_duration(clause, p)['status'], expected)

    def test_canonical_and_comparison_json_round_trips_preserve_exact_bound(self):
        import json
        from fractions import Fraction
        from wahojobs.profiles.canonical_v2 import canonical_profile_v2_json_bytes, parse_canonical_profile_v2_json
        amount = '4.999999999999999999999999999999'
        p = duration_profile(constraint=f'I have at most {amount} years of professional experience in marketing')
        loaded = parse_canonical_profile_v2_json(canonical_profile_v2_json_bytes(p))
        self.assertEqual(loaded['constraints'], p['constraints'])
        result = json.loads(json.dumps(compare_duration(CLAUSE, loaded), allow_nan=False))
        self.assertEqual(result['status'], 'contradicted')
        self.assertEqual(Fraction(result['scope_results'][0]['upper_months']), Fraction(amount) * 12)
        aggregate = parse_canonical_profile_v2_json(canonical_profile_v2_json_bytes(duration_profile(4.99)))
        self.assertEqual(compare_duration(CLAUSE, aggregate)['scope_results'][0]['upper_months'], '59.88')

    def test_numeric_reproductions_reject_through_comparison_consumer_and_selector(self):
        from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2
        for amount, unit in [('4.999999999999999999999999999999', 'years'), ('59.999999999999999999999999999999', 'months')]:
            t = BackgroundIntegrationTests('test_required_shortfall_is_rejected_by_consumer_not_section_barrier')
            t.setUp()
            try:
                t.f.integration._professional_background_evidence = None
                t.f.profile = validate_canonical_profile_v2(duration_profile(constraint=f'I have at most {amount} {unit} of professional experience in marketing'))
                _, match = t.current()
                self.assertEqual(match['source_qualification_comparisons'][0]['status'], 'contradicted')
                self.assertEqual(match['affirmative_fit_status'], 'conflicting')
                self.assertFalse(match['conditional_task_fit'])
                moved = deepcopy(match); moved['preview_section'] = 'also_worth_reviewing'
                self.assertEqual(browser._conditional_presentation_matches(dict(matches={'also_worth_reviewing': [moved]})), [])
            finally:
                t.doCleanups()

    def test_unknown_incomplete_and_invalid_duration_inputs_cannot_create_eligibility(self):
        from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2, CanonicalProfileV2Error
        for p in (duration_profile(), duration_profile(constraint='I worked 2 years in marketing at Acme'),
                  duration_profile(constraint='Partial history: 2 years of professional experience in marketing')):
            self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')
        for value in (float('nan'), float('inf'), -1, 81, 10 ** 500, 4.999):
            p = duration_profile(value)
            self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')
            with self.assertRaises(CanonicalProfileV2Error):
                validate_canonical_profile_v2(p)
        for amount in ('NaN', 'Infinity', '-2', '9' * 5000):
            p = duration_profile(constraint=f'I have at most {amount} years of professional experience in marketing')
            self.assertEqual(compare_duration(CLAUSE, p)['status'], 'unresolved')
        self.assertEqual(compare_duration('9' * 5000 + ' years of relevant professional experience in marketing', duration_profile(2))['status'], 'unresolved')


if __name__ == '__main__':
    unittest.main()
