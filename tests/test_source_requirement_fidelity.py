"""Exact public wording, isolated fresh synthetic acceptance, real authenticated consumers."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from tests.matching_delivery_support import DeliveryFixture, JOB, degree_profile, body
from tests.test_accepted_title_uncertainty import profile
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_variant_details import variant_detail_url
from wahojobs.candidate_condition_comparisons import compare_conditions, _modality
from wahojobs.matching.accepted_tasks import _prepare, _prepare_eligibility

FIXTURES=Path(__file__).parent/'fixtures'/'source_requirement_fidelity'
FULL=(FIXTURES/'accepted-generalist.txt').read_text(encoding='utf-8')
WAIVER='No prior AI, tech, or content moderation experience required'
CLOCK=datetime.fromisoformat('2026-09-14T13:02:08+00:00')


def source(*clauses, heading='Who You Are', duty='Review and evaluate AI-generated content.'):
    return '## Responsibilities\n\n'+duty+'\n\n## '+heading+'\n\n'+'\n\n'.join(clauses)


def comparisons(quote, heading='Requirements', candidate=None):
    packet=dict(job_id=1,external_id='synthetic',url='https://example.test/1',source_hash='synthetic',
                captured_at=CLOCK.isoformat(),caveats=[],kind='Advertised role',
                conditions=[dict(heading=heading,reference='source block 1',text=quote)])
    return compare_conditions(packet, candidate or profile())


class SourceRequirementFidelityTests(unittest.TestCase):
    def fixture(self, text, candidate=None, **kw):
        f=DeliveryFixture(candidate or profile(),text,title=kw.pop('title','Generalist'),now=CLOCK,**kw)
        self.addCleanup(f.close)
        return f

    def test_original_accepted_waiver_metadata_and_visible_wording(self):
        self.assertIn(WAIVER,FULL)
        f=self.fixture(FULL)
        response,run,ctx,m=f.current()
        detail,packet=f.detail(run)
        waiver=next(r for r in packet['comparisons'] if r['source']['quote']==WAIVER)
        self.assertEqual((waiver['kind'],waiver['modality'],waiver['status']),('waiver','not_required','not_applicable'))
        self.assertEqual(waiver['supported_parts'],[])
        self.assertEqual(waiver['profile_facts'],[])
        self.assertIn(b"<details class='employer-description'>",detail.body)
        self.assertNotIn(('Check the requirement: “'+WAIVER+'”.').encode(),response.body)
        self.assertIn(WAIVER.encode(),detail.body)
        self.assertEqual(m['source_qualification_comparisons'],packet['comparisons'])
        self.assertNotIn(WAIVER,[r['source']['quote'] for r in m['source_task_fit']['conditions']])
        self.assertFalse(browser._primary_presentation_matches(ctx))
        self.assertEqual([r['job_id'] for r in browser._conditional_presentation_matches(ctx)],[JOB])
        self.assertIn('Clear written communication skills in English',
                      [r['source']['quote'] for r in m['source_task_fit']['conditions']])
        self.assertEqual(len(m['source_task_fit']['conditions']),1)
        generic=m['non_decisive_source_questions']
        self.assertEqual(len(generic),4)
        self.assertTrue(all(r['status']=='unresolved' and not r['admission_decisive'] for r in generic))
        self.assertEqual({r['source']['quote'] for r in generic}, {
            'Strong attention to detail with a systematic, thorough approach to tasks',
            'Comfortable evaluating a broad variety of topics and content formats',
            'Self-motivated and reliable when working independently',
            'Able to follow structured guidelines and apply them consistently'})
        self.assertEqual(len(m['accepted_task_fit']['facts']),1)
        self.assertTrue(all(not r['professional_domains'] for r in m['accepted_task_fit']['facts']))

    def test_explicit_required_preferred_and_waived_are_distinct(self):
        for quote,mode in [(WAIVER,'not_required'),('Prior AI experience required','required'),
            ('Prior AI experience preferred','preferred'),('No specialized background required','not_required'),
            ('Professional experience is not required','not_required'),('No relevant professional experience needed','not_required')]:
            with self.subTest(quote=quote):
                row=comparisons(quote,heading='Who You Are')[0]
                self.assertEqual(row['modality'],mode)
                self.assertEqual(row['status'],'not_applicable' if mode=='not_required' else 'unresolved')
                self.assertFalse(row['supported_parts'])
        self.assertEqual(comparisons('Prior AI experience preferred')[0]['modality'],'conflicting')

    def test_exceptions_mixed_clauses_and_quantitative_bounds_are_not_waivers(self):
        for quote in ['No AI experience required unless assigned clinical work',
            'AI experience is not required except for clinical assignments',
            'No specialized background or prior AI experience required — just a sharp mind']:
            with self.subTest(quote=quote):
                row=comparisons(quote)[0]
                self.assertEqual(row['modality'],'unresolved')
                self.assertEqual(row['status'],'unresolved')
                self.assertFalse(row['supported_parts'])
                self.assertIn('scope',row['message'])
        for quote in ['No less than 3 years of Python experience required',
            'No fewer than 3 years of professional experience required',
            'No more than 20 hours per week required','Not only AI experience required']:
            self.assertEqual(_modality('Who You Are',quote),'required')

    def test_ai_waiver_retains_required_professional_background(self):
        for field in ('medical','legal','software engineering'):
            with self.subTest(field=field):
                f=self.fixture(source('No prior AI experience required','Hands-on '+field+' experience required'))
                _,run,ctx,m=f.current();_,packet=f.detail(run)
                self.assertEqual(packet['comparisons'][0]['modality'],'not_required')
                row=packet['comparisons'][1]
                self.assertEqual(row['modality'],'required')
                self.assertEqual(row['kind'],'professional_background')
                self.assertFalse(row['supported_parts'])
                self.assertFalse(browser._primary_presentation_matches(ctx))
                self.assertFalse(browser._conditional_presentation_matches(ctx))
                self.assertEqual(m['primary_admission_source'],'accepted_task_professional_background')

    def test_waiver_does_not_cancel_independent_language_or_location(self):
        for clause in ('Native French required','Must be based in Canada','No AI experience required; native French required'):
            with self.subTest(clause=clause):
                candidate=profile()
                if 'French' in clause:
                    from tests.test_professional_background_components import confirmed
                    from wahojobs.profiles.canonical_v2 import _material_field_paths, validate_canonical_profile_v2
                    candidate['languages'].append(dict(language='French',proficiency='basic',locale='',confidence='high'))
                    candidate['provenance']['field_sources']=[]
                    for path in _material_field_paths(candidate):confirmed(candidate,path)
                    candidate=validate_canonical_profile_v2(candidate)
                f=self.fixture(source(WAIVER,clause),candidate)
                _,run,ctx,m=f.current();_,packet=f.detail(run)
                self.assertFalse(browser._primary_presentation_matches(ctx))
                self.assertFalse(browser._conditional_presentation_matches(ctx))
                self.assertFalse(m['primary_recommendation_eligible'])
                self.assertEqual(packet['comparisons'][0]['modality'],'not_required')
        for quote,expected in [('No native French required','not_required'),
            ('No AI experience required; native French required','required'),
            ('No AI experience required unless native French required','unresolved')]:
            language,_=_prepare_eligibility('synthetic','test','1','https://example.test/1',source(quote),'text/markdown','{}')
            self.assertEqual(language[0]['modality'],expected)

    def test_preferred_profession_and_existing_qualifying_alternative(self):
        f=self.fixture(source(WAIVER)+'\n\n## Nice to Have\n\nHands-on medical experience')
        _,_,ctx,m=f.current()
        self.assertNotEqual(m.get('primary_admission_source'),'accepted_task_professional_background')
        self.assertEqual(m['source_qualification_comparisons'][1]['modality'],'preferred')
        g=self.fixture(body(),degree_profile('marketing',years=2),title='Uncatalogued assessment position')
        _,_,ctx,m=g.current()
        row=m['source_qualification_comparisons'][0]
        self.assertEqual(row['status'],'supported')
        self.assertIn('qualifying_routes',row['components'])
        self.assertFalse(browser._conditional_presentation_matches(ctx))

    def test_teaser_full_source_and_software_interest_remain_distinct(self):
        profiles=json.loads((FIXTURES/'profiles.json').read_text(encoding='utf-8-sig'))
        for key in ('evaluator_br','software_de'):
            with self.subTest(profile=key):
                f=self.fixture(FULL,profiles[key]);_,_,ctx,m=f.current()
                self.assertEqual(bool(m['accepted_task_fit']),key=='evaluator_br')
                self.assertEqual(bool(browser._conditional_presentation_matches(ctx)),key=='evaluator_br')
        teaser=self.fixture('Get paid to shape the future of AI — evaluate content across any topic, from anywhere. No experience needed.')
        _,_,ctx,m=teaser.current()
        self.assertIsNone(m['accepted_task_fit'])
        self.assertFalse(browser._conditional_presentation_matches(ctx))

    def test_content_task_requires_duties_ai_context_and_supported_domain(self):
        self.assertTrue(_prepare('synthetic','p','id','url',
            source('',duty='Review content and video.'),'text/markdown','{}'))
        self.assertTrue(_prepare('synthetic','p','id','url',
            source('',duty='Review content generated by AI.'),'text/markdown','{}'))
        for text in [source(WAIVER,duty='Review content for accuracy.'),
            source('',duty='Review marketing content for spelling.')+'\n\n## Nice to Have\n\nFamiliarity with AI tools.',
            'Our company builds AI.\n\n## Responsibilities\n\nReview content for spelling.',
            source('',duty='Review human-written content using AI tools.'),
            source('',duty='Review non-AI-generated content.'),
            'Our clients evaluate AI-generated content.',
            source(WAIVER,duty='You will not evaluate AI-generated content.'),
            '## Nice to Have\n\nExperience evaluating AI-generated content.']:
            self.assertEqual(_prepare('synthetic','p','id','url',text,'text/markdown','{}'),())
        f=self.fixture(source(WAIVER,duty='Review and evaluate medical AI-generated content.'))
        _,_,ctx,m=f.current()
        self.assertIsNone(m['accepted_task_fit'])
        self.assertFalse(browser._conditional_presentation_matches(ctx))

    def test_independent_waiver_and_professional_clause_keep_exact_scope(self):
        for separator in ('; ','. ', ', but '):
            quote='No AI experience required'+separator+'Hands-on medical experience required'
            with self.subTest(quote=quote):
                f=self.fixture(source(quote));_,run,ctx,m=f.current();detail,packet=f.detail(run)
                rows=packet['comparisons']
                self.assertEqual([r['modality'] for r in rows],['not_required','required'])
                self.assertEqual(rows[1]['kind'],'professional_background')
                self.assertFalse(rows[1]['supported_parts'])
                self.assertEqual(rows[0]['source']['line'],rows[1]['source']['line'])
                self.assertEqual(rows[0]['source']['block_reference'],rows[1]['source']['block_reference'])
                self.assertIn(quote.encode(),detail.body)
                self.assertFalse(browser._conditional_presentation_matches(ctx))
                self.assertFalse(browser._primary_presentation_matches(ctx))

    def test_required_experience_is_a_question_but_whole_waiver_is_not(self):
        for quote,conditional in [(WAIVER,False),('Prior AI experience required',True)]:
            with self.subTest(quote=quote):
                f=self.fixture(source(quote))
                response,run,ctx,m=f.current();detail,packet=f.detail(run)
                self.assertEqual(bool(browser._conditional_presentation_matches(ctx)),conditional)
                self.assertEqual(bool(browser._primary_presentation_matches(ctx)),not conditional)
                row=packet['comparisons'][0]
                self.assertEqual(row['modality'],'required' if conditional else 'not_required')
                if conditional:
                    self.assertIn(quote,[r['source']['quote'] for r in m['source_task_fit']['conditions']])
                else:
                    self.assertFalse(m.get('source_task_fit'))
                    self.assertIn(b'The employer states:',detail.body)
                    self.assertIn(quote.encode(),detail.body)

    def test_source_replacement_exact_sibling_and_owner_binding(self):
        f=self.fixture(FULL,sibling=True)
        _,run,ctx,m=f.current()
        self.assertEqual([row['job_id'] for row in browser._conditional_presentation_matches(ctx)],[JOB])
        detail,packet=f.detail(run)
        old_hash=packet['source_hash']
        url=variant_detail_url(dict(job_id=JOB,canonical_opportunity_id=900002),run_id=run.match_run_id)
        self.assertEqual(f.get(url,owner=1).status,404)
        sibling,sibling_packet=f.detail(run,JOB+1)
        self.assertNotIn(WAIVER.encode(),sibling.body)
        with f.connections.writable_connection_provider() as connection:
            connection.row_factory=sqlite3.Row
            f._capture(connection,source('Hands-on medical experience required'),JOB)
            connection.commit()
        from wahojobs import authenticated_source_detail as detail_module
        original_render=detail_module.render_authenticated_job_page
        observed=[]
        def observe(job,**kw):
            observed.append(deepcopy(job))
            return original_render(job,**kw)
        with patch.object(detail_module,'render_authenticated_job_page',side_effect=observe):
            replaced_detail,new_packet=f.detail(run)
        local=observed[0]['_authenticated_local_checks']['match']
        self.assertFalse(local.get('conditional_task_fit'))
        self.assertFalse(local['primary_recommendation_eligible'])
        self.assertIn(b'Hands-on medical experience required',replaced_detail.body)
        _,_,new_context,new_match=f.current()
        self.assertFalse(browser._primary_presentation_matches(new_context))
        self.assertFalse(browser._conditional_presentation_matches(new_context))
        self.assertEqual(new_match['primary_admission_source'],'accepted_task_professional_background')
        self.assertNotEqual(new_packet['source_hash'],old_hash)
        self.assertFalse(any(r['modality']=='not_required' for r in new_packet['comparisons']))
        self.assertEqual(packet['source_hash'],old_hash)


if __name__=='__main__':unittest.main()
