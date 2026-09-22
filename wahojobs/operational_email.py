"""Small Resend HTTPS adapter for the reviewed version-2 operational outbox.

No SMTP, authentication-email reuse, provider SDK, redirects or automatic retries.
Durable per-event claims outlive Resend's 24-hour idempotency window.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from wahojobs.daily_inventory import read_json, write_json, parse
from wahojobs.daily_source_policy import ALERT_RECIPIENT
from wahojobs.maintenance_gate import operation_gate

SENDER = 'Wahojobs Operations <alerts@ops.wahojobs.com>'
ENDPOINT = 'https://api.resend.com/emails'
MAX_PACKET_BYTES = 256_000
MAX_EVENTS = 200
DAILY_MESSAGE_LIMIT = 25  # hourly batches plus one controlled commissioning message


class DeliveryUnavailable(RuntimeError):
    """Safe, fixed diagnostic; never includes a credential or HTTP response."""


def validate_packet(packet):
    if (type(packet) is not dict or set(packet) != {'version', 'application', 'recipient', 'events'}
            or type(packet['version']) is not int or packet['version'] != 2
            or packet['application'] != 'wahojobs-beta' or packet['recipient'] != ALERT_RECIPIENT
            or type(packet['events']) is not list or not 0 < len(packet['events']) <= MAX_EVENTS):
        raise ValueError('invalid_operational_delivery_packet')
    seen = set()
    for event in packet['events']:
        if (type(event) is not dict or not {'id', 'kind', 'key', 'at'} <= set(event)
                or set(event) - {'id', 'kind', 'key', 'at', 'issue', 'delivery'}
                or type(event['id']) is not str or not re.fullmatch('[a-f0-9]{32}', event['id'])
                or event['id'] in seen or event['kind'] not in ('opened', 'recovered')
                or type(event['key']) is not str or not re.fullmatch('[a-z0-9_]+:[a-z0-9_]+', event['key'])
                or type(event['at']) is not str or len(event['at']) > 64
                or 'issue' in event and type(event['issue']) is not dict):
            raise ValueError('invalid_operational_event')
        parse(event['at']); seen.add(event['id'])
    if len(json.dumps(packet, allow_nan=False).encode()) > MAX_PACKET_BYTES:
        raise ValueError('operational_packet_too_large')
    return packet


def message(events):
    opened = sum(event['kind'] == 'opened' for event in events)
    recovered = len(events) - opened
    lines = ['Wahojobs beta inventory operations', '',
             f'{opened} new issue(s); {recovered} recovery notice(s).', '']
    for event in events:
        lines.extend([f"{event['kind'].upper()}: {event['key']}", f"Observed: {event['at']}",
                      'Event: ' + event['id']])
        if event.get('issue'): lines.append(json.dumps(event['issue'], ensure_ascii=True, sort_keys=True))
        lines.append('')
    lines.append('This message reports stored operational state. It does not perform employer requests.')
    return dict(from_=SENDER, to=[ALERT_RECIPIENT],
                subject=f'Wahojobs inventory: {opened} issue(s), {recovered} recovery notice(s)',
                text='\n'.join(lines))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeliveryUnavailable('operational_delivery_redirect_refused')


def https_send(payload, credential, idempotency_key):
    request = Request(ENDPOINT, data=json.dumps(payload, allow_nan=False).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json',
        'Idempotency-Key': idempotency_key, 'User-Agent': 'Wahojobs-Operations/1'})
    try:
        # Do not inherit arbitrary environment proxies for credential delivery.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            if response.status not in (200, 201): raise DeliveryUnavailable()
            raw = response.read(4097)
            if len(raw) > 4096: raise DeliveryUnavailable()
            value = json.loads(raw)
            if type(value) is not dict or not re.fullmatch('[a-f0-9-]{36}', value.get('id', '')):
                raise DeliveryUnavailable()
            return value['id']
    except Exception:
        raise DeliveryUnavailable('operational_delivery_failed_or_uncertain') from None


def send_packet(packet, credential, state_directory, *, transport=None, at=None):
    """Single attempt; unknown outcomes are never automatically repeated."""
    packet = validate_packet(packet)
    if type(credential) is not str or not re.fullmatch(r're_[A-Za-z0-9_-]{12,250}', credential):
        raise ValueError('invalid_delivery_credential')
    root = Path(state_directory)
    if not root.is_dir(): raise ValueError('prepared_delivery_state_required')
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None: raise ValueError('aware_delivery_time_required')
    day = at.astimezone(timezone.utc).date().isoformat()
    with operation_gate(root / 'resend-delivery'):
        path = root / 'resend-delivery.json'
        ledger = read_json(path, dict(version=1, events={}, attempts_by_day={}))
        if (type(ledger) is not dict or ledger.get('version') != 1
                or type(ledger.get('events')) is not dict or type(ledger.get('attempts_by_day')) is not dict):
            raise ValueError('delivery_ledger_invalid')
        prior = [ledger['events'][event['id']] for event in packet['events'] if event['id'] in ledger['events']]
        fresh = [event for event in packet['events'] if event['id'] not in ledger['events']]
        if not fresh:
            if all(row.get('status') == 'accepted_by_api' for row in prior): return 'previously_accepted'
            raise DeliveryUnavailable('prior_delivery_uncertain_no_retry')
        if ledger['attempts_by_day'].get(day, 0) >= DAILY_MESSAGE_LIMIT:
            raise DeliveryUnavailable('operational_daily_message_limit')
        ids = sorted(event['id'] for event in fresh)
        key = 'wahojobs-inventory-' + sha256('\n'.join(ids).encode()).hexdigest()
        payload = message(fresh); payload['from'] = payload.pop('from_')
        digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        ledger['attempts_by_day'][day] = ledger['attempts_by_day'].get(day, 0) + 1
        for event_id in ids:
            ledger['events'][event_id] = dict(status='attempted', at=at.isoformat(), payload_sha256=digest)
        # Reserve before any transmission. A killed process leaves consumed IDs.
        write_json(path, ledger)
        try:
            request_id = (transport or https_send)(payload, credential, key)
        except Exception:
            for event_id in ids: ledger['events'][event_id]['status'] = 'failed_or_uncertain'
            write_json(path, ledger)
            raise DeliveryUnavailable('operational_delivery_failed_or_uncertain') from None
        for event_id in ids:
            ledger['events'][event_id].update(status='accepted_by_api', request_id=request_id)
        write_json(path, ledger)
        if any(row.get('status') != 'accepted_by_api' for row in prior):
            raise DeliveryUnavailable('prior_delivery_uncertain_no_retry')
        return 'accepted_by_api'  # This does not establish owner receipt.


def systemd_credential():
    """LoadCredential file only; never accept secrets as arguments or env values."""
    directory = Path(os.environ.get('CREDENTIALS_DIRECTORY', ''))
    if not directory.is_absolute(): raise ValueError('systemd_credential_required')
    path = directory / 'resend-api-key'
    value = path.lstat()
    if (not stat.S_ISREG(value.st_mode) or path.is_symlink() or value.st_nlink != 1
            or value.st_mode & 0o077 or value.st_size > 512):
        raise ValueError('private_systemd_credential_required')
    return path.read_text().strip()
