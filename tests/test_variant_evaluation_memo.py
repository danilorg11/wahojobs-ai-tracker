"""Equivalent computation never substitutes identity, source proof or time."""
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from scripts import profile_match_digest as matcher, profile_to_matches_preview as preview
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_confirmed_activity_matching import candidate
from wahojobs.matching.accepted_tasks import TASK_PROJECTION_VERSION, SOURCE_ELIGIBILITY_VERSION
from wahojobs.matching.evaluation_memo import VariantEvaluationMemo
from wahojobs.matching import domains
from wahojobs.profiles.canonical import canonical_to_matcher_profile


class VariantEvaluationMemoTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.profile = canonical_to_matcher_profile(candidate(['Model output evaluation']), include_task_evidence=True)
        self.row = self.f.integration._load_inventory()[0][0]
        self.row.update(title='Portuguese AI Data Reviewer', canonical_title='Portuguese AI Data Reviewer',
                        location='Remote - Brazil', department='', expertise='', source_category='')
        self.memo = VariantEvaluationMemo(self.profile)
        self.score = Mock(wraps=matcher.score_opportunity)

    def variant(self, *, task=False, **changes):
        row = deepcopy(self.row)
        row.update(changes)
        if task:
            row['accepted_task_evidence'] = dict(version=TASK_PROJECTION_VERSION,
                source_reference=dict(job_id=row['job_id'], canonical_opportunity_id=row['canonical_opportunity_id'],
                    source_slug=row['source_slug'], source_url=row['url'], material_content_sha256='synthetic-'+str(row['job_id'])),
                facts=[dict(quote='Evaluate AI responses.', professional_domains=[], block_reference='duties:1')],
                transferable_scope=[], beginner_scope=[])
        return row

    def evaluated(self, row, *, profile=None, now=None):
        profile = profile if profile is not None else self.profile
        result = self.memo.evaluate(profile, row, self.score, preview.apply_semantic_guardrails)
        return preview.complete_variant_guardrails(profile, row, result, evaluated_at=now or self.f.now)

    def reference(self, row, *, profile=None, now=None):
        profile = profile if profile is not None else self.profile
        return preview.apply_preview_guardrails(profile, row, matcher.score_opportunity(profile, row),
                                                evaluated_at=now or self.f.now)

    def test_equivalent_variants_reuse_comparison_but_keep_exact_task_and_trust_receipts(self):
        first = self.variant(task=True)
        second = self.variant(task=True, job_id=8008, url='https://jobs.example.test/other')
        for row in (first, second):
            match = self.evaluated(row)
            self.assertEqual(match, self.reference(row))
            self.assertEqual(match['accepted_task_fit']['source_reference']['job_id'], row['job_id'])
            self.assertEqual(match['url'], row['url'])
        self.assertEqual(self.score.call_count, 1)

    def test_source_binding_and_changed_task_facts_cannot_borrow_previous_fit(self):
        first = self.variant(task=True)
        self.evaluated(first)
        for change in ('missing', 'wrong_identity', 'different_quote', 'different_domain'):
            row = deepcopy(first)
            if change == 'missing':
                row.pop('accepted_task_evidence')
            elif change == 'wrong_identity':
                row['accepted_task_evidence']['source_reference']['job_id'] = -1
            elif change == 'different_quote':
                row['accepted_task_evidence']['facts'][0]['quote'] = 'Rate model responses.'
            else:
                row['accepted_task_evidence']['facts'][0]['professional_domains'] = ['legal']
            self.assertEqual(self.evaluated(row), self.reference(row), change)

    def test_semantic_inputs_and_unknown_future_metadata_are_not_dropped(self):
        self.evaluated(self.row)
        for changes in (dict(title='Korean AI Reviewer'), dict(location='United States'),
                        dict(commitment='Internship'), dict(canonical_opportunity_id=999),
                        dict(source_run_qualifies=False), dict(job_is_active=False),
                        dict(overlay_required_languages=['Korean']), dict(future_metadata='different')):
            row = self.variant(**changes)
            before = self.score.call_count
            self.assertEqual(self.evaluated(row), self.reference(row))
            self.assertEqual(self.score.call_count, before + 1)

    def test_accepted_eligibility_is_checked_even_on_semantic_cache_hit(self):
        row = self.variant(task=True)
        self.evaluated(row)
        from wahojobs.matching.languages import prepare_language_conditions
        row['accepted_eligibility_evidence'] = dict(version=SOURCE_ELIGIBILITY_VERSION,
            source_reference=row['accepted_task_evidence']['source_reference'],
            language_conditions=prepare_language_conditions('Native Korean is required.', 'required'), country_conditions=[])
        self.assertTrue(row['accepted_eligibility_evidence']['language_conditions'])
        result = self.evaluated(row)
        self.assertEqual(result, self.reference(row))
        self.assertEqual(result['affirmative_fit_status'], 'uncertain')
        self.assertFalse(result['primary_recommendation_eligible'])
        self.assertTrue(result['source_language_checks'])
        self.assertEqual(self.score.call_count, 1)

    def test_expiry_is_rechecked_on_hit_and_new_evaluation_has_no_old_state(self):
        self.evaluated(self.row)
        later = self.f.now + timedelta(days=10)
        self.assertEqual(self.evaluated(self.row, now=later), self.reference(self.row, now=later))
        self.assertEqual(self.score.call_count, 1)
        self.memo = VariantEvaluationMemo(self.profile)
        self.evaluated(self.row)
        self.assertEqual(self.score.call_count, 2)

    def test_nested_mutation_and_separate_profiles_do_not_change_another_result(self):
        first = self.evaluated(self.row)
        first['score_components']['raw_matcher_score'] = -500
        first['affirmative_fit']['missing_requirements'] = ('corrupted',)
        first['reasons'].append('corrupted')
        result = self.evaluated(self.row)
        result['affirmative_fit_supported_evidence'].append({'corrupted': True})
        self.assertEqual(self.evaluated(self.row), self.reference(self.row))
        other = deepcopy(self.profile)
        other['country'] = 'Korea'
        self.assertEqual(self.evaluated(self.row, profile=other), self.reference(self.row, profile=other))
        self.assertEqual(self.evaluated(self.row), self.reference(self.row))

    def test_capacity_and_unsupported_types_fall_back_without_changing_decisions(self):
        self.memo.MAX_ENTRIES = 2
        for n in range(5):
            row = self.variant(future_metadata=n)
            self.assertEqual(self.evaluated(row), self.reference(row))
        self.assertEqual(len(self.memo._cache), 2)
        for value in (('tuple',), {'set'}, 'x' * 70000):
            row = self.variant(future_metadata=value)
            self.assertEqual(self.evaluated(row), self.reference(row))
        self.assertLessEqual(self.memo._bytes, self.memo.MAX_BYTES)

    def test_literal_rejection_preserves_separator_boundary_and_unicode_semantics(self):
        terms = ['C++', 'C#', 'machine learning', 'quality assurance', 'AI', 'é', '.', '', 'Python']
        texts = ['C++ / C#', 'machine-learning', 'MACHINE/LEARNING', 'machine.learning',
                 'quality & assurance', 'quality-assurance', 'paid', 'AI', 'é', '.', 'pythonista', 'Python']
        for term in terms:
            for text in texts:
                normalized = matcher.normalize_text(text)
                pattern = matcher.keyword_match_pattern(term)
                expected = bool(pattern and pattern.search(normalized))
                self.assertEqual(matcher.keyword_matches(text, term), expected, (text, term))
                self.assertEqual(matcher.contains_any(text, [term]), expected, (text, term))
                alias = domains.alias_pattern(term)
                self.assertEqual(domains.contains_alias(normalized, term), bool(alias and alias.search(normalized)))

    def test_startup_prepares_role_inputs_without_profile_and_propagates_failure(self):
        from wahojobs import authenticated_profile_matches as browser
        with patch.object(browser.profile_preview, 'prepare_matching_features', wraps=preview.prepare_matching_features) as prepare:
            self.f.integration.prepare_serving_inventory()
        self.assertEqual(prepare.call_count, 1)
        prepared = prepare.call_args.args[0]
        self.assertTrue(prepared)
        before = deepcopy(prepared)
        preview.prepare_matching_features(prepared)
        self.assertEqual(prepared, before)
        with patch.object(browser.profile_preview, 'prepare_matching_features', side_effect=ValueError('synthetic')):
            with self.assertRaisesRegex(ValueError, 'synthetic'):
                self.f.integration.prepare_serving_inventory()
        # Changed inputs select new parser entries; old prepared text grants no fit.
        self.assertEqual(self.evaluated(self.variant(title='Korean Legal Expert')),
                         self.reference(self.variant(title='Korean Legal Expert')))

    def test_catalog_and_matches_source_reads_do_not_overlap_and_failure_releases_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from wahojobs import authenticated_profile_matches as browser
        entered, release, other_started, public_entered = (Event() for _ in range(4))
        def matching_read():
            entered.set()
            self.assertTrue(release.wait(5))
            raise ValueError('synthetic read failure')
        def public_read(*args, **kwargs):
            public_entered.set()
            return []
        def browse():
            other_started.set()
            return self.f.integration._load_public_jobs_inventory()
        with patch.object(type(self.f.integration), '_read_matching_inventory', side_effect=matching_read), \
             patch.object(browser.public_jobs_catalog, 'load_public_jobs', side_effect=public_read), \
             ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.f.integration._load_inventory)
            self.assertTrue(entered.wait(5))
            second = pool.submit(browse)
            self.assertTrue(other_started.wait(5))
            try:
                self.assertFalse(public_entered.wait(.05))
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, 'synthetic read failure'):
                first.result(timeout=5)
            self.assertEqual(second.result(timeout=5), ())
            self.assertTrue(public_entered.is_set())


if __name__ == '__main__':
    unittest.main()
