"""Captured wording plus synthetic profiles/counterexamples; no live requests."""
from copy import deepcopy
from contextlib import closing
import hashlib
import sqlite3
import unittest
from unittest.mock import patch

from tests.test_authenticated_card_evidence import SOURCES, card
from wahojobs.authenticated_card_evidence import prepare_card_evidence, render_card_evidence
from wahojobs.authenticated_source_detail import prepare_detail_display


def profile():
    # The demonstrated facts of the approved synthetic Portugal biology profile.
    return {'education': {'education_level': 'doctorate', 'degrees': ['PhD in Molecular Biology'],
                         'entries': [{'kind': 'doctorate', 'qualification': 'PhD in Molecular Biology',
                                      'field': 'Molecular Biology', 'status': 'completed'}]},
            'skills': {'software_tools': ['laboratory microscopy', 'R', 'reference managers']},
            'preferences': {'preference_model': {'workloads': ['part_time']}, 'availability': 'unknown'},
            'location': {'country': 'Portugal'}, 'provenance': {'field_sources': []}}


def confirmed(p, path):
    p['provenance']['field_sources'].append({'field_path': path, 'explicit': True,
        'source_kind': 'user_confirmation', 'source_ordinals': [1], 'path_version': 'canonical_profile_v2_path_v1'})


def prepared(text=None, p=None, identity=11242):
    source = deepcopy(SOURCES[identity])
    if text is not None:
        source.update(body=text, metadata_json='{}')
    return prepare_card_evidence(card(source), source, p if p is not None else profile())


def compared(text, p=None):
    return [r for r in prepared(text, p)['comparisons'] if r['kind'] != 'unassessed']


