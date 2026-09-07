"""Accepted public clauses and synthetic profiles; no personal recovery data."""
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

from tests import test_accepted_task_matching as task_support
from tests.test_confirmed_activity_matching import candidate, v2
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests import test_profile_review_transfer as review_support
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_variant_details import variant_detail_url
from wahojobs.matching.languages import prepare_language_conditions, compare_language_condition
from wahojobs.matching.accepted_tasks import _prepare_eligibility
from wahojobs.profiles.canonical_v2 import project_v2_to_matcher_v1
from wahojobs.profiles.canonical import canonical_to_matcher_profile


GERMAN_CLAUSES = (
    'Native or near-native German speaker living in the United States',
    'Native German speaker — Austrian German speakers strongly encouraged to apply',
    'Native or near-native German speaker — whether through upbringing, long-term residence in a German-speaking country, or advanced professional fluency',
    'Native or near-native German speaker — Swiss German speakers and High German speakers based in Switzerland are strongly encouraged to apply',
    'Native or near-native fluency in German, plus business-level written English.',
)


def profile(level='basic', residence='Brazil', activities=('AI evaluation',)):
    c = candidate(activities)
    c['languages'].append(dict(language='German', proficiency=level, locale='', confidence='high'))
    c['languages'][0]['proficiency'] = 'fluent'
    c['languages'][1]['proficiency'] = 'native'
    c['location']['residence'] = residence
    c['location']['country'] = residence
    from wahojobs.profiles.canonical import field_sources_for_profile
    c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
    return v2(c)


class LanguageProficiencyComparisonTests(unittest.TestCase):
    def check(self, quote, level='basic', mode='required'):
        clauses = prepare_language_conditions(quote, mode)
        self.assertTrue(clauses)
        return [compare_language_condition(profile(level), c) for c in clauses]

    def test_five_public_clauses_reject_basic_not_presence(self):
        for clause in GERMAN_CLAUSES:
            with self.subTest(clause=clause):
                checks = self.check(clause)
                self.assertEqual(checks[0]['status'], 'contradicted')
                self.assertEqual(checks[0]['modality'], 'required')
                self.assertEqual(checks[0]['quote'], clause)
                self.assertTrue(checks[0]['profile_facts'])

    def test_unknown_advanced_native_and_no_cefr_inference(self):
        for level, status in [('unknown','unresolved'), ('unspecified','unresolved'),
                              ('advanced','unresolved'), ('fluent','unresolved'), ('native','supported')]:
            self.assertEqual(self.check('Native or near-native German',level)[0]['status'],status)
        self.assertEqual(prepare_language_conditions('German at CEFR C2','required'),[])

    def test_preferred_negated_and_conflicting_modality_do_not_veto(self):
        for clause, mode, expected in [('Native German','preferred','preferred'),
            ('Native German not required','required','not_required'),
            ('Not native German','required','not_required'),
            ('Native German preferred','required','unresolved'),
            ('Native German unless another language is accepted','required','unresolved')]:
            self.assertEqual(self.check(clause,mode=mode)[0]['modality'],expected)
        self.assertEqual(prepare_language_conditions('Non-native German speakers welcome','required'), [])
        for wording in ('Native German citizens', 'Native German nationals', 'Native German customers'):
            self.assertEqual(prepare_language_conditions(wording,'required'), [])

    def test_alternatives_and_conjunction_are_not_flattened(self):
        self.assertEqual(self.check('Native German or Portuguese')[0]['status'],'supported')
        self.assertEqual(self.check('Native German and Portuguese')[0]['status'],'contradicted')
        self.assertEqual(self.check('Native German and Portuguese or English')[0]['status'],'unresolved')
        for check in self.check('Native German or fluent English'):
            self.assertEqual(check['status'],'unresolved')
        missing=compare_language_condition({'languages':['German']},prepare_language_conditions('Native German','required')[0])
        self.assertNotEqual(missing['status'],'contradicted')

    def test_no_title_or_incidental_context_and_conflicting_source_release(self):
        def prepared(body): return _prepare_eligibility('hash','provider','id','https://example.test/x',body,'text/plain','{}')
        for body in ['Native German AI Expert','About us\n\nNative German customers use our product.',
                     'Responsibilities\n\nEvaluate German outputs for native German customers.']:
            self.assertEqual(prepared(body),((),()))
        rows,_=prepared('Requirements\n\nNative German\n\nNative German not required')
        self.assertEqual(rows[0]['modality'],'unresolved')

    def test_compound_residence_alternatives_and_unrelated_dimensions(self):
        from wahojobs.matching.source_geography import prepare_applicant_residence_clause as prepare
        c=prepare('Native German speaker living in the United States or Brazil','required','source')
        self.assertEqual(c['countries'],['Brazil','United States'])
        self.assertEqual(c['dimension'],'residence')
        for quote,mode in [('Native German speaker living in the United States','preferred'),
            ('Native German speaker living in the United States preferred','required'),
            ('Native German speaker not living in the United States','required'),
            ('Our headquarters are in Germany','required'),('German nationality is required','required'),
            ('Must be authorized to work in the United States','required'),
            ('Working with customers based in Germany','required')]:
            self.assertIsNone(prepare(quote,mode,'source'))

    def test_conflicting_profile_levels_and_absent_proficiency_are_unknown(self):
        r=prepare_language_conditions('Native German','required')[0]
        for p in ({'languages':['German']}, {'languages':[{'language':'German','proficiency':'unknown'}]},
            {'languages':[{'language':'German','proficiency':'native'},{'language':'German','proficiency':'basic'}]}):
            self.assertEqual(compare_language_condition(p,r)['status'],'unresolved')


