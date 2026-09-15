"""Exact-binding durable reuse; offline contracts, not classifier evaluation."""
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from html import unescape
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.professional_background_durable_support import durable_fixture, attach_store
from tests.test_accepted_title_uncertainty import BODY, profile
from tests.test_professional_background_components import duration_profile
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_variant_details import variant_detail_url
from wahojobs.professional_background_semantics import digest
from wahojobs.professional_background_store import SQLiteProfessionalBackgroundStore, initialize_preparation_store


class DurablePreparationTests(unittest.TestCase):
    def setUp(self):
        self.f, self.path = durable_fixture()
        self.addCleanup(self.f.close)

    def request(self):
        selection = dict(profile_id=self.f.f.profile['identity']['profile_id'], job_ids=[7003],
                         credentials=dict(authentication_input=None, session_token='synthetic', csrf_secret='synthetic'))
        with patch.object(browser.AuthenticatedProfileMatchesService, 'resolve', side_effect=lambda **kw: self.f.authority()):
            _, requests = self.f.preparer._inspect(self.f.f.integration._service, self.f.f.provider, **selection)
        return next(iter(requests.values()))[0]

    def row(self):
        with closing(sqlite3.connect(self.path)) as connection:
            raw = connection.execute('SELECT record_json FROM preparation_results').fetchone()[0]
        return json.loads(raw)

    def mutate(self, change, *, rehash=True):
        record = self.row()
        change(record)
        raw = json.dumps(record)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE preparation_results SET record_json=?,record_sha256=?',
                               (raw, digest(record) if rehash else '0' * 64))

    def test_positive_restart_validates_and_never_recharges(self):
        self.assertEqual(self.f.execute()['items'][0]['state'], 'published')
        before = self.row()
        attach_store(self.f, self.path)
        self.assertEqual(self.f.prepare()['items'][0]['state'], 'reusable')
        _, match = self.f.current()
        self.assertEqual(match['source_qualification_comparisons'][0]['components']['occupational_relevance']['semantic']['relation'], 'supported_partial')
        self.assertEqual(self.row(), before)
        self.assertNotIn('candidate_facts', before)
        self.assertNotIn('source_text', before)
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_all_conservative_results_reuse_without_automatic_retry(self):
        for relation in ('not_established', 'ambiguous', 'contradicted'):
            with self.subTest(relation=relation):
                self.f.client.session.relation = relation
                result = self.f.execute(replace=True)
                self.assertEqual(result['items'][0]['state'], 'published')
                attach_store(self.f, self.path)
                count = len(self.f.client.session.calls)
                self.assertEqual(self.f.execute()['items'][0]['state'], 'reusable')
                self.assertEqual(self.f.prepare()['items'][0]['relation'], relation)
                _, match = self.f.current()
                self.assertFalse(match['source_qualification_comparisons'][0]['supported_parts'])
                self.assertEqual(len(self.f.client.session.calls), count)

    def test_changed_owner_profile_revision_capture_variant_recipe_and_policy(self):
        self.f.execute()
        request = self.request()
        changes = [lambda r: r['binding']['owner'].__setitem__(0, 'other-owner'),
            lambda r: r['binding'].__setitem__('profile_id', 'other-profile'),
            lambda r: r['binding'].__setitem__('revision_id', 'other-revision'),
            lambda r: r['binding'].__setitem__('profile_digest', 'other-content'),
            lambda r: r['binding']['source'].__setitem__('accepted_capture_id', 9999),
            lambda r: r['binding']['source'].__setitem__('job_id', 7006),
            lambda r: r['binding'].__setitem__('recipe', 'other-recipe'),
            lambda r: r['binding'].__setitem__('model_identity_policy', 'other-policy')]
        for change in changes:
            changed = deepcopy(request); change(changed)
            self.assertIsNone(self.f.evidence.lookup(changed))
            changed['request_id'] = digest({k:v for k,v in changed.items() if k != 'request_id'})
            self.assertIsNone(self.f.evidence.lookup(changed))
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_new_identical_capture_is_not_rebound(self):
        self.f.execute()
        original = self.request()
        self.f.source(BODY)
        current = self.request()
        self.assertNotEqual(current['binding']['source']['accepted_capture_id'], original['binding']['source']['accepted_capture_id'])
        self.assertNotEqual(current['request_id'], original['request_id'])
        self.assertIsNone(self.f.evidence.lookup(current))
        self.f.current()
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_replacement_visible_to_other_provider_and_restart(self):
        self.f.execute()
        old = self.f.evidence
        old_token = old.generation_token
        run, match = self.f.current()
        attach_store(self.f, self.path)
        self.f.client.session.relation = 'not_established'
        self.f.execute(replace=True)
        self.assertEqual(old.lookup(self.request())['relation'], 'not_established')
        with self.assertRaisesRegex(ValueError, 'changed_during_consumption'):
            with old.consume_generation(old_token): self.fail('superseded acceptance')
        self.assertEqual(self.f.f.get(variant_detail_url(match, run_id=run.match_run_id)).status, 200)
        attach_store(self.f, self.path)
        self.assertEqual(self.f.prepare()['items'][0]['relation'], 'not_established')

    def test_revocation_during_generation_prevents_late_positive_publication(self):
        self.f.execute()
        request = self.request()
        other = SQLiteProfessionalBackgroundStore(self.path)
        self.f.client.session.after_request = lambda: other.revoke(request)
        result = self.f.execute(replace=True)
        self.assertEqual(result['items'][0]['state'], 'failed')
        self.assertIn('changed_during_consumption', result['items'][0]['reason'])
        attach_store(self.f, self.path)
        self.assertEqual(self.f.prepare()['items'][0]['reason'], 'professional_evidence_revoked')
        self.assertIsNone(self.f.evidence.lookup(request))

    def test_authorized_revocation_has_no_dispatch_and_blocks_restart_retry(self):
        self.f.execute()
        self.assertEqual(self.f.execute(revoke=True)['items'][0]['state'], 'revoked')
        attach_store(self.f, self.path)
        self.assertEqual(self.f.execute()['items'][0]['reason'], 'professional_evidence_revoked')
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_interrupted_attempt_remains_consumed_disposition_after_restart(self):
        self.f.client.session.mode = 'malformed'
        self.assertEqual(self.f.execute()['items'][0]['state'], 'failed')
        attach_store(self.f, self.path)
        self.assertEqual(self.f.execute()['items'][0]['reason'], 'professional_evidence_attempted')
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_failed_replacement_retains_previous_result(self):
        self.f.execute()
        self.f.client.session.mode = 'malformed'
        self.assertEqual(self.f.execute(replace=True)['items'][0]['state'], 'failed')
        attach_store(self.f, self.path)
        self.assertEqual(self.f.prepare()['items'][0]['relation'], 'supported_partial')

    def test_corrupt_partial_duplicate_reference_and_identity_records_give_no_support(self):
        self.f.execute()
        original = self.row()
        changes = [lambda r: r.pop('provenance'), lambda r: r['output']['candidate_fact_ids'].append(r['output']['candidate_fact_ids'][0]),
            lambda r: r['output'].__setitem__('candidate_fact_ids', ['invented']),
            lambda r: r['output']['source_span'].__setitem__('start', 0),
            lambda r: r['model_identity'].__setitem__('returned_model', 'unapproved-model'),
            lambda r: r['provenance'].__setitem__('response_status', 'incomplete')]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute('UPDATE preparation_results SET record_json=?,record_sha256=?', (json.dumps(original), digest(original)))
                self.mutate(change)
                self.assertEqual(self.f.prepare()['items'][0]['reason'], 'professional_evidence_invalid')
                _, match = self.f.current()
                self.assertNotIn('semantic', match['source_qualification_comparisons'][0]['components']['occupational_relevance'])
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_malformed_json_checksum_and_duplicate_object_keys(self):
        self.f.execute()
        for raw in ('{', '{"binding":{},"binding":{}}', '{}'):
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute('UPDATE preparation_results SET record_json=?,record_sha256=?', (raw, '0'*64))
            self.assertEqual(self.f.evidence.inspect(self.request())[0], 'invalid')

    def test_source_expiry_remains_effective(self):
        self.f.execute()
        request = self.request()
        self.f.f.advance(100)
        attach_store(self.f, self.path)
        self.assertEqual(self.f.evidence.lookup(request)['relation'], 'supported_partial')
        response = self.f.f.get()
        self.assertEqual(response.status, 200)
        context = self.f.f.last_run().recommendation_context
        self.assertFalse(browser._conditional_presentation_matches(context))
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_write_failure_never_reports_publication(self):
        with patch.object(self.f.evidence._store, 'publish', side_effect=ValueError('professional_evidence_storage_unavailable')):
            result = self.f.execute()
        self.assertEqual(result['items'][0]['reason'], 'professional_evidence_storage_unavailable')
        attach_store(self.f, self.path)
        self.assertEqual(self.f.prepare()['items'][0]['reason'], 'professional_evidence_attempted')
        self.assertIsNone(self.f.evidence.lookup(self.request()))

    def test_audit_failure_never_publishes(self):
        def audit(event):
            if event['event'] == 'validated': raise OSError('offline audit failure')
        self.f.preparer._audit_sink = audit
        self.assertEqual(self.f.execute()['items'][0]['state'], 'failed')
        attach_store(self.f, self.path)
        self.assertEqual(self.f.prepare()['items'][0]['reason'], 'professional_evidence_attempted')

    def test_commit_failure_rolls_back_replacement_and_never_reports_success(self):
        self.f.execute()
        before = self.row()
        connect = sqlite3.connect
        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError('offline commit failure')
        def failing_connect(path, *args, **kwargs):
            if str(path).endswith('?mode=rw'):
                kwargs['factory'] = FailingCommit
            return connect(path, *args, **kwargs)
        request = self.request()
        changed = deepcopy(before)
        changed['output']['relation'] = 'not_established'
        with patch('sqlite3.connect', side_effect=failing_connect):
            with self.assertRaisesRegex(ValueError, 'storage_unavailable'):
                self.f.evidence.publish(request, changed['output'], model_identity=changed['model_identity'],
                    provenance=changed['provenance'], expected_generation=self.f.evidence.generation)
        self.assertEqual(self.row(), before)

    def test_generation_counter_failure_rolls_back_written_output(self):
        self.f.execute()
        before = self.row()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE preparation_meta SET generation=9223372036854775807')
        request = self.request()
        changed = deepcopy(before)
        changed['output']['relation'] = 'not_established'
        with self.assertRaisesRegex(ValueError, 'storage_unavailable'):
            self.f.evidence.publish(request, changed['output'], model_identity=changed['model_identity'],
                provenance=changed['provenance'], expected_generation=self.f.evidence.generation)
        self.assertEqual(self.row(), before)

    def test_unauthorized_and_foreign_owner_cannot_revoke_stored_result(self):
        self.f.execute()
        request = self.request()
        plan = self.f.prepare()
        with self.assertRaisesRegex(ValueError, 'revocation_authorization_required'):
            self.f.prepare(execute=True, revoke=True, expected_plan_id=plan['plan_id'])
        self.f.f.owner = 'b'
        with self.assertRaisesRegex(ValueError, 'revocation_authorization_required'):
            self.f.prepare(execute=True, revoke=True, authorized=True, expected_plan_id=plan['plan_id'])
        self.assertEqual(self.f.evidence.lookup(request)['relation'], 'supported_partial')

    def test_missing_store_is_operational_failure_and_not_empty_success(self):
        self.f.execute()
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, 'storage_unavailable'):
            self.f.prepare()
        self.assertEqual(self.f.f.get().status, 503)
        self.assertFalse(self.path.exists())

    def test_storage_busy_does_not_dispatch(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(ValueError, 'storage_unavailable'):
                self.f.execute()
        self.assertFalse(self.f.client.session.calls)

    def test_acceptance_serializes_replacement_without_network_lock(self):
        self.f.execute()
        expected = self.f.evidence.generation_token
        with self.f.evidence.consume_generation(expected):
            with closing(sqlite3.connect(self.path, timeout=0.01)) as connection:
                with self.assertRaises(sqlite3.OperationalError): connection.execute('BEGIN IMMEDIATE')
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('BEGIN IMMEDIATE')

    def test_no_accounting_or_storage_lock_spans_client_execution(self):
        outcomes = []
        def while_client_runs():
            def inspect_accounting():
                outcomes.append(self.f.preparer.accounting['attempts'])
            thread = threading.Thread(target=inspect_accounting)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            with closing(sqlite3.connect(self.path, timeout=0.01)) as connection:
                connection.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError, 'already_in_progress'):
                self.f.execute()
        self.f.client.session.after_request = while_client_runs
        self.assertEqual(self.f.execute()['items'][0]['state'], 'published')
        self.assertEqual(outcomes, [1])

    def test_pid_boundary_and_store_identity_replacement_are_explicit(self):
        store = self.f.evidence._store
        with patch.object(store, '_pid', os.getpid() + 1):
            with self.assertRaisesRegex(ValueError, 'process_mismatch'): store.read(self.request())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE preparation_meta SET store_id=?", ('a'*32,))
        with self.assertRaisesRegex(ValueError, 'identity_changed'): self.f.prepare()

    def test_owner_privacy_purge_requires_matching_existing_admin_scope(self):
        from wahojobs.persistent_profiles import TrustedPrivacyAdminContext
        self.f.execute()
        request = self.request()
        owner = tuple(request['binding']['owner'])
        old = self.f.evidence.generation_token
        with self.assertRaises(ValueError):
            self.f.evidence._store.purge_owner(owner, admin=object())
        admin = TrustedPrivacyAdminContext(operation_scope='purge', environment_namespace=owner[1])
        self.assertEqual(self.f.evidence._store.purge_owner(owner, admin=admin), 1)
        self.assertIsNone(self.f.evidence.lookup(request))
        with self.assertRaisesRegex(ValueError, 'changed_during_consumption'):
            with self.f.evidence.consume_generation(old): self.fail('purged evidence')

    def test_duplicate_validation_does_not_erase_deterministic_duration(self):
        self.f.f.profile = duration_profile(2, role='Campaign adviser')
        self.f.source(BODY.replace('Customer success / support operations', 'marketing'))
        self.f.execute()
        self.mutate(lambda r: r['output']['candidate_fact_ids'].append(r['output']['candidate_fact_ids'][0]))
        _, match = self.f.current()
        duration = match['source_qualification_comparisons'][0]['components']['required_duration']
        self.assertEqual(duration['status'], 'contradicted')
        self.assertEqual(match['affirmative_fit_status'], 'conflicting')


