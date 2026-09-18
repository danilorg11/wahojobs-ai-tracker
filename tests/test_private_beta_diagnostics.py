import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_durable_product_browser_handler import _handler, _Integration, _plain_response, _DeliveryResponse
from wahojobs.request_diagnostics import RequestDiagnostic, RequestDiagnostics, diagnostic_log
from wahojobs.workos_authkit_browser import _cookie, _cookie_check, _OPAQUE


class PrivateBetaDiagnosticsTests(unittest.TestCase):
    def test_cookie_classification_preserves_the_shared_cookie_contract(self):
        name = '__Host-wahojobs_login_csrf'
        token = 'a' * 43
        cases = (
            ((), 'login_cookie_header_absent'),
            ((('Cookie', name + '=' + token), ('Cookie', 'another=value')),
                'login_cookie_headers_multiple'),
            ((('Cookie', name + '=' + token + '; malformed'),), 'login_cookie_segment_rejected'),
            ((('Cookie', name + '=' + token + '; ' + name + '=' + token),),
                'login_cookie_target_duplicate'),
            ((('Cookie', name + '=' + token),), None),
            ((('Cookie', name + '=' + token + '; ' + '; '.join(f'other{i}=value' for i in range(15))),), None),
        )
        for headers, reason in cases:
            with self.subTest(reason=reason):
                value, valid, actual = _cookie_check(headers, name, _OPAQUE)
                self.assertEqual(actual, reason)
                self.assertEqual(_cookie(headers, name, _OPAQUE), (value, valid))
                self.assertEqual(valid, reason is None)
                self.assertEqual(value, token if reason is None else None)

    def test_optional_login_labels_cannot_expose_values_or_reject_valid_delivery(self):
        from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
        class Response:
            status = 403
            body = b'rejected'
            headers = (('Content-Length', '8'),)
            def __init__(self, value):
                self.value = value
            @property
            def login_start_outcome(self):
                if isinstance(self.value, BaseException):
                    raise self.value
                return self.value
        for value, expected in (
            ('private-secret', 'response'),
            ([], 'response'),
            (RuntimeError('private-secret'), 'response'),
            ('login_authorization_prepared', 'response'),
            ('login_csrf_mismatch', 'login_csrf_mismatch'),
        ):
            with self.subTest(kind=type(value).__name__):
                records = RequestDiagnostics()
                integration = _Integration(Response(value))
                handler, _, events = _handler(integration, target='/auth/workos/start')
                handler.__class__ = make_durable_product_browser_handler(integration, diagnostics=records)
                handler.do_POST()
                record = records.snapshot()[-1]
                self.assertEqual((record.status, record.outcome), (403, expected))
                self.assertNotIn('private-secret', repr(record) + repr(events))
                self.assertNotIn('login_csrf_mismatch', repr(events))

    def test_request_identifiers_are_generated_and_private_request_data_is_omitted(self):
        records = RequestDiagnostics(capacity=2)
        secrets = 'private-email@example.test&code=provider-secret&profile_id=owner-secret'
        for index in range(3):
            from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
            handler, _, events = _handler(_Integration(_plain_response()), target='/auth/workos/callback?'+secrets)
            # Factory injection is exercised by constructing the handler type with the same transport doubles.
            handler.__class__ = make_durable_product_browser_handler(_Integration(_plain_response()), diagnostics=records)
            handler.headers['X-Wahojobs-Request-ID'] = 'attacker-controlled-id'
            handler.headers['Cookie'] = 'private-cookie'
            handler.do_GET()
            ids = [e[2] for e in events if e[:2] == ('send_header','X-Wahojobs-Request-ID')]
            self.assertEqual(len(ids),1)
            self.assertRegex(ids[0], r'^[0-9a-f]{32}$')
        stored = records.snapshot()
        self.assertEqual(len(stored),2)
        self.assertNotEqual(stored[0].request_id, stored[1].request_id)
        self.assertEqual({r.route for r in stored},{'authentication'})
        self.assertNotIn('secret',repr(stored))
        self.assertNotIn('private-',repr(stored))
        self.assertNotIn('attacker',repr(stored))

    def test_failure_category_is_useful_without_exception_text_and_sink_failure_does_not_alter_delivery(self):
        from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
        records = RequestDiagnostics()
        integration = _Integration(failure=ValueError('secret token and private profile'))
        handler, _, events = _handler(integration)
        handler.__class__ = make_durable_product_browser_handler(integration, diagnostics=records)
        handler.do_POST()
        record = records.snapshot()[0]
        self.assertEqual((record.status,record.outcome),(503,'integration_failure'))
        self.assertNotIn('secret',repr(record))
        self.assertIn(('send_header','X-Wahojobs-Request-ID',record.request_id),events)
        events=[]
        integration=_Integration(_DeliveryResponse(events))
        handler,_,events=_handler(integration,events=events)
        with patch.object(RequestDiagnostics,'record',side_effect=RuntimeError('disk full')):
            handler.do_GET()
        self.assertEqual(events.count(('acknowledge',)),1)
        self.assertNotIn(('fail',),events)

    def test_logs_rotate_with_bounded_safe_records_and_refuse_existing_log_family(self):
        with tempfile.TemporaryDirectory() as directory:
            with diagnostic_log(directory) as records:
                for index in range(5500):
                    records.record(RequestDiagnostic(f'{index:032x}','GET','matches',200,'response',4))
                self.assertEqual(len(records.snapshot()),200)
            files = list(Path(directory).glob('requests.jsonl*'))
            self.assertLessEqual(len(files),3)
            self.assertGreater(len(files),1)
            for path in files:
                self.assertLessEqual(path.stat().st_size,262144)
                for line in path.read_text().splitlines():
                    self.assertEqual(set(json.loads(line)),{'request_id','method','route','status','outcome','elapsed_ms','observed_at'})
                    self.assertTrue(json.loads(line)['observed_at'].endswith('+00:00'))
            with self.assertRaisesRegex(ValueError,'already_exists'):
                with diagnostic_log(directory):
                    self.fail('Must refuse an existing log family')