class CandidateConditionComparisonsTests(unittest.TestCase):
    def test_captured_coding_degree_r_mention_and_missing_git_docker(self):
        rows = prepared()['comparisons']; education = next(r for r in rows if r['kind'] == 'education')
        self.assertEqual(education['status'], 'unresolved')
        self.assertEqual(education['supported_parts'], ['degree level/status'])
        self.assertIn('whether Molecular Biology is accepted', education['message'])
        tools = [r for r in rows if r['kind'] == 'tools']
        self.assertEqual(len(tools), 2)
        self.assertIn('R, an accepted tool option', tools[0]['message'])
        self.assertEqual(tools[0]['status'], 'not_established')
        self.assertIn('Git/GitHub and Docker experience isn’t stated', tools[1]['message'])
        self.assertIn('available hours aren’t established', next(r for r in rows if r['kind'] == 'workload')['message'])
        self.assertTrue(any('Demonstrated depth' in r['source']['quote'] and r['kind'] == 'unassessed' for r in rows))

    def test_captured_ideal_degree_is_supported_without_promoting_preference(self):
        rows = prepared(identity=11271)['comparisons']
        degree = next(r for r in rows if r['kind'] == 'education')
        self.assertEqual((degree['status'], degree['modality']), ('supported', 'preferred'))
        self.assertIn('Preferred: your PhD in Molecular Biology matches', degree['message'])
        conditional = next(r for r in rows if "Master's degree considered" in r['source']['quote'])
        self.assertEqual(conditional['kind'], 'unassessed')
        self.assertEqual(conditional['status'], 'unresolved')

    def test_required_and_preferred_education_and_inline_conflict(self):
        for heading, mode in [('Required', 'required'), ('Preferred', 'preferred'), ('Ideal Qualifications', 'preferred')]:
            row = compared(f'**{heading}**\n- PhD in Molecular Biology')[0]
            self.assertEqual((row['status'], row['modality']), ('supported', mode))
        row = compared('**Required**\n- PhD in Molecular Biology preferred')[0]
        self.assertEqual((row['status'], row['modality']), ('unresolved', 'conflicting'))

    def test_degree_and_field_or_alternatives_not_related_equivalence(self):
        row = compared("**Required**\n- Master's degree or PhD in Chemistry or Molecular Biology")[0]
        self.assertEqual(row['status'], 'supported')
        row = compared('**Required**\n- PhD in Chemistry or a closely related field')[0]
        self.assertEqual(row['status'], 'unresolved')
        row = compared('**Required**\n- PhD in Chemistry and Molecular Biology')
        self.assertEqual(row, [])
        self.assertEqual(compared('**Required**\n- PhD in Chemistry, Molecular Biology'), [])

    def test_in_progress_versus_completed_and_no_degree_inference(self):
        p = profile(); p['education']['entries'][0]['status'] = 'in_progress'
        self.assertEqual(compared('**Required**\n- PhD in Molecular Biology', p)[0]['status'], 'not_established')
        self.assertEqual(compared('**Preferred**\n- PhD or doctoral candidate in Molecular Biology', p)[0]['status'], 'supported')
        p['education']['entries'] = []
        self.assertEqual(compared('**Required**\n- PhD in Molecular Biology', p)[0]['status'], 'not_established')

    def test_missing_degree_is_not_conflict_but_explicit_no_degree_is(self):
        p = profile(); p['education'] = {}
        self.assertEqual(compared('**Required**\n- PhD in Biology', p)[0]['status'], 'not_established')
        p['education']['education_level'] = 'no_degree'
        self.assertEqual(compared('**Required**\n- PhD in Biology', p)[0]['status'], 'not_established')
        confirmed(p, 'education.education_level')
        self.assertEqual(compared('**Required**\n- PhD in Biology', p)[0]['status'], 'contradicted')

    def test_conflicting_profile_education_stays_unresolved(self):
        p = profile(); p['education']['education_level'] = 'no_degree'; confirmed(p, 'education.education_level')
        self.assertEqual(compared('**Required**\n- PhD in Molecular Biology', p)[0]['status'], 'unresolved')

    def test_tool_mention_never_proves_proficiency_or_years(self):
        p = profile(); p['skills']['entries'] = [{'skill': 'R', 'confidence': 'high'}]
        row = compared('**Required**\n- Working proficiency in Python or R', p)[0]
        self.assertEqual(row['status'], 'not_established')
        self.assertEqual(row['supported_parts'], ['tool mention: R'])
        self.assertEqual(compared('**Required**\n- 5 years of R experience', p), [])

    def test_and_or_alternatives_negative_evidence_does_not_flatten(self):
        p = profile(); p['constraints'] = {'hard_constraints': ['No experience with Docker']}
        confirmed(p, 'constraints.hard_constraints[0]')
        row = compared('**Required**\n- Experience with Python and Docker', p)[0]
        self.assertEqual(row['status'], 'contradicted')
        row = compared('**Required**\n- Experience with Python or Docker', p)[0]
        self.assertEqual(row['status'], 'not_established')
        row = compared('**Required**\n- Experience with R or Docker', p)[0]
        self.assertEqual(row['status'], 'not_established')
        self.assertIn('accepted tool option', row['message'])
        p['skills']['software_tools'].append('Docker')
        self.assertEqual(compared('**Required**\n- Experience with Docker', p)[0]['status'], 'unresolved')

    def test_mixed_operators_and_unknown_tools_remain_unassessed(self):
        self.assertEqual(compared('**Required**\n- Experience with Python or R and Docker'), [])
        self.assertEqual(compared('**Required**\n- Experience with Python, R'), [])
        self.assertEqual(compared('**Required**\n- Experience with MATLAB'), [])
        p = profile(); p['skills'] = {'software_tools': ['Julia']}
        row = compared('**Required**\n- Proficiency in Python or another relevant programming language',p)[0]
        self.assertEqual(row['status'],'not_established')
        self.assertIn('or another relevant programming language',row['message'])

    def test_unknown_tools_are_not_title_inferences_and_negation_is_not_positive(self):
        packet = prepared('**Role overview**\nPython PhD Biology\n\n**Required**\n- No PhD required\n- R is not required')
        # Explicit waivers now have source-grounded explanations, never a
        # positive candidate qualification or a title-derived shortfall.
        self.assertTrue(all(r['modality'] == 'not_required' and r['status'] == 'not_applicable'
                            and not r['profile_facts'] and not r['supported_parts']
                            for r in packet['comparisons']))
        self.assertTrue(all(r['message'] == 'The source explicitly says this is not required.'
                            for r in packet['comparisons']))
        self.assertEqual(compared('**Required**\n- Experience with Python without R'), [])
        self.assertEqual(compared('**Required**\n- PhD in '+('x'*2000)), [])

    def test_conflicting_or_qualified_source_does_not_get_affirmative_verdict(self):
        rows = compared('**Required**\n- PhD in Molecular Biology\n- PhD not required')
        self.assertEqual(rows[0]['status'], 'unresolved')
        rows = compared('**Required**\n- PhD in Molecular Biology\n- PhD in Chemistry')
        self.assertTrue(all(r['status'] == 'unresolved' for r in rows))
        rows = compared('**Required**\n- Proficiency in R\n- R is not required')
        self.assertEqual(rows[0]['status'], 'unresolved')

    def test_workload_preference_is_never_hourly_capacity(self):
        for availability in ('unknown', 'available', 'immediate', 'part-time'):
            p = profile(); p['preferences']['availability'] = availability
            confirmed(p, 'preferences.availability')
            row = compared('**Engagement**\n- Commitment: part-time, 20+ hours per week', p)[0]
            self.assertEqual(row['status'], 'not_established')
            self.assertIn('You prefer part-time', row['message'])
            self.assertIn('available hours aren’t established', row['message'])

    def test_explicit_unavailability_and_preferred_condition_meaning(self):
        p = profile(); p['preferences']['availability'] = 'unavailable'
        self.assertEqual(compared('**Engagement**\nCommitment: 10+ hours/week', p)[0]['status'], 'not_established')
        confirmed(p, 'preferences.availability')
        row = compared('**Preferred**\nCommitment: 10+ hours/week', p)[0]
        self.assertEqual((row['status'], row['modality']), ('contradicted', 'preferred'))
        self.assertTrue(row['message'].startswith('Preferred:'))
        row = compared('This is not a specific job posting.\n\n**Engagement**\nCommitment: 10+ hours/week',p)[0]
        self.assertEqual(row['status'],'unresolved')
        self.assertIn('future project',row['message'])

    def test_workload_alternatives_questions_and_conflicts_remain_unassessed(self):
        for text in ('Commitment: 10 hours/week OR 5 hours/week', 'Can you commit to 10 hours/week?',
                     'Typically 10 hours/week', 'Commitment: up to 10 hours/week'):
            self.assertEqual(compared('**Engagement**\n'+text), [])
        source = deepcopy(SOURCES[11242]); source['commitment'] = 'Full-time'
        row = next(r for r in prepare_card_evidence(card(source), source, profile())['comparisons'] if r['kind'] == 'workload')
        self.assertEqual(row['status'], 'unresolved')
        rows = compared('**Engagement**\n- Commitment: 10 hours/week\n- Commitment: 20 hours/week')
        self.assertTrue(all(r['status'] == 'unresolved' for r in rows))

    def test_provenance_current_profile_and_exact_source_are_retained_without_mutation(self):
        p = profile(); confirmed(p, 'skills.software_tools[1]'); saved = deepcopy(p)
        source = deepcopy(SOURCES[11242]); before = deepcopy(source)
        packet = prepare_card_evidence(card(source), source, p)
        tool = next(r for r in packet['comparisons'] if 'accepted tool option' in r['message'])
        fact = next(f for f in tool['profile_facts'] if f['field_path'] == 'skills.software_tools[1]')
        self.assertEqual(fact['value'], 'R'); self.assertEqual(fact['sources'][0]['source_ordinals'], [1])
        self.assertEqual(tool['source']['job_id'], source['job_id'])
        self.assertEqual(tool['source']['source_hash'], source['material_content_sha256'])
        self.assertEqual(tool['source']['captured_at'], source['last_captured_at'])
        self.assertIn(tool['source']['quote'], source['body'])
        self.assertEqual((p, source), (saved, before))
        p['skills']['software_tools'] = []
        changed = prepare_card_evidence(card(source), source, p)
        self.assertNotIn('accepted tool option', str(changed['comparisons']))
        source['body'] = '**Required**\n- PhD in Molecular Biology'; source['material_content_sha256'] = 'new-evidence'
        changed = prepare_card_evidence(card(source), source, p)
        self.assertEqual(changed['comparisons'][0]['status'], 'supported')
        self.assertEqual(changed['comparisons'][0]['source']['source_hash'], 'new-evidence')

    def test_cross_variant_source_rejected_and_missing_evidence_no_claim(self):
        source = deepcopy(SOURCES[11242]); match = card(source); match['job_id'] = 11271
        with patch('wahojobs.candidate_condition_comparisons.compare_conditions', side_effect=AssertionError('must not compare')):
            self.assertIsNone(prepare_card_evidence(match, source, profile()))
        self.assertEqual(prepared('**Required**\nAsk the employer.')['comparisons'][0]['kind'], 'unassessed')

    def test_card_detail_share_comparisons_and_preserve_source_controls(self):
        source = deepcopy(SOURCES[11242]); packet = prepare_card_evidence(card(source), source, profile())
        job = dict(job_id=source['job_id'],canonical_opportunity_id=source['canonical_opportunity_id'],
            external_id=source['external_id'],official_url=source['url'],company_slug=source['source_slug'],
            rich_provider=source['source_slug'],rich_external_id=source['external_id'],rich_source_url=source['url'],
            rich_body=source['body'],rich_body_format=source['body_format'],rich_metadata_json=source['metadata_json'],
            last_captured_at=source['last_captured_at'],material_content_sha256=source['material_content_sha256'],
            source_commitment=source.get('commitment'),source_location=source.get('location'))
        self.assertEqual(prepare_detail_display(job, profile())['comparisons'], packet['comparisons'])
        html = render_card_evidence(packet, 'example')
        self.assertNotIn('How your profile compares', html)
        self.assertTrue(any(row['modality'] == 'preferred' for row in packet['comparisons']))
        from tests.test_candidate_source_display import detail
        from wahojobs.authenticated_source_detail import render_authenticated_job_page
        from wahojobs.candidate_source_display import plain
        from html import unescape
        full = render_authenticated_job_page(detail(source,card(source)),profile=profile(),navigation='')
        self.assertIn("<details class='employer-description'>", full)
        self.assertIn('<summary>Employer description and requirements</summary>', full)
        from html.parser import HTMLParser
        parser=HTMLParser(); text=[]; parser.handle_data=text.append; parser.feed(full)
        visible_source = ' '.join(unescape(' '.join(text)).split())
        for row in packet['comparisons']:
            self.assertIn(' '.join(plain(row['source']['quote']).split()), visible_source)
        for internal in ('not_established', 'supported_parts', 'source block', 'field_path'):
            self.assertNotIn(internal, html)


