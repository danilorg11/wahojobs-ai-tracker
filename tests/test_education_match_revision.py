"""Saved education reaches current matching without rewriting old run snapshots.

The correction form, durable save, authentication, current-profile selection,
matcher handler, projection and evaluation are real. Inventory uses a separate
disposable SyntheticMatcherFixture database; workflow records are stubbed empty.
This is a server-handler regression, not a browser/HTTPS or ranking-policy test.
"""
from copy import deepcopy
import json
import unittest
from unittest import mock

from tests import test_persistent_profile_corrections as correction_support
from tests import test_profile_review_transfer as transfer
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs import authenticated_profile_matches as browser
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)


class EducationMatchRevisionTests(unittest.TestCase):
    def setUp(self):
        self.correction = transfer.ProfileReviewTransferTests()
        self.correction.setUp()
        self.addCleanup(self.correction.doCleanups)
        self.f = self.correction.f
        self.matcher = SyntheticMatcherFixture()
        self.addCleanup(self.matcher.close)
        # Resolve the real saved profile on every request, rather than assigning
        # a copied profile to the fixture's usual synthetic authority substitute.
        self.matcher.integration._service = browser.AuthenticatedProfileMatchesService(
            authentication_gateway=DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace=self.f.session['environment'],
                clock=lambda: self.f.now,
            ),
            authorization_gateway=DurablePersistentProfileReadAuthorizationGateway(),
            connection_provider=correction_support._ReadProvider(self.f.path),
            clock=lambda: self.f.now,
            binding_secret=b'education-current-revision-test-binding' * 2,
        )

    def get(self, target='/find-matches'):
        # Tracking belongs to the correction account's database, whereas this
        # test intentionally supplies an independent inventory-only database.
        with mock.patch.object(
            browser.AuthenticatedProfileMatchesBrowserIntegration,
            '_load_pipeline_records', return_value=[],
        ):
            return self.matcher.integration.handle(
                'GET', target, self.f._browser_headers(self.f.session),
            )

    def test_confirmed_education_invalidates_old_run_and_preserves_its_snapshot(self):
        before = self.f._current().trusted_dict(include_structured_profile=True)
        service_resolve = self.matcher.integration._service.resolve
        resolved_revisions = []

        def resolve(**kwargs):
            result = service_resolve(**kwargs)
            self.assertEqual(result.state, 'profile')
            resolved_revisions.append(result.authorized_state()._revision_id)
            return result

        with (
            mock.patch.object(browser.AuthenticatedProfileMatchesService, 'resolve',
                              side_effect=resolve),
            mock.patch.object(browser, 'project_v2_to_matcher_v1',
                              wraps=browser.project_v2_to_matcher_v1) as project,
            mock.patch.object(browser.profile_preview,
                              'build_preview_context_from_canonical_rows',
                              wraps=browser.profile_preview.build_preview_context_from_canonical_rows) as evaluate,
        ):
            self.assertEqual(self.get().status, 200)
            old_run = self.matcher.last_run()
            old_snapshot = deepcopy(old_run.recommendation_context)
            self.assertIsNotNone(old_snapshot.get('_authenticated_reuse'))
            self.assertEqual(resolved_revisions[-1], before['revision_id'])
            project.reset_mock()
            evaluate.reset_mock()

            # Establish that this explicit URL is reusable before the save.
            target = '/find-matches?run=' + old_run.match_run_id
            self.assertEqual(self.get(target).status, 200)
            project.assert_not_called()
            evaluate.assert_not_called()

            entry = dict(kind='phd', qualification='Biology', field='Biology',
                         institution='U Penn', status='completed', completion_year=None)
            counts = self.f._profile_counts()
            _, confirmation = self.correction.review(
                # The legacy correction fixture explicitly reports no degree.
                # Correct that assertion as well as adding the new entry.
                {'education_entries': json.dumps([entry]),
                 'education_level': 'not_specified', 'no_degree': None,
                 'hard_constraints': ''},
            )
            # An unconfirmed review must not change current matcher authority.
            self.assertEqual(self.f._current().revision_id, before['revision_id'])
            self.assertEqual(self.correction.apply(confirmation).status, 200)
            saved = self.f._current().trusted_dict(include_structured_profile=True)
            self.assertNotEqual(saved['revision_id'], before['revision_id'])
            self.assertEqual(saved['revision_number'], before['revision_number'] + 1)
            self.assertEqual(self.f._profile_counts()[1], counts[1] + 1)
            education = saved['structured_profile']['education']
            self.assertEqual(education['entries'], [entry])
            self.assertEqual(education['education_level'], 'phd')
            self.assertIn('Biology', education['fields_or_domains'])
            self.assertIn('U Penn', education['institutions'])
            inventory_proof = old_snapshot['_authenticated_reuse']['inventory']
            self.assertEqual(self.matcher.integration._inventory_commit_token(), inventory_proof)
            project.reset_mock()
            evaluate.reset_mock()

            # Revisiting the old URL must evaluate the current persisted revision,
            # even though the inventory and the requested old run did not change.
            self.assertEqual(self.get(target).status, 200)
            project.assert_called_once()
            evaluate.assert_called_once()
            self.assertEqual(resolved_revisions[-1], saved['revision_id'])
            self.assertEqual(project.call_args.args[0], saved['structured_profile'])
            projected = evaluate.call_args.args[0]
            self.assertEqual(projected['education']['education_level'], 'phd')
            self.assertEqual(projected['education']['fields_or_domains'], education['fields_or_domains'])
            self.assertIn('U Penn', projected['education']['institutions'])
            current_run = self.matcher.last_run()
            self.assertNotEqual(current_run.match_run_id, old_run.match_run_id)
            matcher_input = current_run.recommendation_context['matcher_profile']
            self.assertEqual(matcher_input['education_level'], 'phd')
            self.assertIn('Biology', matcher_input['degrees_or_domains'])
            self.assertEqual(old_run.recommendation_context, old_snapshot)
            self.assertEqual(
                self.matcher.integration._registry.peek(old_run.match_run_id).recommendation_context,
                old_snapshot,
            )
            project.reset_mock()
            evaluate.reset_mock()

            # A current request without an explicit run always evaluates current
            # authority; it does not silently select the historical snapshot.
            self.assertEqual(self.get().status, 200)
            project.assert_called_once()
            evaluate.assert_called_once()
            self.assertEqual(resolved_revisions[-1], saved['revision_id'])
            self.assertEqual(project.call_args.args[0], saved['structured_profile'])
            self.assertNotEqual(self.matcher.last_run().match_run_id, current_run.match_run_id)
            self.assertEqual(old_run.recommendation_context, old_snapshot)
            self.assertEqual(self.f._profile_counts()[1], counts[1] + 1)


if __name__ == '__main__':
    unittest.main()