class DurableSetupTests(unittest.TestCase):
    def test_explicit_empty_setup_idempotency_and_no_product_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'derived.sqlite3'
            with self.assertRaisesRegex(ValueError, 'storage_unavailable'): SQLiteProfessionalBackgroundStore(path)
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(path)) as connection:
                self.assertTrue(initialize_preparation_store(connection))
                self.assertFalse(initialize_preparation_store(connection))
            self.assertEqual(SQLiteProfessionalBackgroundStore(path).generation[1], 0)
            with closing(sqlite3.connect(':memory:')) as connection:
                connection.execute('CREATE TABLE product_profiles(value TEXT)')
                with self.assertRaisesRegex(ValueError, 'setup_unavailable'): initialize_preparation_store(connection)
                self.assertEqual(connection.execute('SELECT name FROM sqlite_master').fetchall(), [('product_profiles',)])

    def test_setup_rollback_on_partial_statement_failure(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            def authorize(action, name, *rest):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX else sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            with self.assertRaisesRegex(ValueError, 'setup_unavailable'): initialize_preparation_store(connection)
            connection.set_authorizer(None)
            self.assertEqual(connection.execute('SELECT name FROM sqlite_master').fetchall(), [])
            self.assertEqual(connection.execute('PRAGMA application_id').fetchone()[0], 0)

    def test_schema_drift_is_storage_failure_not_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'derived.sqlite3'
            with closing(sqlite3.connect(path)) as connection:
                initialize_preparation_store(connection)
                connection.execute('DROP INDEX preparation_results_owner')
            with self.assertRaisesRegex(ValueError, 'storage_unavailable'): SQLiteProfessionalBackgroundStore(path)


class DurableQualificationPrecedenceTests(unittest.TestCase):
    """Run the existing scenario assertions after replacing the provider."""
    def setUp(self):
        self.fixture, self.path = durable_fixture()
        self.addCleanup(self.fixture.close)
        self.f = self.fixture.f
        self.body = self.fixture.frozen_source()['body']

    def result(self, *, execute=True, target='/find-matches'):
        from tests.test_professional_background_preparation import ConditionalPoolPreparationTests
        if execute:
            self.fixture.execute()
        attach_store(self.fixture, self.path)
        return ConditionalPoolPreparationTests.result(self, execute=False, target=target)

    def test_duration_shortfall_after_restore(self):
        from tests.test_professional_background_preparation import ConditionalPoolPreparationTests
        ConditionalPoolPreparationTests.test_c02_shortfall_is_an_executed_veto_even_with_optimistic_support(self)

    def test_exact_duration_boundary_after_restore(self):
        from tests.test_professional_background_preparation import ConditionalPoolPreparationTests
        ConditionalPoolPreparationTests.test_c03_exact_duration_does_not_resolve_ambiguous_occupation(self)

    def test_degree_alternative_after_restore(self):
        from tests.test_professional_background_preparation import ConditionalPoolPreparationTests
        ConditionalPoolPreparationTests.test_c04_degree_alternative_survives_without_new_conditional_fit(self)

    def test_proficiency_contradiction_after_restore(self):
        from tests.test_professional_background_preparation import ConditionalPoolPreparationTests
        ConditionalPoolPreparationTests.test_c05_proficiency_conflict_precedes_partial_occupational_support(self)

    def test_same_canonical_variant_after_restore(self):
        from tests.test_professional_background_preparation import ConditionalPoolPreparationTests
        ConditionalPoolPreparationTests.test_same_canonical_variant_cannot_borrow_prepared_support(self)

    def test_generated_action_and_foreign_owner_after_restore(self):
        from tests.test_professional_background_preparation import ConditionalPoolPreparationTests
        ConditionalPoolPreparationTests.test_generated_detail_and_action_references_keep_exact_owner_and_source(self)


class SavedMatchesDependencyTests(unittest.TestCase):
    """Production GETs with the same synthetic authenticated fixture as the probe."""
    request = DurablePreparationTests.request
    row = DurablePreparationTests.row
    mutate = DurablePreparationTests.mutate

    def setUp(self):
        self.f, self.path = durable_fixture()
        self.addCleanup(self.f.close)
        self.assertEqual(self.f.execute()['items'][0]['state'], 'published')
        self.initial = self.f.f.get()
        self.assertEqual(self.initial.status, 200)
        self.run = self.f.f.last_run()
        self.original = deepcopy(self.run.recommendation_context)
        self.match = self.match_in(self.run)
        with patch.object(browser.AuthenticatedProfileMatchesService, 'resolve',
                          side_effect=lambda **kw: self.f.authority()):
            self.target = self.f.f.integration.current_matches_target(self.run.match_run_id,
                (('Host', 'app.test'), ('Cookie', 'wahojobs_session=' + self.f.f.owner * 43)))
        self.assertEqual(self.target, '/find-matches?run=' + self.run.match_run_id)
        self.detail_target = variant_detail_url(self.match, run_id=self.run.match_run_id)
        self.assertIn(self.detail_target, unescape(self.initial.body.decode()))
        self.assertEqual(self.conditional(self.run), [7003])

    @staticmethod
    def match_in(run):
        return next(m for values in run.recommendation_context['matches'].values()
                    for m in values if m['job_id'] == 7003)

    @staticmethod
    def comparison(match):
        return next(r for r in match['source_qualification_comparisons'] if r['kind'] == 'professional_background')

    @staticmethod
    def conditional(run):
        return [m['job_id'] for m in browser._conditional_presentation_matches(run.recommendation_context)]

    def corrupt(self):
        # The review's exact fault: no checksum, generation or publication update.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE preparation_results SET record_json='{' ")

    def no_support_html(self, response):
        self.assertNotIn(b'Your declared role has partial occupational relevance.', response.body)
        self.assertNotIn(b"<p class='decision-placement'>", response.body)

    def unavailable(self):
        count = len(self.f.f.integration._registry._runs)
        with patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_load_inventory') as inventory:
            response = self.f.f.get(self.target)
        inventory.assert_not_called()
        self.assertEqual(response.status, 503)
        self.assertIn(b'Matches cannot be loaded safely right now.', response.body)
        self.no_support_html(response)
        self.assertNotIn(b'Your matches', response.body)
        self.assertEqual(len(self.f.f.integration._registry._runs), count)
        self.assertEqual(self.run.recommendation_context, self.original)
        return response

    def fresh_without_semantic(self, target='/find-matches'):
        response = self.f.f.get(target)
        self.assertEqual(response.status, 200)
        self.no_support_html(response)
        run = self.f.f.last_run()
        match = self.match_in(run)
        self.assertNotEqual(run.match_run_id, self.run.match_run_id)
        self.assertEqual(run.owner_profile_id, self.run.owner_profile_id)
        for key in ('job_id', 'canonical_opportunity_id', 'url'):
            self.assertEqual(match[key], self.match[key])
        comparison = self.comparison(match)
        self.assertNotIn('semantic', comparison['components']['occupational_relevance'])
        self.assertEqual(comparison['components']['required_duration'],
                         self.comparison(self.match)['components']['required_duration'])
        self.assertNotEqual(comparison['status'], 'contradicted')
        self.assertFalse(match['primary_recommendation_eligible'])
        self.assertEqual(self.conditional(run), [])
        return response, run

    def test_valid_unchanged_context_reuses_comparison_admission_and_run(self):
        with patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_load_inventory') as inventory:
            response = self.f.f.get(self.target)
        inventory.assert_not_called()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.f.f.last_run().match_run_id, self.run.match_run_id)
        self.assertEqual(self.run.recommendation_context, self.original)
        self.assertEqual(self.conditional(self.run), [7003])
        self.assertFalse(self.match['primary_recommendation_eligible'])
        self.assertIn(b"<p class='decision-placement'>", response.body)
        self.assertIn(b'Your declared role has partial occupational relevance.', response.body)
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_first_saved_request_detects_original_corruption_without_prior_lookup(self):
        token = self.f.evidence.generation_token
        self.corrupt()
        # First handler operation after corruption; no inspection or fresh GET.
        response = self.unavailable()
        self.assertEqual(self.f.evidence.generation_token, token)
        self.unavailable()  # failure did not mint reusable authority or repair data
        self.assertEqual(self.f.prepare()['items'][0]['reason'], 'professional_evidence_invalid')
        self.assertEqual(len(self.f.client.session.calls), 1)
        receipt_root = os.environ.get('WAHOJOBS_DURABLE_TEST_RECEIPTS')
        if receipt_root:
            Path(receipt_root, 'saved-context-first-request.json').write_text(json.dumps(dict(
                request=self.target, before_status=self.initial.status, after_status=response.status,
                unchanged_generation=token == self.f.evidence.generation_token,
                historical_comparison=self.comparison(self.match), historical_conditional_ids=self.conditional(self.run),
                accepted_after_corruption=False, new_context_registered=False,
                rendered_support=False, disposition='professional_evidence_invalid',
                client_dispatches=1, additional_dispatches=0), indent=2))

    def test_all_conservative_relations_reuse_valid_unchanged_contexts(self):
        for relation in ('not_established', 'ambiguous', 'contradicted'):
            with self.subTest(relation=relation):
                self.f.client.session.relation = relation
                self.assertEqual(self.f.execute(replace=True)['items'][0]['state'], 'published')
                run, match = self.f.current()
                before = deepcopy(run.recommendation_context)
                with patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_load_inventory') as inventory:
                    response = self.f.f.get('/find-matches?run=' + run.match_run_id)
                inventory.assert_not_called()
                self.assertEqual(response.status, 200)
                self.no_support_html(response)
                self.assertEqual(self.f.f.last_run().match_run_id, run.match_run_id)
                self.assertEqual(run.recommendation_context, before)
                self.assertEqual(self.comparison(match)['components']['occupational_relevance']['semantic']['relation'], relation)
                self.assertEqual(self.conditional(run), [])
        self.assertEqual(len(self.f.client.session.calls), 4)  # only explicit preparations

    def test_full_validator_rejects_rehashed_duplicate_reference(self):
        self.mutate(lambda r: r['output']['candidate_fact_ids'].append(r['output']['candidate_fact_ids'][0]))
        self.unavailable()
        self.assertEqual(self.f.prepare()['items'][0]['reason'], 'professional_evidence_invalid')
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_valid_but_changed_output_without_generation_cannot_validate_old_payload(self):
        token = self.f.evidence.generation_token
        self.mutate(lambda r: r['output'].__setitem__('relation', 'not_established'))
        self.unavailable()
        self.assertEqual(self.f.evidence.generation_token, token)
        self.assertEqual(self.f.prepare()['items'][0]['relation'], 'not_established')
        run, match = self.f.current()
        self.assertEqual(self.conditional(run), [])
        self.assertEqual(self.comparison(match)['components']['occupational_relevance']['semantic']['relation'], 'not_established')
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_missing_dependency_is_not_storage_failure_or_candidate_contradiction(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('DELETE FROM preparation_results')
        self.unavailable()
        self.assertEqual(self.f.evidence.inspect(self.request()), ('absent', None))
        self.fresh_without_semantic()
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_invalid_latest_conservative_result_cannot_resurrect_old_positive(self):
        self.f.client.session.relation = 'not_established'
        self.assertEqual(self.f.execute(replace=True)['items'][0]['state'], 'published')
        conservative, _ = self.f.current()
        self.corrupt()
        # The old positive run has an obsolete generation: normal recomputation.
        self.fresh_without_semantic(self.target)
        count = len(self.f.f.integration._registry._runs)
        response = self.f.f.get('/find-matches?run=' + conservative.match_run_id)
        self.assertEqual(response.status, 503)
        self.no_support_html(response)
        self.assertEqual(len(self.f.f.integration._registry._runs), count)
        self.assertEqual(len(self.f.client.session.calls), 2)

    def test_replacement_and_revocation_keep_normal_generation_invalidation(self):
        self.f.client.session.relation = 'not_established'
        self.assertEqual(self.f.execute(replace=True)['items'][0]['state'], 'published')
        response = self.f.f.get(self.target)
        self.assertEqual(response.status, 200)
        self.no_support_html(response)
        run = self.f.f.last_run()
        self.assertNotEqual(run.match_run_id, self.run.match_run_id)
        self.assertEqual(run.owner_profile_id, self.run.owner_profile_id)
        self.assertEqual(self.conditional(run), [])
        self.assertEqual(self.comparison(self.match_in(run))['components']['occupational_relevance']['semantic']['relation'], 'not_established')
        self.assertEqual(self.f.execute(revoke=True)['items'][0]['state'], 'revoked')
        self.fresh_without_semantic('/find-matches?run=' + run.match_run_id)
        self.assertEqual(self.f.prepare()['items'][0]['reason'], 'professional_evidence_revoked')
        self.assertEqual(len(self.f.client.session.calls), 2)

    def test_old_detail_and_action_run_links_cannot_regain_invalid_support(self):
        before_detail = self.f.f.get(self.detail_target)
        self.assertEqual(before_detail.status, 200)
        action_run = self.f.f.last_run()
        action_match = self.match_in(action_run)
        action_detail = variant_detail_url(action_match, run_id=action_run.match_run_id)
        self.assertEqual(action_run.owner_profile_id, self.run.owner_profile_id)
        self.corrupt()
        self.unavailable()  # saved Matches itself detects damage first
        for target in (self.detail_target, action_detail):
            response = self.f.f.get(target)
            self.assertEqual(response.status, 200)
            self.no_support_html(response)
            current = self.f.f.last_run()
            self.assertEqual(current.owner_profile_id, self.run.owner_profile_id)
            for key in ('job_id', 'canonical_opportunity_id', 'url'):
                self.assertEqual(self.match_in(current)[key], self.match[key])
        # Detail-created action contexts have no selection proof and recompute.
        response, fresh = self.fresh_without_semantic('/find-matches?run=' + action_run.match_run_id)
        self.assertNotIn(variant_detail_url(self.match_in(fresh), run_id=fresh.match_run_id), unescape(response.body.decode()))
        self.assertIn('/account/profile?run=' + fresh.match_run_id, unescape(response.body.decode()))
        self.fresh_without_semantic()
        self.f.f.owner = 'b'
        self.f.f.profile = profile()
        self.f.f.profile['identity']['profile_id'] = 'prf_' + '0' * 31 + '2'
        foreign = self.f.f.get(self.detail_target)
        self.assertEqual(foreign.status, 404)
        self.no_support_html(foreign)
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_corruption_after_collection_before_guard_prevents_acceptance(self):
        original = self.f.f.integration._with_card_evidence
        def damage_after_presentation(*args, **kwargs):
            context = original(*args, **kwargs)
            self.corrupt()
            return context
        with patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_with_card_evidence', side_effect=damage_after_presentation):
            self.unavailable()
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_validated_snapshot_and_registration_hold_same_publication_guard(self):
        render = browser._render_match_results
        observed = []
        def guarded_render(*args, **kwargs):
            with closing(sqlite3.connect(self.path, timeout=0)) as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("UPDATE preparation_results SET record_json='{' ")
            observed.append(True)
            return render(*args, **kwargs)
        with patch.object(browser, '_render_match_results', side_effect=guarded_render):
            response = self.f.f.get(self.target)
        self.assertEqual(observed, [True])
        self.assertEqual(response.status, 200)
        self.assertIn(b"<p class='decision-placement'>", response.body)
        # Damage after acceptance belongs to the next request, not that response.
        self.corrupt()
        self.unavailable()
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_only_context_dependencies_are_read_and_unrelated_damage_is_ignored(self):
        request = self.request()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT INTO preparation_results VALUES (?,?,?,?,?)',
                ('0' * 64, json.dumps(['other', 'synthetic', 'other']), 'validated', '{', '0' * 64))
        store = self.f.evidence._store
        read = store.read_in_transaction
        requested = []
        def observed(connection, request):
            requested.append(request['request_id'])
            return read(connection, request)
        with patch.object(store, 'read_in_transaction', side_effect=observed):
            response = self.f.f.get(self.target)
        self.assertEqual(response.status, 200)
        self.assertTrue(requested)
        self.assertEqual(set(requested), {request['request_id']})
        self.assertEqual(self.f.f.last_run().match_run_id, self.run.match_run_id)
        self.assertEqual(self.conditional(self.run), [7003])
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_acceptance_storage_failure_stays_distinct_from_invalid_dependency(self):
        request = self.request()
        expected = self.f.evidence.lookup(request)
        token = self.f.evidence.generation_token
        store = self.f.evidence._store
        with patch.object(store, 'read_in_transaction', side_effect=sqlite3.OperationalError('offline read failure')):
            self.unavailable()
            with self.assertRaisesRegex(ValueError, 'professional_evidence_storage_unavailable'):
                with self.f.evidence.consume_generation(token, dependencies=((request, expected),)):
                    self.fail('storage failure accepted')
        self.corrupt()
        with self.assertRaisesRegex(ValueError, 'professional_evidence_dependency_invalid'):
            with self.f.evidence.consume_generation(token, dependencies=((request, expected),)):
                self.fail('invalid dependency accepted')
        self.assertEqual(len(self.f.client.session.calls), 1)

    def test_saved_reuse_retains_owner_revision_capture_recipe_and_expiry_checks(self):
        self.f.f.owner = 'b'
        old_profile = self.f.f.profile
        self.f.f.profile = profile()
        self.f.f.profile['identity']['profile_id'] = 'prf_' + '0' * 31 + '2'
        self.assertEqual(self.f.f.get(self.target).status, 410)
        self.f.f.profile = old_profile
        self.f.f.owner = 'a'
        self.f.revision = 'synthetic-confirmed-revision-2'
        self.fresh_without_semantic(self.target)
        self.f.revision = 'synthetic-confirmed-revision-1'
        recipe = self.f.evidence.recipe
        self.f.evidence.recipe += '-incompatible'
        self.fresh_without_semantic(self.target)
        self.f.evidence.recipe = recipe
        self.f.source(BODY)  # unchanged body is still a new accepted capture
        self.fresh_without_semantic(self.target)
        self.f.f.advance(100)
        response = self.f.f.get(self.target)
        self.assertEqual(response.status, 200)
        self.no_support_html(response)
        self.assertEqual(self.conditional(self.f.f.last_run()), [])
        self.assertEqual(len(self.f.client.session.calls), 1)