class AuthenticatedConditionComparisonTests(unittest.TestCase):
    def setUp(self):
        from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
        from wahojobs.opportunity_enrichment_schema import OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS
        self.f = SyntheticMatcherFixture(); self.addCleanup(self.f.close)
        # Minimal saved source content owned by this disposable synthetic DB.
        with closing(sqlite3.connect(self.f.path)) as conn, conn:
            conn.execute(OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[0])
            for identity in (7003, 7006):
                url, external = conn.execute('SELECT url,external_id FROM jobs WHERE id=?', (identity,)).fetchone()
                text = '**Required**\n- Proficiency in Python or R' if identity == 7003 else '**Required**\n- Experience with Docker'
                conn.execute('INSERT INTO job_source_contents(job_id,provider,source_type,source_url,external_id,body,body_format,material_content_sha256,first_captured_at,last_captured_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (identity,'configured-production','synthetic',url,external,text,'text/markdown',
                              hashlib.sha256(text.encode()).hexdigest(),self.f.now.isoformat(),self.f.now.isoformat()))

    def observed_response(self, target):
        from wahojobs.candidate_condition_comparisons import compare_conditions
        comparisons = {}
        def observe(packet, profile, **kwargs):
            rows = compare_conditions(packet, profile, **kwargs)
            comparisons[packet['job_id']] = deepcopy(rows)
            return rows
        with patch('wahojobs.candidate_condition_comparisons.compare_conditions', side_effect=observe):
            response = self.f.get(target)
        return response, comparisons

    def test_old_run_uses_current_profile_and_evidence_without_changing_decisions(self):
        from wahojobs import authenticated_profile_matches as browser
        from tests.test_profile_preference_model import with_preference_model
        response = self.f.get(); self.assertEqual(response.status, 200)
        run = self.f.last_run(); old = '/find-matches?run=' + run.match_run_id
        decisions = deepcopy(run.recommendation_context['matches'])
        with patch('wahojobs.candidate_condition_comparisons.compare_conditions', return_value=[]):
            without = self.f.get(old)
        self.assertEqual(without.status, 200)
        self.assertEqual(run.recommendation_context['matches'], decisions)
        current, current_rows = self.observed_response(old)
        self.assertIn('requested proficiency', str(current_rows[7003]))
        self.assertIn('Check the requirement: “Proficiency in Python or R”.', current.body.decode())
        # New display facts must be read from this revision, never a cached card.
        self.f.profile['skills'].setdefault('software_tools', []).append('R')
        self.f.profile = with_preference_model(self.f.profile, self.f.profile['preferences']['preference_model'])
        changed, changed_rows = self.observed_response(old); self.assertEqual(changed.status, 200)
        self.assertIn('profile mentions', str(changed_rows[7003]))
        self.assertIn('R, an accepted tool option', str(changed_rows[7003]))
        self.assertIn('Check the requirement: “Proficiency in Python or R”.', changed.body.decode())
        text = '**Required**\n- Experience with Docker'
        self.f.update_inventory('UPDATE job_source_contents SET body=?,material_content_sha256=? WHERE job_id=7003',
                                (text,hashlib.sha256(text.encode()).hexdigest()))
        after, after_rows = self.observed_response(old); self.assertEqual(after.status, 200)
        self.assertNotIn('R, an accepted tool option', str(after_rows[7003]))
        self.assertIn('Docker experience isn’t stated', str(after_rows[7003]))
        self.assertIn('Check the requirement: “Experience with Docker”.', after.body.decode())
        # Exact details use the same current clause without any catalog scorer.
        original = browser.profile_preview.query_preview_rows
        scoped_calls = []
        def scoped(conn, **kwargs):
            self.assertEqual(kwargs.get('canonical_opportunity_id'),7002)
            scoped_calls.append(kwargs)
            return original(conn, **kwargs)
        with patch.object(browser.profile_preview, 'query_preview_rows', side_effect=scoped):
            detail, detail_rows = self.observed_response('/job/opportunity-7002?variant=7003')
        self.assertEqual(len(scoped_calls),1)
        self.assertEqual(detail.status, 200)
        self.assertIn('Docker experience isn’t stated', str(detail_rows[7003]))
        self.assertNotIn('R, an accepted tool option', str(detail_rows[7003]))
        self.assertIn('Check the requirement: “Experience with Docker”.', detail.body.decode())

    def test_only_visible_ids_are_compared_owner_isolation_and_alternative_identity(self):
        from wahojobs import authenticated_profile_matches as browser
        from wahojobs.candidate_condition_comparisons import compare_conditions
        seen = []
        def record(packet, p, **kwargs):
            seen.append(packet['job_id']); return compare_conditions(packet,p,**kwargs)
        with patch('wahojobs.candidate_condition_comparisons.compare_conditions', side_effect=record):
            response = self.f.get()
        self.assertEqual(response.status,200)
        run=self.f.last_run()
        visible=browser._primary_presentation_matches(run.recommendation_context)
        self.assertEqual(seen,[m['job_id'] for m in visible])
        old='/find-matches?run='+run.match_run_id
        self.f.set_preferences('part_time')
        response, current_rows=self.observed_response(old);self.assertEqual(response.status,200)
        self.assertIn('Docker experience isn’t stated',str(current_rows[7006]))
        self.assertIn('Check the requirement: “Experience with Docker”.',response.body.decode())
        self.assertNotIn('accepted tool option',str(current_rows[7006]))
        self.assertEqual(current_rows[7006][0]['source']['job_id'],7006)
        self.f.owner='b'
        response=self.f.get(old);self.assertEqual(response.status,410)
        self.assertNotIn('candidate-comparisons',response.body.decode())


if __name__ == '__main__':
    unittest.main()
