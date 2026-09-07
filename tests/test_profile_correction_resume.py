"""Saved proposals survive token expiry; only fresh authority can apply them."""
import json
import sqlite3
import unittest

from tests import test_persistent_profile_corrections as support
from tests import test_profile_review_transfer as transfer_support
from tests.browser_session_authentication_test_support import seed_browser_session
from tests.persistent_profiles_repository_test_support import create_command
from wahojobs.persistent_profiles_repository import create_persistent_profile
from wahojobs.persistent_profile_corrections import PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS as TTL
from wahojobs.profiles.correction_editor import changed_profile_sections
from scripts.local_product_app import MatchRunRegistry


class ProfileCorrectionResumeTests(unittest.TestCase):
    def setUp(self):
        self.t = transfer_support.ProfileReviewTransferTests()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.f = self.t.f
        self.browser = self.t.browser

    def proposal(self):
        page, form = self.t.review(dict(country='Brazil', city='Example City', region='SP',
            specialties='Model output evaluation, Image review, Annotation',
            software_tools='R', job_titles=''))
        ref = dict(form['fields'])['draft']
        prepared = self.browser._correction_registry.peek(ref).recommendation_context['correction_preparation']
        return page, form, prepared.profile_for_browser()

    def expire(self):
        self.f.registry_time += TTL + 1

    def landing(self):
        return self.t.get('/account/profile?correction=resume')

    def resume(self, landing):
        form = self.f._form(landing, 'retained_draft')
        response, _ = self.f._post_form(self.browser, form['action'], form['fields'])
        self.assertEqual(response.status, 303)
        return self.t.get(self.f._response_header(response, 'Location'))

    def test_expired_token_rejected_same_proposal_resumed_and_only_one_apply(self):
        _page, old, expected = self.proposal()
        before = support._logical_snapshot(self.f.path)
        self.expire()
        expired = self.t.apply(old)
        self.assertEqual(expired.status, 410)
        self.assertIn(b'Your review session expired, but your changes were saved', expired.body)
        fresh = self.resume(expired)
        current = self.f._form(fresh, 'draft', 'review_token')
        self.assertNotEqual(dict(old['fields'])['review_token'], dict(current['fields'])['review_token'])
        self.assertEqual(support._logical_snapshot(self.f.path), before)
        self.assertEqual(self.t.apply(old).status, 410)
        counts = self.f._profile_counts()
        self.assertEqual(self.t.apply(current).status, 200)
        self.assertFalse(changed_profile_sections(expected, self.t.current()))
        self.assertEqual(self.f._profile_counts()[1], counts[1] + 1)
        self.assertIn(self.t.apply(current).status, (200, 409, 410))
        self.assertEqual(self.f._profile_counts()[1], counts[1] + 1)

    def test_repeated_expiry_preserves_omissions_and_no_entitlement_or_revision_writes(self):
        _page, form, expected = self.proposal()
        before = support._logical_snapshot(self.f.path)
        for _ in range(3):
            self.expire()
            fresh = self.resume(self.t.apply(form))
            form = self.f._form(fresh, 'draft', 'review_token')
            retained = self.f.service.retained_review(self.f._grant(), dict(form['fields'])['draft'])
            self.assertFalse(changed_profile_sections(expected, retained['proposed']))
            self.assertEqual(retained['proposed']['experience'].get('job_titles', []), [])
            self.assertEqual(support._logical_snapshot(self.f.path), before)

    def test_restart_reseals_persisted_proposal_without_any_old_token(self):
        _, old, expected = self.proposal()
        # Discard all process-local runs and use a fresh service. The durable
        # checkpoint is read, revalidated and bound to newly minted authority.
        self.f.service = self.f._build_service()
        self.browser = self.f._build_browser(correction_registry=MatchRunRegistry(
            absolute_ttl_seconds=TTL, _retention_clock=lambda: self.f.registry_time))
        self.t.browser = self.browser
        self.assertEqual(self.t.apply(old).status, 410)
        fresh = self.resume(self.landing())
        form = self.f._form(fresh, 'draft', 'review_token')
        self.assertEqual(self.t.apply(form).status, 200)
        self.assertFalse(changed_profile_sections(expected, self.t.current()))

    def test_new_revision_conflicts_and_keeps_owner_inspection(self):
        _, form, _ = self.proposal()
        landing = self.landing()
        grant = self.f._grant()
        offer, *_ = self.f._issue(grant, city='New Saved City')
        self.assertEqual(self.f._consume(grant, offer).state, 'corrected')
        self.expire()
        before = support._logical_snapshot(self.f.path)
        conflict = self.landing()
        self.assertEqual(conflict.status, 409)
        self.assertIn(b'Example City', conflict.body)
        self.assertNotIn(b'Apply profile update', conflict.body)
        f = self.f._form(landing, 'retained_draft')
        result, _ = self.f._post_form(self.browser, f['action'], f['fields'])
        self.assertEqual(result.status, 409)
        self.assertEqual(self.t.apply(form).status, 409)
        self.assertEqual(support._logical_snapshot(self.f.path), before)

    def test_cross_owner_cannot_read_or_resume_even_with_reference(self):
        _, form, _ = self.proposal()
        with self.f._connection() as connection:
            outsider = seed_browser_session(connection, suffix='85')
            create_persistent_profile(connection, create_command(outsider['principal'], idempotency_key='other-owner-resume-test'))
        grant = self.f._grant(session=outsider)
        self.assertIsNone(self.f.service.retained_review(grant, dict(form['fields'])['draft']))
        resume = self.f._form(self.landing(), 'retained_draft')
        from wahojobs.persistent_profile_corrections import profile_correction_action_csrf_proof
        target = '/account/profile?action=resume&proof=' + profile_correction_action_csrf_proof(outsider['csrf_secret'], 'resume')
        response, _ = self.f._post_form(self.browser, target, resume['fields'], session=outsider)
        self.assertEqual(response.status, 410)
        self.assertNotIn(b'Example City', response.body)

    def test_fresh_authenticated_session_same_owner_can_resume(self):
        _, old, expected = self.proposal()
        new_session = self.f._new_session('resume-new-login')
        grant = self.f._grant(session=new_session)
        retained = self.f.service.retained_review(grant, dict(old['fields'])['draft'])
        self.assertEqual(retained['state'], 'ready')
        self.assertFalse(changed_profile_sections(expected, retained['proposed']))

    def test_corrupted_payload_rejected_without_any_profile_write(self):
        self.proposal()
        path = self.f.path.with_name(self.f.path.name + '.correction-drafts.sqlite3')
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE product_profile_correction_drafts SET payload_json='{}'")
        before = support._logical_snapshot(self.f.path)
        self.assertEqual(self.landing().status, 503)
        self.assertEqual(support._logical_snapshot(self.f.path), before)

    def test_canonical_validator_rechecks_even_with_valid_storage_checksum(self):
        self.proposal()
        path = self.f.path.with_name(self.f.path.name + '.correction-drafts.sqlite3')
        from wahojobs.profile_correction_drafts import encode
        import hashlib
        with sqlite3.connect(path) as connection:
            payload = json.loads(connection.execute('SELECT payload_json FROM product_profile_correction_drafts').fetchone()[0])
            payload['proposed']['skills']['software_tools'] = ['x' * 129]
            value = encode(payload)
            connection.execute('UPDATE product_profile_correction_drafts SET payload_json=?,payload_sha256=?',
                (value, hashlib.sha256(value.encode('ascii')).hexdigest()))
        before = support._logical_snapshot(self.f.path)
        self.assertEqual(self.landing().status, 503)
        self.assertEqual(support._logical_snapshot(self.f.path), before)