class AuthenticatedLanguageSafetyTests(unittest.TestCase):
    role = task_support.AcceptedTaskMatchingTests.role
    source = task_support.AcceptedTaskMatchingTests.source
    current = task_support.AcceptedTaskMatchingTests.current
    match = task_support.AcceptedTaskMatchingTests.match

    def setUp(self):
        self.f=SyntheticMatcherFixture();self.addCleanup(self.f.close)
        self.f.profile=profile();self.role('German AI Data Reviewer', 'Remote')

    def condition(self, clause, *, heading='Who You Are', job_id=7003):
        self.source('Scope of Work\n\nEvaluate AI outputs.\n\n'+heading+'\n\n'+clause,job_id=job_id)

    def ids(self,ctx):
        return {m['job_id'] for m in browser._primary_presentation_matches(ctx)+browser._conditional_presentation_pool(ctx)}

    def test_all_five_are_excluded_before_cutoff_and_all_fallbacks(self):
        from scripts import profile_to_matches_preview as preview
        for clause in GERMAN_CLAUSES:
            with self.subTest(clause=clause):
                self.f.profile=profile()
                self.condition(clause)
                _,run,ctx=self.current();m=self.match(ctx)
                self.assertEqual(m['affirmative_fit_status'],'conflicting')
                self.assertNotIn(7003,self.ids(ctx))
                self.assertIn('mandatory_language_proficiency_conflict',m['actionability_cap_reasons'])
                self.assertFalse(browser.local_product.recent_cached_match_is_usable(m,allow_conditional_task_fit=True))
                self.assertFalse(preview.safe_generic_language_primary_action(m,{'german'}))
                self.assertNotIn(7003,self.ids(preview.refresh_preview_context_freshness(ctx,self.f.now)))
                self.assertTrue(all(7003 not in {x['job_id'] for x in scenario['matches']}
                                    for scenario in browser._presented_relaxation_scenarios(ctx)))
                # Profile changes and a reused old URL use current proficiency.
                self.f.profile=profile('native','United States')
                _,_,updated=self.current('/find-matches?run='+run.match_run_id)
                self.assertNotEqual(self.match(updated)['affirmative_fit_status'],'conflicting')
                self.f.profile=profile()

    def test_compound_residence_is_separate_from_language_and_permission(self):
        self.f.profile=profile('native')
        self.condition(GERMAN_CLAUSES[0])
        _,_,ctx=self.current();m=self.match(ctx)
        self.assertEqual(m['source_applicant_location_check']['status'],'incompatible')
        self.assertEqual(m['source_language_checks'][0]['status'],'supported')
        self.assertNotIn(7003,self.ids(ctx))
        self.f.profile['location']['work_authorization']='United States'
        _,_,ctx=self.current();self.assertNotIn(7003,self.ids(ctx))
        self.f.profile=profile('native','United States')
        _,_,ctx=self.current();self.assertIn(7003,self.ids(ctx))

    def test_unknown_preferred_alternative_and_no_experience_are_not_conflicts(self):
        for level,heading,clause in [('unknown','Who You Are','Native German'),
                ('advanced','Who You Are','Native or near-native German'),
                ('basic','Preferred Qualifications','Native German'),
                ('basic','Who You Are','Native German or Portuguese'),
                ('basic','Requirements','Native German not required. No experience required.')]:
            with self.subTest(level=level,clause=clause):
                self.f.profile=profile(level);self.condition(clause,heading=heading)
                _,_,ctx=self.current();self.assertIn(7003,self.ids(ctx))
                self.assertFalse(self.match(ctx)['affirmative_fit']['conflicting_requirements'])

    def test_evidence_and_profile_invalidate_old_run_without_score_mutation(self):
        self.f.profile=profile('native');self.condition('Native German')
        _,old,ctx=self.current();score=deepcopy(self.match(ctx)['score_components'])
        self.f.profile=profile('basic')
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertNotIn(7003,self.ids(ctx));self.assertEqual(self.match(ctx)['score_components'],score)
        self.condition('Native Portuguese')
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertIn(7003,self.ids(ctx))
        self.f.owner='b';self.assertEqual(self.f.get('/find-matches?run='+old.match_run_id).status,410)

    def test_variant_selection_eligible_unknown_and_no_cross_variant_leakage(self):
        # Resolve the fixture's actual first canonical rather than invent one.
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.f.path)) as c:
            canonical=c.execute('SELECT canonical_opportunity_id FROM jobs WHERE id=7003').fetchone()[0]
        self.f.update_inventory('UPDATE jobs SET canonical_opportunity_id=? WHERE id=7006',(canonical,))
        self.condition('Native German')
        for alternative in ['Native Portuguese','Native or near-native English']:
            self.condition(alternative,job_id=7006)
            _,run,ctx=self.current()
            selected=[m for values in ctx['matches'].values() for m in values]
            self.assertEqual(selected[0]['job_id'],7006)
            self.assertIn(7006,self.ids(ctx))
            self.assertEqual(selected[0]['source_language_checks'][0]['source_reference']['job_id'],7006)
            url=variant_detail_url(selected[0],run_id=run.match_run_id)
            self.assertEqual(self.f.get(url).status,200)
            self.assertEqual(self.f.get(url.split('?')[0]).status,200)

    def test_recent_cache_expiry_closure_and_relaxation_never_resurrect_conflict(self):
        self.condition('Native German')
        _,old,_=self.current()
        self.f.advance(73)
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertNotIn(7003,self.ids(ctx))
        self.f.advance(24*8)
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertNotIn(7003,self.ids(ctx))
        self.f.update_inventory('UPDATE jobs SET is_active=0 WHERE id=7003')
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertNotIn(7003,self.ids(ctx))

    def test_nonempty_relaxation_excludes_conflicts_and_preserves_unknown_alternative(self):
        self.f.update_inventory("UPDATE jobs SET location='Remote - Brazil'")
        self.condition('Native German')
        self.source('Scope of Work\n\nEvaluate AI outputs.',job_id=7006)
        self.f.update_inventory("UPDATE jobs SET commitment='Full-time' WHERE id=7003")
        self.f.update_inventory("UPDATE jobs SET commitment='Part-time' WHERE id=7006")
        self.f.set_preferences('full_time')
        _,_,ctx=self.current()
        scenarios=browser._presented_relaxation_scenarios(ctx)
        self.assertTrue(scenarios)
        unlocked={m['job_id'] for s in scenarios for m in s['matches']}
        self.assertIn(7006,unlocked)
        self.assertNotIn(7003,unlocked)

    def test_preparation_reuse_and_configuration_change_invalidate_old_context(self):
        from wahojobs.matching import accepted_tasks
        self.condition('Native Portuguese')
        _,old,_=self.current()
        misses=_prepare_eligibility.cache_info().misses
        self.current()
        self.assertEqual(_prepare_eligibility.cache_info().misses,misses)
        with patch.object(accepted_tasks,'SOURCE_ELIGIBILITY_VERSION',999):
            _,new,_=self.current('/find-matches?run='+old.match_run_id)
        self.assertNotEqual(old.match_run_id,new.match_run_id)


class CompactLanguageEditingTests(unittest.TestCase):
    setUp = review_support.ProfileReviewTransferTests.setUp
    current = review_support.ProfileReviewTransferTests.current
    get = review_support.ProfileReviewTransferTests.get
    review = review_support.ProfileReviewTransferTests.review
    apply = review_support.ProfileReviewTransferTests.apply
    def test_edit_review_apply_retains_levels_and_projection(self):
        before=self.current()
        page,form=self.review(dict(language_0='German',language_proficiency_0='basic'))
        self.assertEqual(self.current(),before)
        edit=next(x for x in self.f._markup(page).links if 'correction=edit' in x)
        markup=self.get(edit).body.decode()
        self.assertIn("name='language_proficiency_0'",markup)
        self.assertEqual(self.apply(form).status,200)
        saved=self.current()
        german=next(x for x in saved['languages'] if x['language']=='German')
        self.assertEqual(german['proficiency'],'basic')
        projected=canonical_to_matcher_profile(project_v2_to_matcher_v1(saved,matcher_profile_id='test'))
        self.assertEqual(projected['language_proficiency']['German'],'basic')
        condition=prepare_language_conditions('Native German','required')[0]
        self.assertEqual(compare_language_condition(projected,condition)['status'],'contradicted')
