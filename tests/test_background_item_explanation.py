"""Synthetic context contrasts; production matching/rendering, no database or HTTP."""
from copy import deepcopy
from hashlib import sha256
import socket
import sqlite3
import unittest
from unittest.mock import patch

from tests.test_confirmed_activity_matching import candidate
from tests.test_conditional_professional_background import MARKETING, reviewed
from tests.test_optional_item_experience import enriched, item
from tests.test_profile_to_matches_preview import synthetic_opportunity_row, SYNTHETIC_EVALUATED_AT
from scripts import profile_to_matches_preview as preview
from wahojobs import authenticated_card_evidence as cards, authenticated_profile_matches as browser
from wahojobs.authenticated_source_detail import prepare_detail_display, render_authenticated_job_page
from wahojobs.authenticated_variant_details import variant_detail_url
from wahojobs.candidate_condition_comparisons import _professional_background, render_comparisons
from wahojobs.matching import accepted_tasks
from wahojobs.matching.source_task_fit import apply_source_task_fit
from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2, project_v2_to_matcher_v1
from wahojobs.profiles.item_experience import PREFIX, linked
from wahojobs.public_job_page import PUBLIC_JOB_STATE_LIVE


def base():
    return reviewed(candidate(['Model output evaluation', 'Marketing', 'Image annotation']))


def reported(profile, contexts, label='Marketing'):
    return enriched(profile, [item(label, 'specialties', contexts=contexts, autonomy='unknown', months=None)])


def opportunity(heading='Qualifications'):
    row = synthetic_opportunity_row('Portuguese AI Data Reviewer', expertise='', location='Remote - Brazil')
    row['external_id'] = 'synthetic-background-context'
    body = 'Key Responsibilities\n\nEvaluate AI outputs.\n\n' + heading + '\n\n' + MARKETING
    source = dict(row, body=body, body_format='text/plain', metadata_json='{}',
                  content_provider=row['source_slug'], content_external_id=row['external_id'],
                  source_url=row['url'], last_captured_at=SYNTHETIC_EVALUATED_AT.isoformat(),
                  material_content_sha256=sha256(body.encode()).hexdigest())
    return row, source


def match(profile, heading='Qualifications'):
    row, source = opportunity(heading)
    # Substitute only the accepted-source read. Preparation, scoring, guards,
    # background admission and presentation selection are production functions.
    with patch.object(accepted_tasks, 'load_card_sources', return_value={row['job_id']: source}):
        rows = accepted_tasks.project_accepted_tasks(None, [row])
    projected = project_v2_to_matcher_v1(profile, matcher_profile_id='synthetic-background-context')
    context = preview.build_preview_context_from_canonical_rows(
        projected, inventory_rows=rows, metadata_overlay_status={}, evaluated_at=SYNTHETIC_EVALUATED_AT)
    context['matches'] = {k: [apply_source_task_fit(m, source, profile) for m in ms]
                          for k, ms in context['matches'].items()}
    selected = next(m for ms in context['matches'].values() for m in ms)
    return projected, context, selected, source


