"""No email or employer network: test the existing outbox's Resend boundary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, build_opener

from wahojobs import operational_email as email
from wahojobs.daily_inventory import read_json, write_json
from wahojobs.maintenance_gate import operation_gate

KEY = 're_isolated_test_credential'
AT = datetime(2026, 9, 22, 13, tzinfo=timezone.utc)


def packet(*numbers):
    return dict(version=2, application='wahojobs-beta', recipient=email.ALERT_RECIPIENT,
                events=[dict(id=f'{n:032x}', kind='opened', key='mercor:age36', at=AT.isoformat(),
                             issue=dict(severity='warning', records=n), delivery='attempted') for n in numbers])


class OperationalEmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = Mock(return_value='49a3999c-0ce1-4ea6-ab68-afcd6dc2e794')

    def send(self, value, **kwargs):
        return email.send_packet(value, KEY, self.root, transport=self.transport, at=kwargs.get('at', AT))

    def test_batches_to_only_approved_recipient_and_preserves_event_ids(self):
        self.assertEqual(self.send(packet(1, 2)), 'accepted_by_api')
        body, credential, key = self.transport.call_args.args
        self.assertEqual(body['from'], email.SENDER); self.assertEqual(body['to'], [email.ALERT_RECIPIENT])
        self.assertEqual(set(body), {'from', 'to', 'subject', 'text'})
        self.assertIn(f'{1:032x}', body['text']); self.assertIn(f'{2:032x}', body['text'])
        self.assertEqual(credential, KEY); self.assertTrue(key.startswith('wahojobs-inventory-'))
        self.assertNotIn(KEY, (self.root/'resend-delivery.json').read_text())
        self.assertEqual(self.send(packet(1, 2), at=AT+timedelta(days=2)), 'previously_accepted')
        self.assertEqual(self.transport.call_count, 1)

    def test_overlapping_batches_deduplicate_each_event_after_restart(self):
        self.send(packet(1, 2)); self.send(packet(2, 3))
        text = self.transport.call_args.args[0]['text']
        self.assertNotIn(f'{2:032x}', text); self.assertIn(f'{3:032x}', text)
        self.assertEqual(len(read_json(self.root/'resend-delivery.json')['events']), 3)

    def test_ambiguous_failure_consumes_ids_and_never_retries(self):
        self.transport.side_effect = TimeoutError(KEY)
        for value in (packet(1), packet(1)):
            with self.assertRaises(email.DeliveryUnavailable) as raised: self.send(value)
            self.assertNotIn(KEY, str(raised.exception))
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(read_json(self.root/'resend-delivery.json')['events'][f'{1:032x}']['status'], 'failed_or_uncertain')

    def test_pretransmission_persistence_and_concurrent_lock_fail_closed(self):
        with patch.object(email, 'write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.send(packet(1))
        self.transport.assert_not_called()
        with operation_gate(self.root/'resend-delivery'):
            with self.assertRaises(OSError): self.send(packet(1))
        self.transport.assert_not_called()

    def test_crash_reservation_and_quota_cannot_resend_or_increase_limit(self):
        write_json(self.root/'resend-delivery.json', dict(version=1,
            events={f'{1:032x}':dict(status='attempted')}, attempts_by_day={AT.date().isoformat():25}))
        with self.assertRaises(email.DeliveryUnavailable): self.send(packet(1))
        with self.assertRaises(email.DeliveryUnavailable): self.send(packet(2))
        self.transport.assert_not_called()

    def test_rejects_changed_destination_duplicates_invalid_schema_and_large_packets(self):
        samples = []
        wrong = packet(1); wrong['recipient']='someone@example.com'; samples.append(wrong)
        wrong = packet(1,1); samples.append(wrong)
        wrong = packet(1); wrong['events'][0]['id']='../arbitrary'; samples.append(wrong)
        wrong = packet(1); wrong['events'][0]['issue']['detail']='x'*email.MAX_PACKET_BYTES; samples.append(wrong)
        wrong = packet(1); wrong['events'][0]['at']='unknown'; samples.append(wrong)
        for value in samples:
            with self.subTest(value=list(value)), self.assertRaises(ValueError): self.send(value)
        self.transport.assert_not_called()

    def test_https_transport_has_exact_scope_and_redacts_provider_failure(self):
        response = Mock(status=200); response.read.return_value=json.dumps({'id':self.transport.return_value}).encode()
        context = Mock(); context.__enter__=Mock(return_value=response); context.__exit__=Mock(return_value=False)
        opener = Mock(); opener.open.return_value=context
        with patch.object(email, 'build_opener', return_value=opener):
            self.assertEqual(email.https_send({'text':'fixture'}, KEY, 'fixture-key'), self.transport.return_value)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, email.ENDPOINT); self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
        opener.open.side_effect=OSError('Bearer '+KEY)
        with patch.object(email, 'build_opener', return_value=opener), self.assertRaises(email.DeliveryUnavailable) as raised:
            email.https_send({'text':'fixture'}, KEY, 'fixture-key')
        self.assertNotIn(KEY, str(raised.exception))

    def test_redirect_and_bad_success_response_are_not_accepted(self):
        with self.assertRaises(email.DeliveryUnavailable):
            email.NoRedirect().redirect_request(Request(email.ENDPOINT), None, 302, 'Found', {}, 'https://outside.example/')
        response=Mock(status=200); response.read.return_value=b'{"success":true}'
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=context
        with patch.object(email,'build_opener',return_value=opener),self.assertRaises(email.DeliveryUnavailable):
            email.https_send({},KEY,'fixture-key')


if __name__ == '__main__': unittest.main()
