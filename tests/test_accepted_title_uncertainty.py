"""Accepted source qualifications remain distinct from title and career totals."""
from copy import deepcopy
import unittest

from tests import test_accepted_task_matching as task_support
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_confirmed_activity_matching import candidate, v2
from wahojobs import authenticated_profile_matches as browser
from wahojobs.candidate_condition_comparisons import compare_conditions, _professional_background
from wahojobs.matching.accepted_tasks import needs_accepted_task_comparison
from wahojobs.profiles.canonical import field_sources_for_profile


BODY = """## About the role

We are hiring expert Evaluators in **Customer success / support operations** to review and assess AI-generated work products (documents, spreadsheets, and slide decks) for accuracy, rigor, and domain quality. You will apply deep subject-matter expertise to grade outputs.

This is a remote, hourly engagement.

## Requirements (must have)

1. **5+ years of relevant professional experience in Customer success / support operations.**

2. **Native or professional fluency in English.**

3. **Highly proficient in Microsoft Office and Google Workspace, especially Slides** (Google Slides / PowerPoint).

## Preferred (nice to have)

- Advanced degree (Master's or higher) from a reputable institution.

## What you'll do

- Evaluate AI-generated artifacts against domain-specific quality rubrics.

- Identify factual, aesthetic, and presentation errors.

- Provide clear, structured written feedback."""


def profile(role=None, total=None):
    result = candidate(['Data annotation', 'Model output evaluation'])
    result['experience']['recent_roles'] = [role] if role else []
    result['experience']['total_years'] = total
    result['provenance']['field_sources'] = field_sources_for_profile(result, 'user_confirmation', explicit=True)
    return v2(result)


