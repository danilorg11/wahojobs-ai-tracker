"""Exact-source preference admission precedes canonical representative choice."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_profile_preference_model import with_preference_model
from wahojobs import authenticated_profile_matches as browser
from wahojobs.profiles.preference_model import empty_profile_preferences_v1


class VariantPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        model = empty_profile_preferences_v1()
        model['engagement_terms'] = ['temporary']
        self.f.profile = with_preference_model(self.f.profile, model)
        self.f.update_inventory("UPDATE jobs SET commitment='Internship' WHERE id=7003")
        self.f.update_inventory("UPDATE jobs SET canonical_opportunity_id=7002, commitment='Temporary' WHERE id=7006")

    def visible(self, target='/find-matches'):
        response = self.f.get(target)
        self.assertEqual(response.status, 200)
        context = self.f.last_run().recommendation_context
        return response, browser._recommendation_presentation_matches(context)

    def test_compatible_alternate_preserves_its_own_card_and_scoped_destination(self):
        response, matches = self.visible()
        self.assertEqual([m['job_id'] for m in matches], [7006])
        self.assertEqual(matches[0]['selected_variant_id'], 7006)
        self.assertEqual(matches[0]['variant_count'], 2)
        self.assertIn(b'id=\'opportunity-7006\'', response.body)
        from wahojobs import authenticated_variant_details as details
        observed = []
        original = details.prepare_variant_notice
        def record(job, *args, **kwargs):
            original(job, *args, **kwargs)
            observed.append(job)
        with patch.object(details, 'prepare_variant_notice', side_effect=record):
            detail = self.f.get('/job/opportunity-7002')
        self.assertEqual(detail.status, 200)
        self.assertEqual(observed[-1]['job_id'], 7006)
        self.assertTrue(observed[-1]['_authenticated_local_checks']['passes'])
        self.assertIn(b'https://jobs.example.test/synthetic-part-time', detail.body)

    def test_compatible_preference_cannot_resurrect_geography_or_language_conflict(self):
        for update in ("location='Remote - United States'", "title='Korean Language Expert'"):
            with self.subTest(update=update):
                self.f.update_inventory("UPDATE jobs SET location='Remote - Brazil', title='Python Backend AI Coding Evaluator' WHERE id=7006")
                self.f.update_inventory('UPDATE jobs SET '+update+' WHERE id=7006')
                self.assertEqual(self.visible()[1], [])

    def test_no_surviving_variant_remains_excluded_and_keeps_preference_diagnostic(self):
        self.f.update_inventory("UPDATE jobs SET commitment='Internship' WHERE id=7006")
        self.assertEqual(self.visible()[1], [])
        typed = self.f.last_run().recommendation_context['_typed_preference_enforcement']
        self.assertTrue(typed['evaluations'])
        self.assertEqual(typed['evaluations'][0]['admission']['status'], 'exclude')

    def test_existing_rank_tie_break_is_preserved_when_both_variants_pass(self):
        self.f.update_inventory("UPDATE jobs SET commitment='Temporary' WHERE id=7003")
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [7003])

    def test_profile_preference_change_and_separate_owner_do_not_reuse_old_selection(self):
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [7006])
        original = deepcopy(self.f.profile)
        model = empty_profile_preferences_v1()
        model['engagement_terms'] = ['internship']
        self.f.profile = with_preference_model(self.f.profile, model)
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [7003])
        self.f.owner = 'b'
        self.f.profile = original
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [7006])

    def test_inventory_invalidation_and_workload_guidance_preserve_existing_contract(self):
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [7006])
        self.f.update_inventory('UPDATE jobs SET is_active=0 WHERE id=7006')
        self.assertEqual(self.visible()[1], [])
        self.f.update_inventory("UPDATE jobs SET is_active=1, commitment='Part-time' WHERE id=7006")
        model = empty_profile_preferences_v1()
        model['workloads'] = ['full_time']
        self.f.profile = with_preference_model(self.f.profile, model)
        # A workload wish remains guidance, not a new exclusion.
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [7003])

    def capacity_inventory(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.row_factory = sqlite3.Row
            canonical = dict(c.execute('SELECT * FROM canonical_opportunities WHERE id=7002').fetchone())
            job = dict(c.execute('SELECT * FROM jobs WHERE id=7003').fetchone())
            c.execute('UPDATE jobs SET is_active=0')
            for i in range(161):
                canonical.update(id=9000+i, canonical_key='capacity-'+str(i))
                c.execute('INSERT INTO canonical_opportunities ('+','.join(canonical)+') VALUES ('+
                          ','.join('?' for _ in canonical)+')', list(canonical.values()))
                job.update(id=9000+i, canonical_opportunity_id=9000+i,
                           external_id='capacity-'+str(i), url='https://jobs.example.test/capacity-'+str(i),
                           source_hash='capacity-'+str(i), is_active=1,
                           commitment='Temporary' if i==160 else 'Internship')
                c.execute('INSERT INTO jobs ('+','.join(job)+') VALUES ('+
                          ','.join('?' for _ in job)+')', list(job.values()))

    def test_preference_failures_do_not_fill_section_capacity_before_survivors(self):
        self.capacity_inventory()
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [9160])
        context = self.f.last_run().recommendation_context
        self.assertTrue(context['_typed_preference_enforcement']['evaluations'])
        self.assertTrue(all(len(group) <= browser.local_product.PREVIEW_MATCH_LIMIT
                            for group in context['matches'].values()))
        self.f.update_inventory("UPDATE jobs SET commitment='Internship' WHERE id=9160")
        self.assertEqual(self.visible()[1], [])

    def test_unverified_unmarked_rows_do_not_fill_capacity_before_trusted_survivor(self):
        from datetime import timedelta
        self.capacity_inventory()
        self.f.update_inventory("UPDATE jobs SET commitment='Temporary'")
        self.f.update_inventory('UPDATE jobs SET last_seen_at=? WHERE id>=9000 AND id<9160',
                                ((self.f.now-timedelta(days=7)).isoformat(),))
        self.assertEqual([m['job_id'] for m in self.visible()[1]], [9160])
        self.f.update_inventory('UPDATE jobs SET last_seen_at=? WHERE id=9160',
                                ((self.f.now-timedelta(days=7)).isoformat(),))
        self.assertEqual(self.visible()[1], [])


class SourceVariantSelectionTests(unittest.TestCase):
    from tests.test_accepted_task_matching import AcceptedTaskMatchingTests as _support
    role = _support.role
    source = _support.source
    visible = VariantPreferenceTests.visible

    def setUp(self):
        from tests.test_source_task_fit import SourceTaskAuthenticatedTests
        SourceTaskAuthenticatedTests.setUp(self)
        self.f.update_inventory('UPDATE jobs SET canonical_opportunity_id=7002 WHERE id=7006')
        self.source('You will perform linguistic analysis.', job_id=7003)
        self.source('You will review Portuguese sentences.', job_id=7006)

    def test_source_rejected_representative_does_not_hide_supported_alternate(self):
        _, matches = self.visible()
        self.assertEqual([m['job_id'] for m in matches], [7006])
        self.assertEqual(matches[0]['selected_variant_id'], 7006)
        self.assertEqual(matches[0]['variant_count'], 2)
        self.assertNotIn('linguistic_analysis', str(matches[0].get('source_task_fit')))

    def test_no_variant_borrows_missing_specialist_background(self):
        self.source('You will perform linguistic analysis.', job_id=7006)
        self.assertEqual(self.visible()[1], [])


class ConditionalVariantPreferenceTests(unittest.TestCase):
    # Reuse the source-writing helper only on this disposable synthetic DB.
    from tests.test_accepted_task_matching import AcceptedTaskMatchingTests as _support
    role = _support.role
    source = _support.source
    visible = VariantPreferenceTests.visible

    def setUp(self):
        VariantPreferenceTests.setUp(self)
        from tests.test_source_language_proficiency import profile
        self.f.profile = profile('advanced')
        self.role('German AI Data Reviewer', 'Remote')
        model = empty_profile_preferences_v1()
        model['engagement_terms'] = ['temporary']
        self.f.profile = with_preference_model(self.f.profile, model)
        self.f.update_inventory("UPDATE jobs SET commitment='Internship' WHERE id=7003")
        self.f.update_inventory("UPDATE jobs SET commitment='Temporary' WHERE id=7006")
        for job_id in (7003, 7006):
            self.source('Scope of Work\n\nEvaluate AI outputs.\n\nWho You Are\n\nNative German', job_id=job_id)

    def test_compatible_conditional_alternate_is_selected_without_claiming_native_fluency(self):
        _, matches = self.visible()
        self.assertEqual([m['job_id'] for m in matches], [7006])
        self.assertTrue(matches[0]['conditional_task_fit'])
        self.assertEqual(matches[0]['affirmative_fit_status'], 'uncertain')
        self.assertIn('advanced', str(matches[0]['source_language_checks']))

    def test_failing_primary_does_not_hide_passing_conditional_variant(self):
        self.source('Scope of Work\n\nEvaluate AI outputs.', job_id=7003)
        _, matches = self.visible()
        self.assertEqual([m['job_id'] for m in matches], [7006])
        self.assertTrue(matches[0]['conditional_task_fit'])

    def test_recent_cache_uses_compatible_variant_and_still_expires(self):
        self.f.advance(73)
        _, matches = self.visible()
        self.assertEqual([m['job_id'] for m in matches], [7006])
        self.assertEqual(matches[0]['presentation_data_status'], 'recently_cached')
        self.f.advance(24 * 8)
        self.assertEqual(self.visible()[1], [])


if __name__ == '__main__':
    unittest.main()
