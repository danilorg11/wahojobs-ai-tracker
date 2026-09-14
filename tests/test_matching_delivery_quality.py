"""Authenticated list/detail consequences of source-supported qualifying routes."""
from copy import deepcopy
import unittest
from tests.matching_delivery_support import DeliveryFixture, degree_profile, body, JOB
from wahojobs import authenticated_profile_matches as browser


class MatchingDeliveryQualityTests(unittest.TestCase):
    def fixture(self,p,text,**kw):
        f=DeliveryFixture(p,text,**kw);self.addCleanup(f.close);return f

    def test_supported_degree_routes_reach_conditional_list_and_exact_detail(self):
        for field in ('marketing','biology'):
            with self.subTest(field=field):
                f=self.fixture(degree_profile(field),body(field))
                response,run,ctx,m=f.current()
                self.assertEqual([x['job_id'] for x in browser._conditional_presentation_matches(ctx)],[JOB])
                self.assertEqual(browser._primary_presentation_matches(ctx),[])
                self.assertEqual(m['preview_section'],'explore_only')
                self.assertFalse(m['primary_recommendation_eligible'])
                row=m['source_qualification_comparisons'][0]
                self.assertEqual(row['status'],'supported')
                self.assertEqual(row['supported_parts'],[])
                self.assertEqual(row['components']['occupational_relevance']['status'],'not_established')
                detail,packet=f.detail(run)
                listed=row['components']['qualifying_routes']
                shown=packet['comparisons'][0]['components']['qualifying_routes']
                self.assertEqual({k:shown[k] for k in ('operator','status','source_text_digest')},
                                 {k:listed[k] for k in ('operator','status','source_text_digest')})
                # Display mode deliberately changes explanatory prose. The
                # decisions, original clauses and their confirmed facts agree.
                self.assertEqual([{k:r[k] for k in ('kind','status','source','profile_facts')} for r in shown['routes']],
                                 [{k:r[k] for k in ('kind','status','source','profile_facts')} for r in listed['routes']])
                self.assertIn(b'Your confirmed degree',detail.body)
                self.assertEqual(f.get(owner=None).status,401)
                self.assertEqual(f.get('/find-matches?run='+run.match_run_id,owner=1).status,410)

    def test_supported_group_with_no_source_questions_keeps_only_title_uncertainty(self):
        f=self.fixture(degree_profile('marketing'),body(extra=''))
        response,run,ctx,m=f.current()
        self.assertEqual([x['job_id'] for x in browser._conditional_presentation_matches(ctx)],[JOB])
        self.assertFalse(m['primary_recommendation_eligible'])
        self.assertEqual(m['source_task_fit']['conditions'],[])
        self.assertEqual(m['affirmative_fit']['unmodeled_requirements'],['Title-defining role or specialization'])
        self.assertIn('title',m['source_task_fit']['candidate_note'])
        detail,packet=f.detail(run)
        self.assertTrue(all(r['status']=='supported' for r in packet['comparisons']))
        for page in (response,detail):
            self.assertIn(b'The title-defined role fit still needs confirmation.',page.body)
            self.assertIn(b'Why this is a possibility',page.body)
        self.assertEqual(packet['placement_explanation']['conditions'],[])

    def test_unsupported_and_contradicted_routes_stay_omitted(self):
        for p in (degree_profile(None),degree_profile('biology'),degree_profile('no_degree',years=2)):
            with self.subTest(education=p['education']):
                f=self.fixture(p,body())
                _,_,ctx,m=f.current()
                self.assertEqual(browser._primary_presentation_matches(ctx),[])
                self.assertEqual(browser._conditional_presentation_matches(ctx),[])
                self.assertFalse(m.get('conditional_task_fit'))

    def test_independent_background_and_required_conflicts_remain_decisive(self):
        for extra in ('5+ years of relevant professional experience in sales','Experience with Python','Native German'):
            with self.subTest(extra=extra):
                p=degree_profile('marketing')
                if extra=='Experience with Python':
                    from tests.test_professional_background_components import confirmed
                    p['constraints']['hard_constraints'].append('I have no experience with Python')
                    confirmed(p,'constraints.hard_constraints[0]')
                if extra=='Native German':
                    from wahojobs.profiles.canonical_v2 import _material_field_paths,validate_canonical_profile_v2
                    from tests.test_professional_background_components import confirmed
                    p['languages'].append(dict(language='German',proficiency='basic',locale='',confidence='high'))
                    p['provenance']['field_sources']=[]
                    for path in _material_field_paths(p): confirmed(p,path)
                    p=validate_canonical_profile_v2(p)
                f=self.fixture(p,body(extra=extra))
                _,_,ctx,_=f.current()
                self.assertEqual(browser._conditional_presentation_matches(ctx),[])

    def test_preferred_nonadjacent_ambiguous_and_standalone_degrees_do_not_open_route(self):
        base='Key Responsibilities\n\nEvaluate AI outputs and provide feedback.\n\nRequirements\n\n'
        clause='5+ years of relevant professional experience in marketing'
        for text in (base+'Bachelor degree in marketing',
            base+clause+'\n\nWorking proficiency in Python\n\nAlternatively, a degree in marketing is sufficient.',
            base+clause+'\n\nAlternatively, a degree in marketing may be sufficient.',
            base+clause+'\n\nPreferred qualifications\n\nAlternatively, a degree in marketing is sufficient.'):
            with self.subTest(text=text):
                f=self.fixture(degree_profile('marketing',years=2),text)
                _,_,ctx,_=f.current()
                self.assertEqual(browser._conditional_presentation_matches(ctx),[])

    def test_supported_alternative_obeys_preferences_and_same_scope_shortfall(self):
        from tests.test_profile_preference_model import with_preference_model
        from wahojobs.profiles.preference_model import empty_profile_preferences_v1
        for kind,expected in (('preferred',[JOB]),('strict',[])):
            with self.subTest(kind=kind):
                model=empty_profile_preferences_v1()
                model['compensation']=dict(minimum_kind=kind,amount='25',currency='USD',period='hour')
                p=with_preference_model(degree_profile('marketing',years=2),model)
                f=self.fixture(p,body())
                _,_,ctx,m=f.current()
                row=m['source_qualification_comparisons'][0]
                self.assertEqual(row['status'],'supported')
                self.assertEqual(row['components']['required_duration']['status'],'contradicted')
                self.assertEqual([x['job_id'] for x in browser._conditional_presentation_matches(ctx)],expected)
                self.assertIn('canonical:900002',ctx['_typed_preference_enforcement']['candidate_references'])

    def test_sibling_source_and_stale_inventory_do_not_borrow_supported_route(self):
        f=self.fixture(degree_profile('marketing'),body(),sibling=True)
        _,run,ctx,m=f.current()
        self.assertEqual([x['job_id'] for x in browser._conditional_presentation_matches(ctx)],[JOB])
        detail,packet=f.detail(run,JOB+1)
        self.assertNotIn(b'Your confirmed degree supports this alternative',detail.body)
        stale=self.fixture(degree_profile('marketing'),body(),source_age_hours=169)
        _,_,ctx,_=stale.current()
        self.assertEqual(browser._conditional_presentation_matches(ctx),[])
        self.assertEqual(browser._primary_presentation_matches(ctx),[])

    def test_replaced_source_invalidates_group_support_and_old_run_detail(self):
        import sqlite3
        f=self.fixture(degree_profile('marketing'),body(extra=''))
        _,old_run,old_context,old_match=f.current()
        self.assertEqual([x['job_id'] for x in browser._conditional_presentation_matches(old_context)],[JOB])
        old_hash=old_match['accepted_task_fit']['source_reference']['material_content_sha256']
        old_group=deepcopy(old_match['source_qualification_comparisons'][0]['components']['qualifying_routes'])
        with f.connections.writable_connection_provider() as connection:
            connection.row_factory=sqlite3.Row
            f._capture(connection,body(alternative=False),JOB)
            connection.commit()
        _,_,current,match=f.current()
        self.assertNotEqual(match['accepted_task_fit']['source_reference']['material_content_sha256'],old_hash)
        self.assertEqual(browser._conditional_presentation_matches(current),[])
        self.assertNotIn('qualifying_routes',match['source_qualification_comparisons'][0]['components'])
        response,packet=f.detail(old_run)
        self.assertNotIn(b'Your confirmed degree supports this alternative',response.body)
        self.assertNotIn(b'The title-defined role fit still needs confirmation.',response.body)
        self.assertNotIn('qualifying_routes',packet['comparisons'][0]['components'])
        self.assertEqual(old_match['source_qualification_comparisons'][0]['components']['qualifying_routes'],old_group)


if __name__=='__main__': unittest.main()
