"""Public clause forms, synthetic profiles, and the existing authenticated fixture."""
from copy import deepcopy
import json
import unittest

from tests import test_accepted_task_matching as task_tests
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_confirmed_activity_matching import candidate, v2
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_card_evidence import prepare_card_evidence
from wahojobs.candidate_condition_comparisons import _professional_background, compare_conditions
from wahojobs.matching.languages import prepare_language_conditions, compare_language_condition
from wahojobs.profiles.canonical import field_sources_for_profile


MUSIC = '3+ years of hands-on music or audio experience'
SUPPORT = 'Experienced support engineer, technical support specialist, or customer success engineer with hands-on product support experience.'
MARKETING = 'Hands-on experience in a marketing role — demand generation, content marketing, product marketing, or a closely related function.'


def reviewed(profile):
    profile=deepcopy(profile)
    profile['provenance']['field_sources']=field_sources_for_profile(profile,'user_correction',explicit=True)
    return v2(profile)


class BackgroundComparisonTests(unittest.TestCase):
    def test_generic_task_work_language_interest_and_total_years_are_not_a_profession(self):
        for facts in [candidate(['AI evaluation','Audio annotation']), candidate(['Interested in music']), candidate()]:
            facts['experience']['total_years']=20
            for quote in [MUSIC,SUPPORT,MARKETING,
                          'Hands-on experience in revenue operations, sales operations, or a closely related function at an industrial or enterprise company']:
                result=_professional_background(quote,facts)
                self.assertEqual(result[0],'not_established')
                self.assertFalse(result[3])

    def test_direct_or_transferable_evidence_keeps_depth_and_duration_unknown(self):
        for quote,field,value in [(MUSIC,'recent_roles','Music producer'),
                                  (MUSIC,'recent_roles','Audio engineer'),
                                  (SUPPORT,'recent_roles','Technical support specialist')]:
            profile=candidate();profile['experience'][field]=[value]
            result=_professional_background(quote,reviewed(profile))
            self.assertEqual(result[0],'unresolved')
            self.assertTrue(result[3]);self.assertTrue(result[2])
        profile=candidate(skills=['Digital Marketing'])
        self.assertFalse(_professional_background(MARKETING,reviewed(profile))[3])
        self.assertNotEqual(_professional_background(MARKETING,reviewed(profile))[0],'supported')

    def test_same_marketing_requirement_preserves_study_project_mention_and_role(self):
        for field,value,context,related in [
                ('education','Marketing','study',False),
                ('skills','Digital Marketing','skill mention',False),
                ('roles','Marketing course student','study',False),
                ('roles','Marketing volunteer projects','projects',False),
                ('roles','Content marketing specialist','professional role',True)]:
            profile=candidate()
            if field=='education': profile['education']['fields_or_domains']=[value]
            elif field=='skills': profile['skills']['normalized']=[value]
            else: profile['experience']['recent_roles']=[value]
            result=_professional_background(MARKETING,reviewed(profile))
            self.assertEqual(result[0],'unresolved')
            self.assertEqual(bool(result[3]),related)
            self.assertIn(context,{f['context'] for f in result[2]})
            self.assertNotEqual(result[0],'contradicted')

    def test_explicit_denials_require_confirmed_provenance_and_preserve_or(self):
        profile=candidate()
        profile['constraints']['hard_constraints']=['No experience in music']
        profile['provenance']['field_sources']=field_sources_for_profile(profile,'user_correction',explicit=True)
        self.assertEqual(_professional_background(MUSIC,reviewed(profile))[0],'not_established')
        profile['constraints']['hard_constraints'].append('No experience in audio')
        profile['provenance']['field_sources']=field_sources_for_profile(profile,'user_correction',explicit=True)
        self.assertEqual(_professional_background(MUSIC,reviewed(profile))[0],'contradicted')
        profile=reviewed(profile)
        profile['provenance']['field_sources']=[]
        self.assertEqual(_professional_background(MUSIC,profile)[0],'not_established')

    def test_unsupported_logic_and_waivers_are_not_invented_requirements(self):
        for quote in ['No hands-on music experience required',
                      'Hands-on music experience preferred',
                      'Hands-on music and audio experience',
                      'Hands-on music/audio experience']:
            self.assertIsNone(_professional_background(quote,candidate()))
        packet=dict(job_id=1,external_id='example',url='https://example.test/x',source_hash='hash',
                    captured_at='2026-09-05',caveats=[],kind='Advertised role',conditions=[dict(
                        heading='Preferred Qualifications',reference='source block 1',text=MUSIC)])
        result=compare_conditions(packet,candidate())[0]
        self.assertEqual(result['modality'],'preferred')
        self.assertNotEqual(result['status'],'contradicted')

    def test_new_language_and_equipment_self_reports_do_not_rewrite_saved_levels(self):
        profile=candidate();profile['languages'].append(dict(language='Spanish',proficiency='advanced',locale='',confidence='high'))
        original=deepcopy(profile)
        for quote in ['Native or near-native Spanish', 'Bilingual proficiency in Spanish and English']:
            self.assertEqual(compare_language_condition(profile,prepare_language_conditions(quote,'required')[0])['status'],'unresolved')
        self.assertEqual(profile,original)
        profile['constraints']['hard_constraints']=['Not native or near-native Spanish']
        confirmed=reviewed(profile)
        self.assertEqual(compare_language_condition(confirmed,prepare_language_conditions('Native or near-native Spanish','required')[0])['status'],'contradicted')
        self.assertEqual(compare_language_condition(confirmed,prepare_language_conditions('Bilingual proficiency in Spanish and English','required')[0])['status'],'unresolved')
        self.assertEqual(compare_language_condition(confirmed,prepare_language_conditions('Native or near-native Spanish','preferred')[0])['modality'],'preferred')
        profile['constraints']['hard_constraints']=['Not near-native Spanish']
        self.assertEqual(compare_language_condition(reviewed(profile),prepare_language_conditions('Native or near-native Spanish','required')[0])['status'],'unresolved')
        confirmed['provenance']['field_sources']=[]
        self.assertEqual(compare_language_condition(confirmed,prepare_language_conditions('Native or near-native Spanish','required')[0])['status'],'unresolved')
        profile['constraints']['hard_constraints']=[]
        # Separately confirmed basic proficiency is a conflict; advanced is not.
        profile['languages'][-1]['proficiency']='basic'
        self.assertEqual(compare_language_condition(profile,prepare_language_conditions('Native or near-native Spanish','required')[0])['status'],'contradicted')
        # Equipment and intention to purchase cannot be inferred from AI work.
        packet=dict(job_id=1,external_id='e',url='https://example.test/e',source_hash='h',captured_at='2026-09-05',caveats=[],kind='Advertised role',conditions=[dict(heading='Requirements',reference='b1',text='MUST own a Mac with an Apple Silicon chip')])
        for constraint in ['', 'I do not own a Mac; I may buy one']:
            profile['constraints']['hard_constraints']=[constraint] if constraint else []
            result=compare_conditions(packet,profile)[0]
            self.assertEqual(result['status'],'unresolved')  # this clause is deliberately unassessed