class FreshAuthenticatedProcessTests(unittest.TestCase):
    def test_conservative_replacement_is_authoritative_in_next_process(self):
        with tempfile.TemporaryDirectory() as directory:
            for phase, relation in (('A', 'supported_partial'), ('replace', 'not_established'), ('B', 'not_established')):
                result = subprocess.run([sys.executable, '-B', '-m', 'tests.professional_background_durable_support',
                                         directory, phase, relation], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            receipts = [json.loads(Path(directory, p + '.json').read_text()) for p in ('A', 'replace', 'B')]
            self.assertEqual(len({r['pid'] for r in receipts}), 3)
            self.assertEqual(receipts[-1]['client_dispatches'], 0)
            self.assertFalse(receipts[-1]['conditional_ids'])
            self.assertEqual(receipts[-1]['relation'], 'not_established')
            receipt_root = os.environ.get('WAHOJOBS_DURABLE_TEST_RECEIPTS')
            if receipt_root:
                Path(receipt_root, 'replacement.json').write_text(json.dumps(receipts, indent=2))

    def test_fresh_authenticated_positive_and_conservative_processes(self):
        for relation in ('supported_partial', 'not_established', 'ambiguous', 'contradicted'):
            with self.subTest(relation=relation), tempfile.TemporaryDirectory() as directory:
                receipts = []
                for phase in ('A', 'B'):
                    command = [sys.executable, '-B', '-m', 'tests.professional_background_durable_support', directory, phase, relation]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    receipts.append(json.loads((Path(directory) / (phase + '.json')).read_text()))
                a, b = receipts
                self.assertNotEqual(a['pid'], b['pid'])
                self.assertNotEqual(a['run_id'], b['run_id'])
                self.assertNotEqual(a['session_digest'], b['session_digest'])
                for key in ('owner', 'profile', 'revision', 'profile_digest', 'request_id', 'relation', 'generation', 'comparisons'):
                    self.assertEqual(a[key], b[key], key)
                self.assertEqual((a['client_dispatches'], b['client_dispatches']), (1, 0))
                self.assertEqual(b['historical_accounting']['attempts'], 0)
                self.assertEqual(b['historical_accounting']['reserved_tokens'], 0)
                self.assertEqual(b['historical_accounting']['reserved_usd'], '0')
                self.assertFalse(b['primary_eligible'])
                # Content-free reproducible evidence survives test cleanup.
                receipt_root = os.environ.get('WAHOJOBS_DURABLE_TEST_RECEIPTS')
                if receipt_root:
                    Path(receipt_root, relation + '.json').write_text(json.dumps(receipts, indent=2))


if __name__ == '__main__':
    unittest.main()
