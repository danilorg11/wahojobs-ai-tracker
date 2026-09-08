"""Captured linguistic tasks plus synthetic counterexamples; no new labels."""
from copy import deepcopy
import json
from pathlib import Path
from contextlib import closing
import sqlite3
from unittest.mock import patch
import unittest

from tests.test_affirmative_fit_evidence import profile, projected
from wahojobs.matching.source_task_fit import apply_source_task_fit, _task
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_card_evidence import prepare_card_evidence, _source_text
from wahojobs.authenticated_variant_details import find_presented_variant, variant_detail_url
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_profile_preference_model import with_preference_model

SOURCES = json.loads((Path(__file__).parent / 'fixtures/language_task_source_examples.json').read_text(encoding='utf-8'))


def candidate(*, roles=(), skills=()):
    return dict(experience={'recent_roles': list(roles)}, skills={'normalized': list(skills)},
                education={'degrees': []}, location={'country': 'Brazil'})


def match(source):
    m = projected('Portuguese Language Data Contributor', profile(languages=('Portuguese',), country='Brazil'))
    m.update({k: source[k] for k in ('job_id', 'canonical_opportunity_id', 'url', 'source_slug')})
    m.update(opportunity_trust_status='trusted', opportunity_trust={'status': 'trusted'},
             primary_recommendation_eligible=True, preview_section='best_matches')
    return m