class AuthenticatedBackgroundTests(unittest.TestCase):
    role=task_tests.AcceptedTaskMatchingTests.role
    source=task_tests.AcceptedTaskMatchingTests.source
    current=task_tests.AcceptedTaskMatchingTests.current
    match=task_tests.AcceptedTaskMatchingTests.match

    def setUp(self):
        self.f=SyntheticMatcherFixture();self.addCleanup(self.f.close)
        self.f.profile=v2(candidate(['Model output evaluation']))
        self.role('Portuguese AI Data Reviewer')

    def test_unknown_profession_not_conditional_backdoor_old_run_and_variants(self):
        self.source('Key Responsibilities\n\nEvaluate AI outputs.')
        _,old,ctx=self.current();score=self.match(ctx)['score']
        self.source('Key Responsibilities\n\nEvaluate AI outputs.\n\nQualifications\n\n'+SUPPORT)
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id);m=self.match(ctx)
        self.assertEqual(m['score'],score)
        self.assertEqual(m['affirmative_fit_status'],'uncertain')
        self.assertEqual(m['primary_admission_source'],'accepted_task_professional_background')
        self.assertFalse(m['conditional_task_fit'])
        self.assertFalse(m['affirmative_fit']['conflicting_requirements'])
        self.assertNotEqual(self.match(ctx,7006).get('primary_admission_source'),m['primary_admission_source'])
        self.assertFalse(browser.local_product.recent_cached_match_is_usable(m,allow_conditional_task_fit=True))
        self.assertTrue(all(7003 not in {x['job_id'] for x in s['matches']} for s in browser._presented_relaxation_scenarios(ctx)))
        self.assertNotIn(7003,{x['job_id'] for x in browser._primary_presentation_matches(ctx)+browser._conditional_presentation_pool(ctx)})
        improved=candidate(['Model output evaluation']);improved['experience']['recent_roles']=['Technical support specialist']
        self.f.profile=reviewed(improved)
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertIn(7003,{x['job_id'] for x in browser._conditional_presentation_pool(ctx)})
        self.assertFalse(self.match(ctx)['primary_recommendation_eligible'])

    def test_preferred_entry_level_and_explicit_geography_remain_separate(self):
        for qualifications in ['Preferred Qualifications\n\n'+MUSIC,
                               'Requirements\n\nNo previous experience required.']:
            self.source('Key Responsibilities\n\nEvaluate AI outputs.\n\n'+qualifications)
            _,_,ctx=self.current()
            self.assertNotEqual(self.match(ctx).get('primary_admission_source'),'accepted_task_professional_background')
        self.role('Portuguese AI Data Reviewer','Remote - United States only')
        _,_,ctx=self.current()
        self.assertFalse(browser._primary_presentation_matches(ctx))
        self.assertFalse(browser._conditional_presentation_matches(ctx))

    def test_same_sources_contrast_generic_and_relevant_practice_without_score_changes(self):
        for clause,role in [(MUSIC,'Audio engineer'),(SUPPORT,'Technical support specialist'),
                            (MARKETING,'Content marketing specialist')]:
            self.source('Key Responsibilities\n\nEvaluate AI outputs.\n\nQualifications\n\n'+clause)
            generic=candidate(['Model output evaluation'])
            self.f.profile=reviewed(generic)
            _,run,ctx=self.current();original=self.match(ctx)
            self.assertFalse(original['conditional_task_fit'])
            self.assertEqual(original['affirmative_fit_status'],'uncertain')
            self.assertFalse(original['affirmative_fit']['conflicting_requirements'])
            practitioner=deepcopy(generic);practitioner['experience']['recent_roles']=[role]
            self.f.profile=reviewed(practitioner)
            _,_,ctx=self.current('/find-matches?run='+run.match_run_id);related=self.match(ctx)
            self.assertTrue(related['conditional_task_fit'])
            self.assertFalse(related['primary_recommendation_eligible'])
            self.assertEqual(original['score_components'],related['score_components'])
            self.assertIn(7003,{m['job_id'] for m in browser._conditional_presentation_pool(ctx)})
            # The same clause under a preferred heading is not a professional gate.
            self.f.profile=reviewed(generic)
            self.source('Key Responsibilities\n\nEvaluate AI outputs.\n\nPreferred Qualifications\n\n'+clause)
            _,_,ctx=self.current()
            self.assertNotEqual(self.match(ctx).get('primary_admission_source'),'accepted_task_professional_background')

    def test_marketing_study_and_projects_cannot_replace_required_role(self):
        self.source('Key Responsibilities\n\nEvaluate AI outputs.\n\nQualifications\n\n'+MARKETING)
        for role in ['', 'Marketing course student','Marketing volunteer projects']:
            p=candidate(['Model output evaluation'],skills=['Digital Marketing'])
            p['education']['fields_or_domains']=['Marketing']
            p['experience']['recent_roles']=[role] if role else []
            self.f.profile=reviewed(p)
            _,_,ctx=self.current();m=self.match(ctx)
            self.assertFalse(m['conditional_task_fit'])
            self.assertFalse(m['affirmative_fit']['conflicting_requirements'])
            self.assertEqual(m['source_task_fit']['conditions'][0]['modality'],'required')

    def test_confirmed_language_denial_uses_same_conditional_path_without_changing_level(self):
        c=candidate(['Model output evaluation'])
        c['languages'].append(dict(language='Spanish',proficiency='advanced',locale='',confidence='high'))
        self.f.profile=reviewed(c)
        self.role('Spanish AI Data Reviewer')
        self.source('Key Responsibilities\n\nEvaluate AI outputs.\n\nRequirements\n\nNative or near-native Spanish.')
        _,old,ctx=self.current()
        self.assertTrue(self.match(ctx)['conditional_task_fit'])
        c['constraints']['hard_constraints']=['Not native or near-native Spanish']
        self.f.profile=reviewed(c)
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertEqual(self.match(ctx)['affirmative_fit_status'],'conflicting')
        self.assertFalse(browser._conditional_presentation_matches(ctx))
        self.assertEqual(self.f.profile['languages'][-1]['proficiency'],'advanced')