class AcceptedTitleComparisonTests(unittest.TestCase):
    role = task_support.AcceptedTaskMatchingTests.role
    source = task_support.AcceptedTaskMatchingTests.source
    current = task_support.AcceptedTaskMatchingTests.current
    match = task_support.AcceptedTaskMatchingTests.match

    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.f.profile = profile()
        self.role('Customer success / support operations Evaluator')
        self.source(BODY)

    def test_generic_and_practitioner_reach_all_exact_requirements_without_promotion(self):
        for role, years in ((None, None), ('Customer support specialist', 6)):
            with self.subTest(role=role):
                self.f.profile = profile(role, years)
                _, _, context = self.current()
                match = self.match(context)
                rows = match['source_qualification_comparisons']
                self.assertEqual(len(rows), 4)
                self.assertEqual([r['modality'] for r in rows], ['required'] * 3 + ['preferred'])
                self.assertEqual(rows[0]['kind'], 'professional_background')
                self.assertEqual(rows[0]['supported_parts'], [])
                self.assertIn('5+ relevant professional years', rows[0]['message'])
                self.assertIn('relationship between the named source domains remains unresolved', rows[0]['message'])
                self.assertEqual(rows[0]['status'], 'unresolved' if role else 'not_established')
                if role:
                    self.assertIn('6 total career years do not establish', rows[0]['message'])
                facts = {f['field_path']: f for f in rows[0]['profile_facts']}
                self.assertEqual(facts['experience.total_years']['value'], years)
                self.assertEqual(facts['experience.years_by_domain']['value'], [])
                self.assertEqual(facts['experience.recent_roles']['value'], [role] if role else [])
                self.assertIn('Native or professional fluency in English.', rows[1]['source']['quote'])
                self.assertIn('Microsoft Office and Google Workspace', rows[2]['source']['quote'])
                self.assertEqual([r['status'] for r in rows[1:]], ['unresolved'] * 3)
                self.assertTrue(all(r['source']['job_id'] == 7003 for r in rows))
                self.assertTrue(all(r['source']['source_hash'] == rows[0]['source']['source_hash'] for r in rows))
                self.assertFalse(match['primary_recommendation_eligible'])
                self.assertFalse(match['conditional_task_fit'])
                self.assertEqual(match['affirmative_fit_status'], 'uncertain')
                self.assertFalse(match['affirmative_fit']['conflicting_requirements'])
                self.assertNotIn(7003, {m['job_id'] for m in browser._primary_presentation_matches(context) + browser._conditional_presentation_matches(context)})
                self.assertNotIn('source_qualification_comparisons', self.match(context, 7006))

    def test_other_unmodeled_titles_reuse_required_background_comparison(self):
        self.role('Uncatalogued assessment position')
        self.source("Responsibilities\n\nEvaluate AI outputs.\n\nRequirements\n\nHands-on experience in a marketing role")
        self.f.profile = profile('Marketing specialist', 6)
        _, _, context = self.current()
        match = self.match(context)
        rows = match['source_qualification_comparisons']
        self.assertEqual(rows[0]['kind'], 'professional_background')
        self.assertEqual(rows[0]['supported_parts'], ['related role: marketing'])
        self.assertEqual(rows[0]['status'], 'unresolved')
        self.assertTrue(match['conditional_task_fit'])
        self.assertFalse(match['primary_recommendation_eligible'])

    def test_no_task_evidence_or_other_requirements_cannot_enter_new_review_path(self):
        base = dict(accepted_task_fit={'facts': ['bound duty']}, eligible_for_personalized=True,
                    job_id=7003, canonical_opportunity_id=7003, url='https://example.test/role',
                    affirmative_fit_status='uncertain', actionability_cap_reasons=[],
                    affirmative_fit=dict(status='uncertain', missing_requirements=[], conflicting_requirements=[],
                                         unmodeled_requirements=['Title-defining role or specialization']))
        self.assertTrue(needs_accepted_task_comparison(base))
        for change in ({'accepted_task_fit': None}, {'eligible_for_personalized': False},
                       {'professional_domain_hard_gate_applied': True}, {'job_is_active': False},
                       {'location_eligibility_status': 'incompatible'},
                       {'actionability_cap_reasons': ['mandatory_language_proficiency_conflict']},
                       {'actionability_cap_reasons': ['unsupported_specialization']}):
            self.assertFalse(needs_accepted_task_comparison(dict(base, **change)))
        for key in ('missing_requirements', 'conflicting_requirements'):
            changed = deepcopy(base)
            changed['affirmative_fit'][key] = ['Biology PhD']
            self.assertFalse(needs_accepted_task_comparison(changed))

    def test_reached_source_residence_conflict_and_waivers_are_retained(self):
        self.source(BODY + '\n\nRequirements\n\nMust be based in the United States.')
        _, _, context = self.current()
        match = self.match(context)
        self.assertFalse(match['primary_recommendation_eligible'])
        self.assertNotIn(7003, {m['job_id'] for m in browser._conditional_presentation_matches(context)})
        packet = dict(job_id=1, external_id='example', url='https://example.test/role', source_hash='hash',
                      captured_at='2026-09-10', caveats=[], kind='Advertised role', conditions=[dict(
                          heading='Requirements (must have)', reference='source block 1',
                          text='1. No relevant professional experience required.\n2. Native English preferred.')])
        rows = compare_conditions(packet, profile())
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['kind'], 'professional_background')
        self.assertEqual(rows[1]['modality'], 'conflicting')

    def test_duration_clause_reuses_professional_comparison_and_explicit_denials(self):
        clause = '5+ years of relevant professional experience in marketing'
        related = _professional_background(clause, profile('Marketing specialist', 6))
        self.assertEqual(related[0], 'unresolved')
        self.assertEqual(related[3], ['related role: marketing'])
        self.assertIn('6 total career years do not establish', related[1])
        for field in ('marketing', 'Customer success / support operations'):
            candidate_profile = candidate(['Model output evaluation'])
            candidate_profile['constraints']['hard_constraints'] = ['No professional experience in ' + field]
            candidate_profile['provenance']['field_sources'] = field_sources_for_profile(candidate_profile, 'user_confirmation', explicit=True)
            confirmed = v2(candidate_profile)
            result = _professional_background('5+ years of relevant professional experience in ' + field, confirmed)
            self.assertEqual(result[0], 'contradicted')
            self.assertEqual(result[3], [])
            confirmed['provenance']['field_sources'] = []
            self.assertNotEqual(_professional_background('5+ years of relevant professional experience in ' + field, confirmed)[0], 'contradicted')


