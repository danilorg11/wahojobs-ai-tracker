"""Offline callback labels preserve authentication and single-use transactions."""
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from tests import test_workos_authkit as gateway_fixture
from tests.workos_authkit_test_support import callback_target, deliver, snapshot, NOW
from wahojobs.browser_session_lifecycle import discard_request_scoped_session_secret_vault
from wahojobs.request_diagnostics import callback_outcome, CALLBACK_OUTCOME_STATUS
from wahojobs.workos_authkit import WorkOSAuthKitAuthentication, WorkOSAuthKitFailure
from wahojobs.workos_authkit_browser import _completion_outcome
from wahojobs.trusted_login_completion import _failure_result


class CallbackDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = gateway_fixture.WorkOSAuthKitGatewayTests('test_valid_invited_first_login_succeeds_exactly_once')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def prepared(self):
        return self.fixture.gateway.prepare_authorization(self.fixture.connection)

    def check(self, prepared, target, reason, exchanges):
        before = snapshot(self.fixture.connection)
        result, vault = self.fixture._complete_target(prepared, target)
        self.addCleanup(discard_request_scoped_session_secret_vault, vault)
        self.assertEqual(result.status, 'authentication_denied')
        self.assertEqual(result.callback_outcome, reason)
        self.assertEqual(self.fixture.boundary.exchange_count, exchanges)
        self.assertEqual(snapshot(self.fixture.connection), before)
        return result

    def test_parser_and_transaction_precedence_without_exchange(self):
        prepared = self.prepared()
        state = parse_qs(urlsplit(prepared.authorization_url).query)['state'][0]
        for target, reason in (
            ('https://foreign.example.test/auth/workos/callback', 'callback_target_rejected'),
            ('/auth/workos/callback?state=%QQ&code=private-code', 'callback_parameters_rejected'),
            ('/auth/workos/callback?state=invalid&code=private-code', 'callback_state_format_rejected'),
            ('/auth/workos/callback?'+urlencode({'state':'z'*43,'code':'private-code'}), 'callback_transaction_state_mismatch'),
        ):
            with self.subTest(reason=reason):
                result = self.check(prepared, target, reason, 0)
                self.assertEqual(_completion_outcome(result, 'login_cookie_target_absent'), reason)
                self.assertNotIn(state, repr(result))
                self.assertNotIn('private-code', repr(result))
        invalid = SimpleNamespace(transaction_id=None)
        result = self.check(invalid, callback_target(prepared), 'callback_transaction_input_rejected', 0)
        self.assertEqual(_completion_outcome(result, 'login_cookie_target_absent'), 'callback_cookie_target_absent')
        self.fixture.clock.advance(timedelta(minutes=11))
        self.check(prepared, callback_target(prepared), 'callback_transaction_not_found', 0)

    def test_provider_error_and_invalid_code_consume_claim_without_exchange(self):
        for key, value, reason in (
            ('error', 'private-provider-message', 'callback_provider_error_returned'),
            ('code', 'short', 'callback_code_format_rejected'),
        ):
            prepared = self.prepared()
            state = parse_qs(urlsplit(prepared.authorization_url).query)['state'][0]
            target = '/auth/workos/callback?' + urlencode({'state':state,key:value})
            self.check(prepared, target, reason, 0)
            self.check(prepared, target, 'callback_transaction_not_found', 0)

    def test_provider_claims_are_classified_without_retaining_projection(self):
        valid = dict(user_id='user_0123456789abcdef', email='private-person@example.test',
                     email_verified=True, authentication_method='MagicAuth')
        projections = [(object(), 'callback_claim_projection_rejected')]
        for field, value, reason in (
            ('user_id','private-invalid-subject','callback_claim_subject_rejected'),
            ('email',None,'callback_claim_email_type_rejected'),
            ('email_verified',False,'callback_claim_email_unverified'),
            ('authentication_method','private-other-method','callback_claim_method_rejected'),
        ):
            projections.append((WorkOSAuthKitAuthentication(**(valid | {field:value})), reason))
        for projected, reason in projections:
            prepared = self.prepared()
            original = self.fixture.boundary.exchange_code
            def exchange(**kwargs):
                original(**kwargs)
                return projected
            expected = self.fixture.boundary.exchange_count + 1
            with patch.object(self.fixture.boundary, 'exchange_code', side_effect=exchange):
                result = self.check(prepared, callback_target(prepared), reason, expected)
            self.assertNotIn('private-', repr(result))
            self.check(prepared, callback_target(prepared), 'callback_transaction_not_found', expected)

    def test_exchange_completion_clock_checks_remain_denials(self):
        for delta, reason in ((timedelta(seconds=-1),'callback_completion_clock_reversed'),
                              (timedelta(minutes=10),'callback_completion_expired')):
            self.fixture.clock.advance(NOW-self.fixture.clock())
            prepared = self.prepared()
            original = self.fixture.boundary.exchange_code
            def exchange(**kwargs):
                value = original(**kwargs)
                self.fixture.clock.advance(delta)
                return value
            expected = self.fixture.boundary.exchange_count + 1
            with patch.object(self.fixture.boundary, 'exchange_code', side_effect=exchange):
                self.check(prepared, callback_target(prepared), reason, expected)

    def test_existing_subject_return_and_missing_subject_same_email_stay_distinct(self):
        _invitation, _prepared, _target, issued, vault = self.fixture._first_login()
        deliver(self.fixture.connection, issued, vault)
        prepared = self.prepared()
        returned, vault = self.fixture._complete_target(prepared, callback_target(prepared))
        self.assertEqual(returned.status, 'issued')
        deliver(self.fixture.connection, returned, vault)
        self.fixture.boundary.subject = 'user_otherverifiedsubject'
        prepared = self.prepared()
        self.check(prepared, callback_target(prepared), 'callback_durable_identity_missing_uninvited', 3)
        self.assertEqual(snapshot(self.fixture.connection)['users'], 1)
        self.assertEqual(snapshot(self.fixture.connection)['account_sessions'], 2)

    def test_identity_rejection_and_sealed_session_denial_are_distinct(self):
        _invitation, _prepared, _target, issued, vault = self.fixture._first_login()
        deliver(self.fixture.connection, issued, vault)
        prepared = self.prepared()
        with patch('wahojobs.workos_authkit.authoritative_auth_identity_row_valid', return_value=False):
            self.check(prepared, callback_target(prepared), 'callback_durable_identity_rejected', 2)
        denied = _failure_result('authentication_denied')
        self.assertEqual(_completion_outcome(denied, None), 'callback_session_authentication_denied')
        self.assertEqual(denied.status, 'authentication_denied')

    def test_optional_metadata_is_closed_and_cannot_override_status(self):
        for label, status in CALLBACK_OUTCOME_STATUS.items():
            self.assertEqual(callback_outcome(SimpleNamespace(callback_outcome=label), status), label)
            self.assertIsNone(callback_outcome(SimpleNamespace(callback_outcome=label), 200))
        for value in ('private-secret', {'private':'secret'}, None):
            self.assertIsNone(callback_outcome(SimpleNamespace(callback_outcome=value), 401))
        class Faulty:
            @property
            def callback_outcome(self):
                raise RuntimeError('private-secret')
        self.assertIsNone(callback_outcome(Faulty(), 401))
        class FaultyStatus:
            @property
            def status(self):
                raise RuntimeError('private-secret')
        self.assertIsNone(_completion_outcome(FaultyStatus(), None))
        self.assertEqual(_completion_outcome(FaultyStatus(), None, status='authentication_denied'),
                         'callback_authentication_denied')
        malformed = WorkOSAuthKitFailure('authentication_denied')
        object.__setattr__(malformed, 'callback_outcome', {'private': 'secret'})
        self.assertIsNone(_completion_outcome(malformed, None, status='authentication_denied'))
        with self.assertRaises(ValueError):
            WorkOSAuthKitFailure('authentication_denied', 'private-secret')
        with self.assertRaises(ValueError):
            WorkOSAuthKitFailure('unavailable', 'callback_claim_email_unverified')


if __name__ == '__main__':
    unittest.main()