class SourceLocationPresentationTests(unittest.TestCase):
    def source(self,city,country,code,body):
        url='https://www.alignerr.com/jobs/synthetic'
        record=dict(location='United States',city=city,countryCode=code)
        metadata={'wahojobs_source_detail_v1':dict(provider='alignerr',external_id='synthetic',url=url,record=record,display_text=body)}
        source=dict(job_id=1,canonical_opportunity_id=2,external_id='synthetic',url=url,source_slug='alignerr',content_provider='alignerr',content_external_id='synthetic',source_url=url,body=body,body_format='text/markdown',metadata_json=json.dumps(metadata),last_captured_at='2026-09-05',material_content_sha256='hash',location='Remote')
        match={k:source[k] for k in ('job_id','canonical_opportunity_id','url','source_slug')}
        match.update(location_eligibility_status='unknown')
        return source,match

    def test_three_same_variant_disagreements_are_employer_questions(self):
        for city,country,code,body in [
            ('Berlin','Germany','DE',"Germany — and Berlin in particular — has a Brazilian community. We're looking for native Portuguese speakers based here."),
            (None,'Singapore','SG','This role is ideal for experienced support engineers, technical support specialists, and customer success professionals in Singapore.'),
            ('Manila','Philippines','PH','As an author based in Manila, you will write tasks.\n\nThis role is open to professionals across the Philippines.')]:
            source,match=self.source(city,country,code,body)
            packet=prepare_card_evidence(match,source,dict(location={'country':'Brazil'}))
            self.assertIn(country,packet['geography']);self.assertIn('United States',packet['geography'])
            self.assertIn('Eligibility from Brazil needs confirmation',packet['geography'])
            self.assertFalse(packet['comparisons'])  # not a candidate-owned claim
            match['job_id']=3;self.assertIsNone(prepare_card_evidence(match,source,{}))
            match['job_id']=1
            metadata=json.loads(source['metadata_json'])
            metadata['wahojobs_source_detail_v1']['external_id']='another-variant'
            source['metadata_json']=json.dumps(metadata)
            self.assertNotIn('description mentions',prepare_card_evidence(match,source,{})['geography'])

    def test_incidental_country_and_supported_applicant_location_do_not_become_conflicts(self):
        for body in ['Our headquarters are in Germany; our customers work in Berlin.',
                     'We serve the Germany customer market from Berlin.']:
            source,match=self.source('Berlin','Germany','DE',body)
            self.assertNotIn('description mentions',prepare_card_evidence(match,source,{})['geography'])
        source,match=self.source('Berlin','Germany','DE','Our role invites applicants in Germany, including Berlin.')
        match['location_eligibility_status']='eligible'
        self.assertEqual(prepare_card_evidence(match,source,{})['geography'],'')


if __name__=='__main__':unittest.main()