class BackgroundItemExplanationTests(unittest.TestCase):
    def setUp(self):
        for target in ((sqlite3, 'connect'), (socket.socket, 'connect')):
            guard = patch.object(*target, side_effect=AssertionError('No database or network access'))
            guard.start(); self.addCleanup(guard.stop)

    def test_four_schema_valid_contrasts_preserve_complete_matching_and_admission(self):
        original = base()
        profiles = [reported(original, ['professional']), reported(original, ['study', 'projects']),
                    deepcopy(original), reported(original, ['professional'], 'Image annotation')]
        before = deepcopy(profiles)
        baseline = _professional_background(MARKETING, original)
        outcomes, projections, messages = [], [], []
        for p in profiles:
            self.assertEqual(validate_canonical_profile_v2(p), p)
            self.assertTrue(all(linked(p, d) for d in p['experience'].get('item_details', [])))
            stripped = deepcopy(p); stripped['experience'].pop('item_details', None)
            stripped['provenance']['field_sources'] = [r for r in stripped['provenance']['field_sources']
                                                       if not r['field_path'].startswith(PREFIX)]
            self.assertEqual(stripped, original)
            self.assertEqual(_professional_background(MARKETING, p), baseline)
            display = _professional_background(MARKETING, p, include_item_experience=True)
            self.assertEqual((display[0], display[3]), (baseline[0], baseline[3]))
            self.assertEqual(display[2][:len(baseline[2])], baseline[2])
            messages.append(display[1])
            projected, context, result, source = match(p)
            projections.append(projected); outcomes.append(context['matches'])
            self.assertEqual(result['affirmative_fit_status'], 'uncertain')
            self.assertFalse(result['affirmative_fit']['conflicting_requirements'])
            self.assertEqual(browser._primary_presentation_matches(context), [])
            self.assertEqual(browser._conditional_presentation_pool(context), [])
            packet = cards.prepare_card_evidence(result, source, p, include_item_experience=True)
            admission_packet = cards.prepare_card_evidence(result, source, p)
            unchanged = deepcopy(packet)
            for displayed, admitted in zip(unchanged['comparisons'], admission_packet['comparisons']):
                for key in ('message', 'profile_facts'):
                    if key in admitted:
                        displayed[key] = admitted[key]
            self.assertEqual(unchanged, admission_packet)
            comparison = packet['comparisons'][0]
            self.assertEqual(comparison['status'], 'unresolved')
            self.assertEqual(comparison['source']['quote'], MARKETING)
            self.assertEqual(comparison['source']['job_id'], source['job_id'])
        self.assertTrue(all(p == projections[0] for p in projections))
        self.assertTrue(all(c == outcomes[0] for c in outcomes))
        self.assertEqual(messages[0], 'You’ve reported using Marketing in your work. Check whether that experience covers the activities described below.')
        self.assertEqual(messages[1], 'You’ve listed studies and projects involving Marketing. Experience in a related role isn’t specified.')
        fallback = 'Your profile lists Marketing, but doesn’t specify whether you’ve used it professionally.'
        self.assertEqual(messages[2:], [fallback, fallback])
        self.assertEqual(profiles, before)

    def test_unconfirmed_unknown_and_unlinked_details_do_not_supply_context(self):
        p = base(); baseline = _professional_background(MARKETING, p, include_item_experience=True)
        for mode in ('unconfirmed', 'unknown', 'unlinked'):
            changed = reported(p, [] if mode == 'unknown' else ['professional'])
            if mode == 'unconfirmed':
                for ref in changed['provenance']['field_sources']:
                    if ref['field_path'].startswith(PREFIX):
                        ref.update(explicit=False, source_kind='external_import')
                self.assertEqual(validate_canonical_profile_v2(changed), changed)
            if mode == 'unlinked':
                # Deliberately invalid linkage: even a direct comparator call
                # must not borrow the still-stored context after label removal.
                changed['experience']['item_details'][0]['label'] = 'Unlisted marketing'
            self.assertEqual(_professional_background(MARKETING, changed, include_item_experience=True), baseline)

    def test_existing_role_and_conflicting_evidence_are_not_overridden(self):
        role = candidate(['Marketing', 'Image annotation'])
        role['experience']['recent_roles'] = ['Content marketing specialist']
        role = reviewed(role)
        baseline = _professional_background(MARKETING, role)
        self.assertTrue(baseline[3])
        for contexts in (['study', 'projects'], ['professional']):
            self.assertEqual(_professional_background(MARKETING, reported(role, contexts),
                                                      include_item_experience=True), baseline)
        for activities in (['Marketing', 'Image annotation'], ['Image annotation']):
            p = candidate(activities)
            p['constraints']['hard_constraints'] = ['No experience in marketing']
            p = reviewed(p)
            baseline = _professional_background(MARKETING, p)
            self.assertEqual(baseline[0], 'unresolved' if 'Marketing' in activities else 'contradicted')
            self.assertIn('conflicting' if 'Marketing' in activities else 'do not have', baseline[1])
            changed = reported(p, ['professional'], activities[0])
            self.assertEqual(_professional_background(MARKETING, changed, include_item_experience=True), baseline)

    def test_autonomy_and_duration_are_not_explanatory_inferences(self):
        p = reported(base(), ['professional'])
        expected = _professional_background(MARKETING, p, include_item_experience=True)
        d = deepcopy(p['experience']['item_details']); d[0].update(autonomy='complex', months=120)
        changed = enriched(base(), d)
        result = _professional_background(MARKETING, changed, include_item_experience=True)
        self.assertEqual((result[0], result[1], result[3]), (expected[0], expected[1], expected[3]))
        self.assertNotIn('120', result[1]); self.assertNotIn('proficien', result[1])

    def test_study_context_renders_without_professional_claim_or_admission_change(self):
        p = reported(base(), ['study', 'projects'])
        _, context, selected, source = match(p, 'Preferred Qualifications')
        context['_card_evidence'] = {selected['job_id']: cards.prepare_card_evidence(
            selected, source, p, include_item_experience=True)}
        html = browser._render_match_results(context, inventory_count=1)
        self.assertEqual(html.count('You’ve listed studies and projects involving Marketing. Experience in a related role isn’t specified.'), 1)
        self.assertEqual([m['job_id'] for m in browser._primary_presentation_matches(context)], [source['job_id']])

    def test_single_context_and_item_names_are_not_expanded(self):
        for label in ('Marketing', 'B2B product marketing'):
            p = reviewed(candidate([label]))
            for context, wording, absent in (('study', 'studies', 'projects'), ('projects', 'projects', 'studies')):
                result = _professional_background(MARKETING, reported(p, [context], label), include_item_experience=True)
                self.assertEqual(result[1], f'You’ve listed {wording} involving {label}. Experience in a related role isn’t specified.')
                self.assertNotIn(absent, result[1])

    def test_requirement_label_keeps_original_source_and_references(self):
        p = reported(base(), ['professional'])
        _, _, selected, source = match(p)
        packet = cards.prepare_card_evidence(selected, source, p, include_item_experience=True)
        before = deepcopy(packet)
        html = cards.render_conditions(packet, 'qa')
        self.assertIn('Your background', html)
        self.assertIn('Employer requirement', html)
        self.assertNotIn('Employer preference', html)
        self.assertEqual(html.count(MARKETING), 1)
        # The same block renderer is used in the exact-variant detail page.
        detail_block = render_comparisons(packet, block_reference=packet['conditions'][0]['reference'])
        self.assertIn('Employer requirement', detail_block)
        self.assertEqual(packet, before)
        self.assertEqual(packet['comparisons'][0]['source']['quote'], MARKETING)

    def test_prefix_removal_is_local_to_explicit_background_labels(self):
        _, _, selected, source = match(base(), 'Preferred Qualifications')
        packet = cards.prepare_card_evidence(selected, source, base(), include_item_experience=True)
        before = deepcopy(packet)
        self.assertIn('Preferred:', render_comparisons(packet))
        ref = packet['conditions'][0]['reference']
        self.assertNotIn('Preferred:', render_comparisons(packet, block_reference=ref))
        self.assertEqual(packet, before)
        other = deepcopy(packet['comparisons'][0]); other['kind'] = 'tools'
        packet['comparisons'].append(other)
        self.assertIn('Preferred:', render_comparisons(packet, block_reference=ref))
        self.assertNotIn('Employer preference', render_comparisons(packet, block_reference=ref))

    def test_normal_admitted_card_and_detail_renderers_show_context_once(self):
        p = reported(base(), ['professional'])
        projected, context, selected, source = match(p, 'Preferred Qualifications')
        old_projection, old_context, _, _ = match(base(), 'Preferred Qualifications')
        self.assertEqual(projected, old_projection)
        self.assertEqual(context['matches'], old_context['matches'])
        self.assertEqual([m['job_id'] for m in browser._primary_presentation_matches(context)], [source['job_id']])
        packet = cards.prepare_card_evidence(selected, source, p, include_item_experience=True)
        context['_card_evidence'] = {selected['job_id']: packet}
        html = browser._render_match_results(context, inventory_count=1)
        explanation = 'You’ve reported using Marketing in your work. Check whether that experience covers the activities described below.'
        self.assertEqual(html.count(explanation), 1)
        self.assertIn('Your background', html)
        self.assertIn('Employer preference', html)
        self.assertNotIn('Preferred:', html)
        self.assertEqual(html.count(MARKETING), 1)
        self.assertIn('Qualifications &amp; conditions', html)
        self.assertIn(variant_detail_url(selected).replace('&', '&amp;'), html)
        job = dict(job_id=source['job_id'], canonical_opportunity_id=source['canonical_opportunity_id'],
                   external_id=source['external_id'], official_url=source['url'], company_slug=source['source_slug'],
                   company_name='Synthetic', source_title=source['title'], source_location=source['location'],
                   rich_provider=source['source_slug'], rich_external_id=source['external_id'],
                   rich_source_url=source['source_url'], rich_body=source['body'], rich_body_format=source['body_format'],
                   rich_metadata_json='{}', material_content_sha256=source['material_content_sha256'],
                   last_captured_at=source['last_captured_at'], public_state=PUBLIC_JOB_STATE_LIVE,
                   job_is_active=True, canonical_is_active=True, _authenticated_recommendation=selected)
        detail_packet = prepare_detail_display(job, p)
        self.assertEqual(detail_packet['comparisons'], packet['comparisons'])
        detail = render_authenticated_job_page(job, profile=p, navigation='')
        self.assertEqual(detail.count(explanation), 1)
        self.assertIn('Your background', detail)
        self.assertIn('Employer preference', detail)
        self.assertNotIn('Preferred:', detail)
        self.assertEqual(detail.count(MARKETING), 1)
        self.assertIn(source['url'], detail)
        self.assertIn('Apply on company site', detail)
        # Exact source identity remains a prerequisite for any explanation.
        job['rich_external_id'] = 'another-variant'
        self.assertIsNone(prepare_detail_display(job, p))


if __name__ == '__main__':
    unittest.main()