class ConditionalSectionSelectionTests(unittest.TestCase):
    """Ordinary synthetic selector fixtures, not model observations or inventory validation."""

    @staticmethod
    def row(jid=1, score=12):
        ref = dict(job_id=jid, canonical_opportunity_id=jid, source_slug='synthetic-contract',
                   source_url=f'https://example.test/{jid}', material_content_sha256=f'material-{jid}')
        return dict(job_id=jid, canonical_opportunity_id=jid, source_slug=ref['source_slug'],
            url=ref['source_url'], source='Synthetic contract', display_title=f'Contract {jid:04d}', score=score,
            raw_product_section='explore_only', effective_product_section='explore_only', preview_section='explore_only',
            accepted_task_pre_review=dict(review_only=True, primary_admission_reasons=['affirmative_fit_uncertain'],
                sections=dict(raw_product_section='explore_only', effective_product_section='explore_only', preview_section='explore_only')),
            accepted_task_fit=dict(facts=['synthetic accepted task'], source_reference=ref),
            source_task_fit=dict(status='uncertain', source_reference=deepcopy(ref)),
            source_qualification_comparisons=[dict(kind='professional_background', modality='required',
                status='unresolved', supported_parts=['synthetic grounded occupational relation'],
                source=dict(job_id=jid, url=ref['source_url'], source_hash=ref['material_content_sha256']))],
            affirmative_fit_status='uncertain', primary_recommendation_eligible=False, conditional_task_fit=True,
            primary_admission_source='accepted_task_source_conditions',
            affirmative_fit=dict(missing_requirements=[], conflicting_requirements=[]),
            opportunity_trust_status='trusted', actionability_cap_reasons=[], score_components={})

    def allowed(self, row):
        from wahojobs.matching.accepted_tasks import is_conditional_section_candidate
        return is_conditional_section_candidate(row)

    def test_both_task_and_grounded_professional_support_are_required(self):
        row = self.row()
        self.assertTrue(self.allowed(row))
        self.assertFalse(self.allowed(dict(row, accepted_task_fit=None)))
        no_parts = deepcopy(row)
        no_parts['source_qualification_comparisons'][0]['supported_parts'] = []
        self.assertFalse(self.allowed(no_parts))
        self.assertFalse(self.allowed(dict(row, source_qualification_comparisons=[])))
        raw_label = dict(row, source_qualification_comparisons=[], relation='supported_partial')
        self.assertFalse(self.allowed(raw_label))

    def test_every_independent_restriction_blocks_only_the_new_exception(self):
        changes = [dict(opportunity_trust_status='stale_source'), dict(job_is_active=False),
            dict(canonical_is_active=False), dict(eligible_for_personalized=False),
            dict(location_eligibility_status='incompatible'), dict(actionability_cap_reasons=['unsupported_specialization']),
            dict(source_task_location_checks=[dict(status='unconfirmed')]),
            dict(source_language_checks=[dict(modality='required', status='contradicted')]),
            dict(primary_recommendation_eligible=True), dict(conditional_task_fit=False),
            dict(affirmative_fit_status='supported'), dict(primary_admission_source='other')]
        changes += [{key: True} for key in ('professional_domain_hard_gate_applied',
            'specialized_actionability_cap_applied', 'location_actionability_cap_applied',
            'preview_domain_hard_gate_applied', 'raw_professional_domain_hard_gate_applied')]
        changes += [dict(score_components={key: -1}) for key in
                    ('avoid_keyword_penalty', 'quality_gate_penalty', 'specialist_domain_penalty')]
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(self.allowed(dict(self.row(), **change)))
        for key in ('missing_requirements', 'conflicting_requirements'):
            row = self.row()
            row['affirmative_fit'][key] = ['Independent requirement']
            self.assertFalse(self.allowed(row))

    def test_earlier_independent_exploratory_reason_is_not_overridden(self):
        row = self.row()
        row['accepted_task_pre_review']['review_only'] = False
        self.assertFalse(self.allowed(row))
        for key in ('raw_product_section', 'effective_product_section', 'preview_section'):
            row = self.row()
            row['accepted_task_pre_review']['sections'][key] = 'excluded'
            self.assertFalse(self.allowed(row))
            self.assertFalse(self.allowed(dict(self.row(), **{key: 'excluded'})))

    def test_missing_pre_review_receipt_and_mismatched_source_fail_closed(self):
        self.assertFalse(self.allowed(dict(self.row(), accepted_task_pre_review=None)))
        for key, value in [('job_id', 2), ('canonical_opportunity_id', 2),
                           ('source_slug', 'other'), ('url', 'https://example.test/other')]:
            self.assertFalse(self.allowed(dict(self.row(), **{key: value})))
        row = self.row()
        row['source_qualification_comparisons'][0]['source']['source_hash'] = 'different'
        self.assertFalse(self.allowed(row))
        row = self.row()
        row['source_task_fit']['source_reference']['material_content_sha256'] = 'different'
        self.assertFalse(self.allowed(row))

    def test_every_required_background_and_contradiction_is_checked(self):
        row = self.row()
        second = deepcopy(row['source_qualification_comparisons'][0])
        second['supported_parts'] = []
        row['source_qualification_comparisons'].append(second)
        self.assertFalse(self.allowed(row))
        second.update(kind='degree', supported_parts=['Study'], status='contradicted')
        self.assertFalse(self.allowed(row))
        second['modality'] = 'preferred'
        self.assertTrue(self.allowed(row))

    def test_failed_duration_route_is_not_an_unconditional_group_veto(self):
        row = self.row()
        bg = row['source_qualification_comparisons'][0]
        bg.update(status='supported', components=dict(qualifying_routes=dict(operator='any_of', status='supported',
            routes=[dict(status='contradicted'), dict(status='supported')])))
        self.assertTrue(self.allowed(row))
        bg['status'] = 'contradicted'
        self.assertFalse(self.allowed(row))

    def test_existing_main_and_unrelated_conditional_routes_are_unchanged(self):
        ordinary = self.row(10)
        ordinary.update(preview_section='also_worth_reviewing', accepted_task_fit=None,
                        source_qualification_comparisons=[], accepted_task_pre_review=None)
        main = dict(self.row(11), preview_section='best_matches', affirmative_fit_status='supported',
                    primary_recommendation_eligible=True, conditional_task_fit=False)
        context = dict(matches=dict(best_matches=[main], also_worth_reviewing=[ordinary], explore_only=[self.row(12)]))
        self.assertEqual([r['job_id'] for r in browser._primary_presentation_matches(context)], [11])
        self.assertEqual([r['job_id'] for r in browser._conditional_presentation_matches(context)], [10, 12])
        self.assertNotIn('conditional_admission_source', browser._conditional_presentation_matches(context)[0])

    def test_competition_uses_existing_section_and_score_order(self):
        existing = dict(self.row(10, score=1), preview_section='also_worth_reviewing')
        context = dict(matches=dict(also_worth_reviewing=[existing], explore_only=[self.row(2, 50), self.row(3, 99)]))
        before = deepcopy(context)
        self.assertEqual([r['job_id'] for r in browser._conditional_presentation_pool(context)], [10, 3, 2])
        self.assertEqual(context, before)
        self.assertEqual(browser._conditional_presentation_matches(context)[1]['preview_section'], 'explore_only')

    def test_existing_display_cap_and_no_duplicate_main_conditional_presentation(self):
        rows = [self.row(jid, 100-jid) for jid in range(1, 16)]
        main = dict(self.row(99), canonical_opportunity_id=1, preview_section='best_matches',
                    affirmative_fit_status='supported', primary_recommendation_eligible=True, conditional_task_fit=False)
        context = dict(matches=dict(best_matches=[main], explore_only=rows))
        selected = browser._conditional_presentation_matches(context)
        self.assertEqual(len(selected), browser.MATCH_PRESENTATION_LIMIT)
        self.assertEqual([m['job_id'] for m in selected], list(range(2, 12)))
        self.assertFalse({m['canonical_opportunity_id'] for m in selected}
                         & {m['canonical_opportunity_id'] for m in browser._primary_presentation_matches(context)})

    def test_existing_section_bound_can_omit_an_otherwise_qualified_row(self):
        cap = browser.local_product.PREVIEW_MATCH_LIMIT
        existing = [dict(self.row(jid), preview_section='also_worth_reviewing') for jid in range(1, cap+1)]
        context = dict(matches=dict(also_worth_reviewing=existing, explore_only=[self.row(cap+1, 999)]))
        pool = browser._conditional_presentation_pool(context)
        self.assertEqual(len(pool), cap)
        self.assertNotIn(cap+1, [r['job_id'] for r in pool])

    def test_same_canonical_variant_cannot_inherit_another_variants_comparison(self):
        first = self.row(1)
        foreign = dict(first, job_id=2, url='https://example.test/2')
        self.assertFalse(self.allowed(foreign))
        context = dict(matches=dict(explore_only=[first, foreign]))
        self.assertEqual([m['job_id'] for m in browser._conditional_presentation_pool(context)], [1])


if __name__ == '__main__':
    unittest.main()