class SourceTaskFitTests(unittest.TestCase):
    def test_captured_tasks_language_is_not_substantive_support_or_conflict(self):
        for source in SOURCES:
            original = match(source)
            assessed = apply_source_task_fit(original, source, candidate())
            self.assertEqual(assessed['affirmative_fit_status'], 'uncertain')
            self.assertFalse(assessed['primary_recommendation_eligible'])
            self.assertFalse(assessed['conditional_task_fit'])
            self.assertEqual(assessed['preview_section'], 'explore_only')
            self.assertEqual(assessed['affirmative_fit']['conflicting_requirements'], ())
            self.assertNotIn('General language-data work', str(assessed['affirmative_fit_supported_evidence']))
            self.assertIn('Portuguese', str(assessed['affirmative_fit_supported_evidence']))
            self.assertIn(assessed['source_task_fit']['quote'], _source_text(source))
            self.assertEqual(assessed['score'], original['score'])
            self.assertEqual(assessed['score_components'], original['score_components'])
            self.assertEqual(assessed['source_task_fit']['source_reference']['job_id'], source['job_id'])
            self.assertNotIn('degree', str(assessed['affirmative_fit']['missing_requirements']).lower())

    def test_translation_editing_supports_related_work_not_all_specialties(self):
        for roles, skills in [(('Portuguese-English translator and editor',), ()), ((), ('translation',)), ((), ('editing',))]:
            source = SOURCES[0]
            assessed = apply_source_task_fit(match(source), source, candidate(roles=roles, skills=skills))
            self.assertEqual(assessed['affirmative_fit_status'], 'supported' if roles else 'uncertain')
            self.assertEqual(assessed['primary_recommendation_eligible'], bool(roles))
            self.assertTrue(assessed['source_task_fit']['profile_facts'])
            if roles:
                self.assertIn('Check the specialist tasks', assessed['source_task_fit']['candidate_note'])
            else:
                self.assertTrue(assessed['conditional_task_fit'])
                self.assertIn('skill mention', assessed['source_task_fit']['candidate_note'])
                self.assertEqual(assessed['source_task_fit']['status'], 'uncertain')

    def test_interest_generic_review_and_negated_or_unrelated_experience_do_not_support(self):
        for role in ('Interested in translation', 'No translation experience', 'Video editor', 'Administrative assistant', 'AI reviewer'):
            with self.subTest(role=role):
                assessed = apply_source_task_fit(match(SOURCES[0]), SOURCES[0], candidate(roles=(role,)))
                self.assertEqual(assessed['affirmative_fit_status'], 'uncertain')
                self.assertFalse(assessed['affirmative_fit']['conflicting_requirements'])

    def test_source_tasks_not_title_or_language_presence_define_the_background_question(self):
        source = dict(SOURCES[0], body=_source_text(SOURCES[0]).replace('Portuguese', 'German'),
                      body_format='text/plain')
        for level in ('basic','advanced','native'):
            p = candidate(roles=('AI evaluator',))
            p['languages'] = [dict(language='German', proficiency=level)]
            original = dict(match(source), title='Community project', matched_languages=['German'])
            assessed = apply_source_task_fit(original, source, p)
            self.assertFalse(assessed['conditional_task_fit'])
            self.assertFalse(assessed['affirmative_fit']['conflicting_requirements'])
            self.assertEqual(assessed['score_components'], original['score_components'])
            self.assertNotIn('degree', str(assessed['affirmative_fit']['missing_requirements']).lower())
        # Source-backed, practical transfer can be considered without asserting
        # a profession or making ideal degrees mandatory.
        for role,context in [('Volunteer linguistic analysis projects','projects'),
                             ('Linguistics student','study')]:
            assessed = apply_source_task_fit(match(source),source,candidate(roles=(role,)))
            self.assertTrue(assessed['conditional_task_fit'])
            self.assertFalse(assessed['primary_recommendation_eligible'])
            self.assertEqual(assessed['source_task_fit']['profile_facts'][0]['context'],context)

    def test_marketing_preferred_qualifications_and_beginner_wording_do_not_invent_tasks(self):
        for text in ('Be an expert and change AI forever.',
                     'A PhD in phonetics and morphology is ideal.',
                     'Linguistic analysis experience is preferred.',
                     'No experience needed. You will perform linguistic analysis.',
                     'You will not perform linguistic analysis.',
                     'Our research partners perform linguistic analysis.',
                     'Our researchers study phonetics and morphology.'):
            with self.subTest(text=text): self.assertIsNone(_task(text))

    def test_explicit_tasks_do_not_depend_on_provider_or_title(self):
        source = dict(SOURCES[0], source_slug='another-provider', body='You will perform linguistic analysis.', body_format='text/plain')
        m = dict(match(source), title='Community project', display_title='Community project')
        self.assertEqual(apply_source_task_fit(m, source, candidate())['affirmative_fit_status'], 'uncertain')
        source['body'] = 'You will teach language classes to students.'
        self.assertEqual(apply_source_task_fit(m, source, candidate(skills=('translation',)))['affirmative_fit_status'], 'uncertain')
        self.assertEqual(apply_source_task_fit(m, source, candidate(roles=('Language teacher',)))['affirmative_fit_status'], 'supported')

    def test_missing_or_cross_variant_source_does_not_transfer_conditions(self):
        m = match(SOURCES[0])
        for source in (None, SOURCES[1], dict(SOURCES[0], source_url='https://another.test'), dict(SOURCES[0], body='')):
            self.assertEqual(apply_source_task_fit(m, source, candidate()), m)

    def test_conditional_disclosure_preserves_links_and_never_enters_main_or_relaxation(self):
        m = apply_source_task_fit(match(SOURCES[0]), SOURCES[0], candidate(skills=('translation',)))
        context = {'matches': {'best_matches': [m]}}
        self.assertEqual(browser._primary_presentation_matches(context), [])
        self.assertEqual([v['job_id'] for v in browser._conditional_presentation_matches(context)], [m['job_id']])
        self.assertEqual(browser._presented_relaxation_scenarios(context), ())
        found = find_presented_variant(context, m['canonical_opportunity_id'], m['job_id'])
        self.assertEqual(found['_detail_recommendation_section'], 'conditional')
        html = browser._render_match_results(context, inventory_count=1)
        self.assertIn('Possibilities with conditions to check', html)
        self.assertIn(variant_detail_url(m).replace('&', '&amp;'), html)
        self.assertNotIn("class='match-card'", html)

    def test_conditional_still_obeys_geography_closure_and_freshness(self):
        original = apply_source_task_fit(match(SOURCES[0]), SOURCES[0], candidate(skills=('translation',)))
        for changes in ({'location_eligibility_status': 'incompatible'}, {'job_is_active': False},
                        {'opportunity_trust_status': 'unverified_source'},
                        {'opportunity_trust_status': 'stale_source', 'opportunity_trust': {'source_age_hours': 169}}):
            context = {'matches': {'best_matches': [dict(original, **changes)]}}
            self.assertEqual(browser._conditional_presentation_matches(context), [])

    def test_normal_detail_packet_has_specific_condition_not_complete_qualification_claim(self):
        source = SOURCES[0]; p = candidate()
        m = apply_source_task_fit(match(source), source, p)
        packet = prepare_card_evidence(m, source, p)
        self.assertIn(m['source_task_fit']['candidate_note'], packet['caveats'])


class SourceTaskAuthenticatedTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture(); self.addCleanup(self.f.close)
        p = self.f.profile
        p['languages'] = [dict(language='Portuguese', proficiency='native', locale='pt-BR', confidence='high')]
        p['experience']['recent_roles'] = ['Administrative assistant']
        p['experience']['professional_domains'] = []
        p['experience']['specialties'] = []
        p['education'].update(education_level='high_school', degrees=[], fields_or_domains=[])
        p['skills'].update(normalized=['spreadsheets'], free_text_labels=['spreadsheets'], domain_specific=[])
        from wahojobs.profiles.normalizer import signals_for_domains
        p['derived_matcher_signals']['signals'] = signals_for_domains(['generalist'], ['spreadsheets'], p['languages'])
        p['derived_matcher_signals']['derived_domains'] = ['generalist']
        from wahojobs.profiles.canonical_v2 import _convert_signals
        p['derived_matcher_signals'] = _convert_signals(p['derived_matcher_signals'])
        self.f.profile = with_preference_model(p, p['preferences']['preference_model'])
        self.f.update_inventory("UPDATE jobs SET title='Portuguese Language Data Contributor', department='Generalist', expertise='Generalist', commitment='Full-time'")
        self.f.update_inventory("UPDATE canonical_opportunities SET canonical_title='Portuguese Language Data Contributor', source_category='Generalist'")
        self.body = _source_text(SOURCES[0])

    def put_source(self, body=None):
        with closing(sqlite3.connect(self.f.path)) as c, c:
            j = c.execute('SELECT external_id,url FROM jobs WHERE id=7003').fetchone()
            c.execute('INSERT OR REPLACE INTO job_source_contents '
                      '(job_id,provider,source_type,source_url,external_id,body,body_format,metadata_json,'
                      'material_content_sha256,first_captured_at,last_captured_at) '
                      "VALUES (7003,'configured-production','catalog',?,?,?,'text/plain','{}','synthetic-evidence',?,?)",
                      (j[1], j[0], body or self.body, self.f.now.isoformat(), self.f.now.isoformat()))

    def current(self, target='/find-matches'):
        r = self.f.get(target); self.assertEqual(r.status, 200, r.body[:300])
        return r, self.f.last_run(), self.f.last_run().recommendation_context

    def test_evidence_change_invalidates_old_run_with_scoped_condition_and_action(self):
        _, run, context = self.current()
        self.assertIn(7003, [m['job_id'] for m in browser._primary_presentation_matches(context)])
        self.put_source()
        old = '/find-matches?run=' + run.match_run_id
        r, run, context = self.current(old)
        self.assertNotIn(7003, [m['job_id'] for m in browser._primary_presentation_matches(context)])
        self.assertEqual(browser._conditional_presentation_matches(context), [])
        suppressed = next(m for rows in context['matches'].values() for m in rows if m['job_id'] == 7003)
        self.assertFalse(suppressed['affirmative_fit']['conflicting_requirements'])
        self.assertFalse(browser.local_product.recent_cached_match_is_usable(suppressed, allow_conditional_task_fit=True))
        self.assertEqual(browser._presented_relaxation_scenarios(context), ())
        # A declared related skill permits consideration, but is not a proven profession.
        self.f.profile['skills']['normalized'] = ['translation']
        r, run, context = self.current(old)
        conditional = browser._conditional_presentation_matches(context)
        self.assertEqual([m['job_id'] for m in conditional], [7003])
        self.assertIn(7006, [m['job_id'] for m in browser._primary_presentation_matches(context)])
        self.assertIn('Practical experience with these specialist tasks still needs confirmation', r.body.decode())
        from wahojobs import authenticated_variant_details as variants
        with patch.object(variants, 'prepare_variant_notice', wraps=variants.prepare_variant_notice) as notice:
            detail = self.f.get(variant_detail_url(conditional[0], run_id=run.match_run_id))
        self.assertEqual(detail.status, 200)
        job = notice.call_args.args[0]
        self.assertEqual(job['job_id'], 7003)
        self.assertEqual(job['_authenticated_recommendation']['_detail_recommendation_section'], 'conditional')
        self.assertFalse(job['_authenticated_local_checks']['passes'])
        self.assertIn(job['official_url'], detail.body.decode())
        self.assertIn('Practical experience with these specialist tasks still needs confirmation', detail.body.decode())
        # No second inventory computation for unchanged current inputs.
        with patch.object(browser.profile_preview, 'query_preview_rows', side_effect=AssertionError('reuse recomputed')):
            self.current('/find-matches?run=' + run.match_run_id)
        self.f.profile['experience']['recent_roles'] = ['Portuguese-English translator and editor']
        _, _, context = self.current(old)
        self.assertIn(7003, [m['job_id'] for m in browser._primary_presentation_matches(context)])
        self.assertEqual(browser._conditional_presentation_matches(context), [])
        self.f.owner = 'b'
        self.assertEqual(self.f.get(old).status, 410)

    def test_preferences_closure_expiry_and_fallback_do_not_resurrect_condition(self):
        self.f.profile['skills']['normalized'] = ['translation']
        self.put_source()
        _, run, context = self.current()
        old = '/find-matches?run=' + run.match_run_id
        self.assertEqual(len(browser._conditional_presentation_matches(context)), 1)
        self.f.set_preferences('part_time')
        _, _, context = self.current(old)
        self.assertEqual(browser._conditional_presentation_matches(context), [])
        for scenario in browser._presented_relaxation_scenarios(context):
            self.assertNotIn(7003, [m['job_id'] for m in scenario['matches']])
        self.f.set_preferences('full_time')
        self.f.advance(73)
        r, _, context = self.current(old)
        self.assertEqual(len(browser._conditional_presentation_matches(context)), 1)
        self.assertIn('Availability needs confirmation.', r.body.decode())
        self.f.advance(120)
        _, _, context = self.current(old)
        self.assertEqual(browser._conditional_presentation_matches(context), [])
        self.f.advance(-193)
        self.f.update_inventory('UPDATE jobs SET is_active=0 WHERE id=7003')
        _, _, context = self.current(old)
        self.assertEqual(browser._conditional_presentation_matches(context), [])

    def test_source_change_to_explicit_beginner_role_removes_the_condition(self):
        self.put_source()
        _, run, _ = self.current()
        self.put_source('Review Portuguese text. No experience needed.')
        _, _, context = self.current('/find-matches?run=' + run.match_run_id)
        self.assertIn(7003, [m['job_id'] for m in browser._primary_presentation_matches(context)])
        self.assertEqual(browser._conditional_presentation_matches(context), [])


if __name__ == '__main__': unittest.main()
