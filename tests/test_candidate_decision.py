from copy import deepcopy
from html import unescape
import json
from pathlib import Path
import unittest

from tests.test_candidate_condition_comparisons import prepared, profile
from wahojobs.candidate_decision import attach_decision, render_assessment, render_reasons, missing_language_fact
from wahojobs.profile_opportunity_navigation import render_profile_update


class CandidateDecisionTests(unittest.TestCase):
    def test_empty_results_explain_hidden_choices_without_claiming_profile_mismatch(self):
        from wahojobs.authenticated_profile_matches import _render_match_results
        html = _render_match_results({'matches': {}, '_hidden_posting_ids': [42]}, inventory_count=5)
        self.assertIn('/tracker?view=hidden', html)
        self.assertIn('hidden by your saved choices', html)
        self.assertNotIn('None of the available opportunities is a clear fit', html)

    def test_tool_correction_uses_existing_collection_and_preserves_partial_confirmation(self):
        from tests.candidate_decision_support import demo_profile
        from tests.test_professional_background_components import confirmed
        from wahojobs.candidate_condition_comparisons import compare_conditions
        p=demo_profile()
        self.assertNotIn('Python', p['skills']['software_tools'])
        for quote in ('Experience with Python', 'Experience with Python required', '**Experience with Python**'):
            packet=prepared('## Requirements\n- '+quote,p)
            self.assertIn('focus=skills',render_profile_update(packet,'/job/opportunity-3809?variant=11242'))
        p['experience']['item_details']=[dict(item_id='a'*32,field='skills',label='Python',contexts=['professional'],autonomy='independent',months=None,basis='self_reported')]
        from wahojobs.profiles.canonical_v2 import _material_field_paths
        p['provenance']['field_sources']=[]
        for path in _material_field_paths(p): confirmed(p,path)
        for quote,expected in [('Experience with Python',''),('Experience with Python and R','focus=software_tools')]:
            packet=prepared('## Requirements\n'+quote,p)
            packet['comparisons']=compare_conditions(packet,p,include_item_experience=True)
            link=render_profile_update(packet,'/job/opportunity-3809?variant=11242')
            if expected:
                self.assertIn(expected,link)
                self.assertIn('saved tool details stay in place',link)
            else: self.assertEqual(link,'')

    def test_varied_existing_personas_and_preserved_provider_sources(self):
        from scripts.profile_matching_coverage import canonical_profile_for_persona
        from wahojobs.profiles.canonical import complete_trusted_fixture_provenance
        from wahojobs.profiles.canonical_v2 import convert_v1_to_v2
        from tests.test_authenticated_card_evidence import card
        from wahojobs.authenticated_card_evidence import prepare_card_evidence, render_card_evidence
        from tests.test_candidate_source_display import detail
        from wahojobs.authenticated_source_detail import prepare_detail_display
        from tests.test_provider_detail_recovery import CASES,candidate,response
        from wahojobs.crawler.provider_details import recover_detail
        root=Path(__file__).parent/'fixtures'
        names={'beginner_bilingual_generalist','multilingual_language_specialist','transcription_data_entry',
               'software_engineer','biology_researcher','healthcare_interested_uncredentialed'}
        personas=[p for p in json.loads((root/'product_readiness_personas_v1.json').read_text())['personas'] if p['persona_id'] in names]
        self.assertEqual(len(personas),len(names))
        sources=json.loads((root/'card_source_wording.json').read_text(encoding='utf-8-sig'))
        sources+=json.loads((root/'language_task_source_examples.json').read_text(encoding='utf-8-sig'))
        for case in CASES:
            recovered=recover_detail(case['provider'],candidate(case),response(case))
            sources.append(dict(deepcopy(sources[0]),external_id=case['external_id'],content_external_id=case['external_id'],
                url=case['url'],source_url=case['url'],source_slug=case['provider'],body=recovered.source_body,
                body_format=recovered.source_body_format,metadata_json=json.dumps(recovered.source_metadata)))
        states=set();providers=set(); profiles=[]
        for persona in personas:
            p=convert_v1_to_v2(json.loads(json.dumps(complete_trusted_fixture_provenance(canonical_profile_for_persona(persona)))),
                persistent_profile_id='prf_0123456789abcdef0123456789abcdef',source_ordinal_resolver=lambda *_:[1])
            profiles.append((persona['persona_id'], p))
        profiles.append(('existing_portugal_biology',profile()))
        for name,p in profiles:
            for source in sources:
                with self.subTest(persona=name,provider=source['source_slug'],source=source['external_id']):
                    packet=prepare_card_evidence(card(source),source,p,include_item_experience=True)
                    original=deepcopy(packet)
                    html=unescape(render_card_evidence(packet,'sample',profile_return_to='/job/opportunity-3809?variant=11242'))
                    self.assertEqual(packet,original,'Rendering is not authority to change an assessment')
                    self.assertEqual(packet['comparisons'],prepare_detail_display(detail(source),p)['comparisons'])
                    for row in packet['comparisons']:
                        self.assertIn(unescape(row['source']['quote']),html)
                        states.add(row['status'])
                    providers.add(source['source_slug'])
        self.assertTrue({'mercor','alignerr','micro1'}<=providers)
        self.assertTrue({'supported','unresolved','not_established'}<=states)

    def test_reasons_use_recorded_inputs_not_narrative_or_posthoc_overlap(self):
        packet=prepared()
        match={'affirmative_fit_why':['You know React and TypeScript'],
               'affirmative_fit':{'supported_evidence':[dict(requirement='Frontend Development',profile_evidence='Frontend Development',source='profile_or_reviewed_adjacency')]}}
        before=deepcopy(match)
        attach_decision(packet,match)
        text=render_reasons(packet)
        self.assertIn('Frontend Development',text)
        self.assertNotIn('React',text); self.assertNotIn('TypeScript',text)
        self.assertIn('self-reported',text);self.assertEqual(match,before)
        match['affirmative_fit']['supported_evidence']=[dict(requirement='AI evaluation',profile_evidence='interest',source='preference')]
        self.assertIn('interest does not establish experience',render_reasons(attach_decision(packet,match)))

    def test_source_mismatch_cannot_decorate_reason_or_language(self):
        packet=prepared()
        match={'accepted_task_fit':dict(source_reference={'job_id':999},facts=['evaluation'],profile_facts=['work']),
               'source_language_checks':[dict(source_reference={'job_id':999},message='incorrect') ]}
        attach_decision(packet,match)
        self.assertEqual(packet['decision_reasons'],[]);self.assertEqual(packet['language_comparisons'],[])

    def test_unknown_preferred_waiver_conflict_and_profile_partial_remain_distinct(self):
        packet=prepared('## Requirements\nExperience with Python\n\nMust have a mysterious qualification.\n\nNo prior AI experience required\n\n## Preferred\nPublications')
        html=unescape(render_assessment(packet))
        for text in ('not established','This does not mean you lack it','Not required','Employer preference'):
            self.assertIn(text,html)
        p=profile();p.setdefault('constraints',{})['hard_constraints']=['I have no experience with Python']
        from tests.test_professional_background_components import confirmed
        confirmed(p,'constraints.hard_constraints[0]')
        conflict=prepared('## Requirements\nExperience with Python',p)
        self.assertIn('Conflicts with your profile',render_assessment(conflict,compact=True))

    def test_alternative_degree_support_never_claims_required_duration(self):
        from tests.matching_delivery_support import degree_profile, body
        packet=prepared(body(extra='',alternative=True),degree_profile('marketing',role='Marketing specialist',years=2))
        html=unescape(render_assessment(packet))
        self.assertEqual(html.count('Alternative qualifying routes'),1)
        self.assertIn('5+ years',html);self.assertIn('Alternatively, a degree in marketing is sufficient.',html)
        self.assertIn('degree supports this alternative',html)
        self.assertIn('2',html)
        self.assertNotIn('<strong>Supported by your profile</strong>',html)

    def test_missing_language_link_and_late_consumed_conflict(self):
        packet=prepared('## Requirements\nNative German')
        ref=dict(job_id=packet['job_id'],external_id=packet['external_id'],source_url=packet['url'],material_content_sha256=packet['source_hash'])
        check=dict(source_reference=ref,quote='Native German',languages=['german'],levels=['native'],operator='all_of',
                   modality='required',status='unresolved',profile_facts=[],message='German proficiency is not stated.')
        match=dict(source_language_checks=[check])
        attach_decision(packet,match)
        self.assertIn('focus=languages',render_profile_update(packet,'/job/opportunity-3809?variant=11242'))
        self.assertIn('Language level is missing',render_assessment(packet,compact=True))
        ambiguous=dict(check,modality='unresolved')
        self.assertFalse(missing_language_fact(ambiguous))
        match['source_task_fit']=dict(source_reference=ref,conditions=[dict(kind='language',status='contradicted',modality='required',
            message='Your confirmed profile rules out native German.',profile_facts=[dict(language='German',levels=['native'])],
            source=dict(job_id=packet['job_id'],url=packet['url'],quote='Native German'))])
        attach_decision(packet,match)
        self.assertIn('Language requirement conflicts',render_assessment(packet,compact=True))
        self.assertNotIn('focus=languages',render_profile_update(packet,'/job/opportunity-3809?variant=11242'))

    def test_existing_tool_details_are_reused_without_repeated_prompt(self):
        from tests.candidate_decision_support import demo_profile
        p=demo_profile();p['skills']['software_tools']=['Python']
        from tests.test_professional_background_components import confirmed
        confirmed(p,'skills.software_tools[0]')
        packet=prepared('## Requirements\nExperience with Python',p)
        self.assertIn('already saved',render_profile_update(packet,'/job/opportunity-3809?variant=11242'))
        p.setdefault('experience',{})['item_details']=[dict(item_id='a'*32,field='software_tools',label='Python',contexts=['professional'],autonomy='independent',months=12,basis='self_reported')]
        from wahojobs.profiles.canonical_v2 import _material_field_paths
        p['provenance']['field_sources']=[]
        for path in _material_field_paths(p): confirmed(p,path)
        from wahojobs.candidate_condition_comparisons import compare_conditions
        packet['comparisons']=compare_conditions(packet,p,include_item_experience=True)
        self.assertEqual(packet['comparisons'][0]['status'],'supported')
        self.assertEqual(render_profile_update(packet,'/job/opportunity-3809?variant=11242'),'')


class CandidateDecisionHTTPTests(unittest.TestCase):
    def test_typed_preferences_formdata_confirmation_and_fresh_return(self):
        from tests.candidate_decision_support import decision_state, observe, verified_https_request
        from unittest.mock import patch
        from tests.candidate_continuity_support import running_process
        from tests.test_candidate_continuity_client import run_client
        script=Path(__file__).with_name('candidate_decision_client.cjs')
        with decision_state(typed_preferences=True) as state, patch('tests.test_candidate_continuity_client.https_request', verified_https_request):
            with running_process(state):
                run_client(state,'decision-preferences',script=script,observe=observe)
            before=observe(state)
            with running_process(state):
                run_client(state,'decision-preferences-return',script=script,observe=observe)
            self.assertEqual(observe(state),before)

    def test_served_correction_workflow_and_fresh_return(self):
        from tests.candidate_decision_support import decision_state, observe, verified_https_request
        from unittest.mock import patch
        from tests.candidate_continuity_support import running_process
        from tests.test_candidate_continuity_client import run_client
        script=Path(__file__).with_name('candidate_decision_client.cjs')
        with decision_state() as state, patch('tests.test_candidate_continuity_client.https_request', verified_https_request):
            with running_process(state):
                result=run_client(state,'decision',script=script,observe=observe)
            before=observe(state)
            self.assertEqual(len(before['revisions']),3)
            self.assertEqual(len(before['items']),1)
            self.assertEqual(before['items'][0]['workflow_status'],'applied')
            with running_process(state):
                run_client(state,'decision-return',script=script,observe=observe)
            self.assertEqual(observe(state),before)


if __name__=='__main__': unittest.main()
