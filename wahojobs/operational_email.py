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
    if (type(packet) is not dict or set(packet) not in ({'version', 'application', 'recipient', 'events'},
                                                       {'version', 'application', 'recipient', 'events', 'context'})
            or type(packet['version']) is not int or packet['version'] != 2
            or packet['application'] != 'wahojobs-beta' or packet['recipient'] != ALERT_RECIPIENT
            or type(packet['events']) is not list or not 0 < len(packet['events']) <= MAX_EVENTS
            or 'context' in packet and type(packet['context']) is not dict):
        raise ValueError('invalid_operational_delivery_packet')
    seen = set()
    for event in packet['events']:
        if (type(event) is not dict or not {'id', 'kind', 'key', 'at'} <= set(event)
                or set(event) - {'id', 'kind', 'key', 'at', 'issue', 'delivery', 'from_key'}
                or type(event['id']) is not str or not re.fullmatch('[a-f0-9]{32}', event['id'])
                or event['id'] in seen or event['kind'] not in ('opened', 'recovered', 'escalated', 'status_changed', 'first_verified')
                or type(event['key']) is not str or not re.fullmatch('[a-z0-9_]+:[a-z0-9_]+', event['key'])
                or type(event['at']) is not str or len(event['at']) > 64
                or 'from_key' in event and (type(event['from_key']) is not str or
                    not re.fullmatch('[a-z0-9_]+:[a-z0-9_]+',event['from_key']))
                or 'issue' in event and type(event['issue']) is not dict):
            raise ValueError('invalid_operational_event')
        parse(event['at']); seen.add(event['id'])
    if len(json.dumps(packet, allow_nan=False).encode()) > MAX_PACKET_BYTES:
        raise ValueError('operational_packet_too_large')
    return packet


def _readable(value):
    return parse(value).strftime('%d %b %Y %H:%M UTC') if value else 'time unavailable'


def _name(key):
    source=key.split(':',1)[0]
    return {'micro1':'micro1','rws':'RWS','oneforma':'OneForma','welocalize':'Welocalize',
            'dataannotation':'DataAnnotation','dataforce':'DataForce'}.get(source,source.capitalize())


def _event_line(event,*,resolved_application_at=None):
    key=event['key'];issue=event.get('issue') or {};name=_name(key)
    if key=='application:unavailable':
        if event['kind']=='recovered':return 'Application recovery verified at '+_readable(event['at'])+'. The failed inventory cycle remains failed.'
        if resolved_application_at:
            return 'Historical application outage detected at '+_readable(event['at'])+'; recovery verified at '+_readable(resolved_application_at)+'.'
        return 'Application-unavailable event detected at '+_readable(event['at'])+'. See the timestamped availability snapshot above for current status.'
    if key=='collection:state':
        return 'Collection verified '+issue.get('current_state','unknown')+' at '+_readable(issue.get('observed_at') or event['at'])+'.'
    if key=='run:accounting_reconciled':
        return 'Retained cycle '+issue['run_id']+' accounting corrected from original capture and transaction evidence. No new collection or verification renewal occurred.'
    if event['kind']=='first_verified':return f'{name}: first daily verification completed.'
    if event['kind']=='recovered':return f'{name}: the {key.split(":",1)[1].replace("_"," ")} problem resolved. Other open conditions remain separate.'
    if key.endswith(':coverage'):
        reason=issue.get('reason','coverage unavailable')
        if reason=='disabled_after_http_403':
            line=f'{name}: disabled after its approved endpoint returned HTTP 403 on {_readable(issue.get("last_attempt_at"))}.'
        else:line=f'{name}: daily coverage unavailable — {reason.rstrip(".")}.'
    elif ':cohort_' in key:
        state=issue.get('state');count=issue.get('records');unit='exact posting records'
        if state=='expired':line=f'{name}: verification expired for {count} {unit}; availability is unconfirmed, not employer-closed.'
        elif state=='escalated':line=f'{name}: {count} {unit} have gone at least 48 hours without verification.'
        else:line=f'{name}: {count} {unit} are approaching the verification deadline.'
        if issue.get('expires_at'):line+=' Deadline: '+_readable(issue['expires_at'])+'.'
    elif key.endswith(':collection'):
        status=issue.get('http_status')
        line=f'{name}: collection failed'+(f' (HTTP {status})' if status else '')+'.'
        if issue.get('last_attempt_at'):line+=' Last attempt: '+_readable(issue['last_attempt_at'])+'.'
    elif key.endswith(':accounting'):
        line=f'{name}: source accounting unavailable; '+('an attempt is evidenced, but its final outcome is unavailable.'
            if issue.get('attempt_started')=='yes' else 'attempt status is unavailable.')
    elif key.endswith(':qualification'):
        line=f'{name}: observation failed qualification; verification was not renewed.'
    elif key.endswith(':publication'):
        line=f'{name}: captured observation was not successfully published; inspect the publication receipt.'
    elif key.endswith(':undercoverage'):
        line=f'{name}: {issue.get("pending_records")} discovered or known posting IDs remain unverified; qualifying exact records were published.'
    elif key.startswith('run:'):
        line='Daily cycle: '+issue.get('reason','execution problem').replace('_',' ')+'.'
    elif key=='delivery:uncertain':line='Operational email delivery failed or is uncertain; the original message was not retried.'
    else:line=f'{name}: '+issue.get('reason','operational condition').replace('_',' ')+'.'
    if issue.get('corrective_action'):line+=' Next: '+issue['corrective_action']
    return line


def message(events,context=None):
    context=context or {};cycle=context.get('cycle') or {}
    counts={kind:sum(event['kind']==kind for event in events)
            for kind in ('opened','escalated','status_changed','first_verified','recovered')}
    state=cycle.get('state')
    state_text={'scheduled':'First daily check scheduled','pending':'Daily check due','running':'Daily check running',
                'missed':'Daily check missed','partial':'Partial daily check','complete':'Daily check complete',
                'failed':'Daily check failed'}.get(state,'Stored operational update')
    headline=state_text
    repair=context.get('operator_repair') or {}
    if repair.get('resolves_daily_failure'):headline='Inventory verified by operator repair'
    application_events=sorted((e for e in events if e['key']=='application:unavailable'),key=lambda e:parse(e['at']))
    resolved_at=application_events[-1]['at'] if application_events and application_events[-1]['kind']=='recovered' else None
    operating=context.get('operating') or {};application=operating.get('application') or {}
    collection=operating.get('collection') or {}
    if application_events:
        headline=('Beta application incident resolved' if resolved_at else 'Application availability incident')
    if application.get('available') is False:headline='CRITICAL — beta application unavailable'
    elif not application_events and any(e['key']=='collection:state' for e in events):
        headline='Collection '+collection.get('state','state verified')
    if counts['status_changed'] and not counts['opened'] and any(
            e.get('issue',{}).get('reason')=='disabled_after_http_403' for e in events):
        headline='Source paused after HTTP 403'
    if application.get('available') is False:headline='CRITICAL — beta application unavailable'
    change=[]
    for kind,label in (('opened','new alert'),('escalated','escalation'),('status_changed','status change'),
                       ('first_verified','first verification'),('recovered','resolved alert')):
        if counts[kind]:change.append(f'{counts[kind]} {label}'+('s' if counts[kind]!=1 else ''))
    subject='Wahojobs inventory: '+headline+('; '+', '.join(change) if change else '')
    lines=['Wahojobs beta inventory operations','',
           'Checked: '+_readable(context.get('checked_at') or events[0]['at'])+'.',
           'State snapshot: '+_readable(context.get('checked_at'))+'.']
    available=application.get('available',context.get('application_ready'))
    label='available' if available is True else 'unavailable' if available is False else 'not verified'
    lines.append('Application: '+label+'; last readiness observation '+_readable(application.get('checked_at'))+'.')
    lines.append('Last cycle ('+_readable(cycle.get('scheduled_at'))+'): '+state_text+'.')
    failure=cycle.get('failure') or {}
    if failure:
        phase=failure.get('phase','')
        label={'backup':'pre-publication backup','prepare-backup':'online backup preparation','prepare':'collection preparation','collection':'source collection',
            'publication':'source publication','stop':'application shutdown','finish':'final integrity checks',
            'restore':'application restoration'}.get(phase)
        if phase.startswith(('collect-','publish-')):
            label=('collection of ' if phase.startswith('collect-') else 'publication of ')+_name(phase.split('-',1)[1]+':coverage')
        if label:
            reason=('allotted execution time expired' if failure.get('timed_out') else
                {'recovery_snapshot_file_limit':'retained backup history exceeded its file limit',
                 'recovery_source_changed':'backup inputs changed during verification',
                 'recovery_snapshot_integrity_failed':'backup verification failed',
                 'recovery_journal_unavailable':'retained collection evidence was unavailable',
                 'no_completed_observations_to_publish':'no completed observations were available for publication'}.get(failure.get('reason'),
                    'the operation failed; inspect the retained diagnostic'))
            lines.append('Failure stage: '+label+'. Reason: '+reason+'.')
            if phase=='backup':lines.append('Publication did not start; captured observations did not renew verification.')
            elif phase=='prepare-backup':lines.append('Publication did not start; the application remained online during evidence preparation.')
    if state in ('partial','failed'):
        qualified=len(cycle.get('qualified_sources',[]));failed=len(cycle.get('failed_sources',[]))
        lines.append(f"Last cycle: {qualified} source{'s' if qualified!=1 else ''} verified and published; "
                     f"{failed} attempted source{'s' if failed!=1 else ''} failed. "+
                     ('Publication succeeded for the verified sources; the daily check was incomplete.' if qualified else
                      'No source publication was verified for this cycle.'))
        for field,label in (('accounting_unavailable_sources','source accounting unavailable'),
                            ('qualification_failed_sources','observations failed qualification'),
                            ('undercovered_sources','sources with pending posting verification'),
                            ('publication_failed_sources','publication incomplete or failed'),
                            ('not_attempted_sources','sources blocked, skipped or disabled')):
            names=cycle.get(field,[])
            if names:lines.append(f"{len(names)} {label}: "+', '.join(_name(name+':coverage') for name in names)+'.')
    elif state=='complete':lines.append('Last cycle completed its qualifying source checks.')
    if repair:
        lines.append('Subsequent operator repair ('+_readable(repair.get('started_at'))+'): '+repair['state']+'. '+
            str(len(repair.get('qualified_sources',[])))+' of '+str(len(repair.get('selected_sources',[])))+
            ' selected sources verified and published. The original scheduled-cycle receipt remains unchanged.')
        if repair.get('remaining_sources'):
            lines.append('Repair sources still requiring verification: '+', '.join(_name(s+':coverage') for s in repair['remaining_sources'])+'.')
        if repair.get('incomplete_sources'):
            lines.append('Repair sources with remaining posting verification: '+', '.join(_name(s+':coverage') for s in repair['incomplete_sources'])+'.')
    if cycle.get('new_opportunities') is not None and state in ('partial','complete'):
        lines.append(f"Catalog impact: {cycle['new_opportunities']:,} newly published opportunities "
                     f"({cycle['new_variants']:,} variants); {cycle['changed_variants']:,} changed variants; "
                     f"{cycle['confirmed_closed']} confirmed closures.")
    if context.get('expired_records'):
        lines.append(f"{context['expired_records']} exact posting records have expired verification; "
                     'their availability is unconfirmed, not employer-closed.')
    if 'active_incidents' in context:
        affected=context.get('affected_sources',[])
        conditions=context['active_incidents'];sources=len(affected)
        lines.append(f"Currently open: {conditions} distinct condition{'s' if conditions!=1 else ''} across "
                     f"{sources} source{'s' if sources!=1 else ''}"+
                     ((' — '+', '.join(_name(s+':coverage') for s in affected)) if affected else '')+'.')
    if collection:
        lines.append('Collection: '+collection.get('state','unknown')+'; observed '+_readable(collection.get('checked_at'))+'.')
        if collection.get('reason'):lines.append('Collection status reason: '+collection['reason'].replace('_',' ')+'.')
        if collection.get('next_execution'):lines.append('Actual next scheduled collection: '+collection['next_execution']+'.')
    elif context.get('publication_paused'):
        lines.append('Collection is paused pending operator clearance; no catch-up run is scheduled.')
    elif context.get('next_scheduled_execution'):
        lines.append('Next scheduled collection: '+_readable(context['next_scheduled_execution'])+'.')
    if context.get('source_state_checked_at'):
        lines.append('Source/cohort issues last checked: '+_readable(context['source_state_checked_at'])+'.')
    if context.get('source_issues'):
        lines.extend(['','Open source/cohort issues as of '+_readable(context.get('source_state_checked_at'))+':'])
        for key,issue in sorted(context['source_issues'].items()):
            lines.append('- '+_event_line(dict(key=key,kind='status_changed',issue=issue)))
    if resolved_at:lines.append('The application incident in this batch was resolved at '+_readable(resolved_at)+'. Both historical transitions are retained below.')
    lines.extend(['','Events included in this notification: '+(', '.join(change) if change else 'no new change')+'.',''])
    for event in events:
        if event['kind']=='first_verified':continue
        lines.append('- '+_event_line(event,resolved_application_at=resolved_at if event['key']=='application:unavailable' and parse(event['at'])<=parse(resolved_at or event['at']) else None))
    first=[_name(e['key']) for e in events if e['kind']=='first_verified']
    if first:lines.append('- First daily verification completed: '+', '.join(first)+'.')
    lines.extend(['','Technical references (retained in operational logs):'])
    for event in events:
        lines.append(f"{event['kind']} {event['key']} {event['id']} {event['at']}")
    return dict(from_=SENDER,to=[ALERT_RECIPIENT],subject=subject,text='\n'.join(lines))


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
        payload = message(fresh,packet.get('context')); payload['from'] = payload.pop('from_')
        digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        batch=root/'delivery-batches'/(key+'.json')
        if batch.exists():raise DeliveryUnavailable('existing_delivery_batch_requires_reconciliation')
        # Retain the exact coherent observation and rendered text before sending.
        write_json(batch,dict(rendered_at=at.isoformat(),context=packet.get('context',{}),
            event_ids=ids,events=fresh,payload=payload,payload_sha256=digest))
        ledger['attempts_by_day'][day] = ledger['attempts_by_day'].get(day, 0) + 1
        for event_id in ids:
            ledger['events'][event_id] = dict(status='attempted', at=at.isoformat(), payload_sha256=digest,
                rendered_at=at.isoformat(),snapshot_checked_at=(packet.get('context') or {}).get('checked_at'),batch=str(batch))
        # Reserve before any transmission. A killed process leaves consumed IDs.
        write_json(path, ledger)
        try:
            request_id = (transport or https_send)(payload, credential, key)
        except Exception:
            for event_id in ids: ledger['events'][event_id]['status'] = 'failed_or_uncertain'
            write_json(path, ledger)
            raise DeliveryUnavailable('operational_delivery_failed_or_uncertain') from None
        for event_id in ids:
            ledger['events'][event_id].update(status='accepted_by_api', request_id=request_id,
                accepted_at=datetime.now(timezone.utc).isoformat())
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
    mode = stat.S_IMODE(value.st_mode)
    # systemd 255 grants only the service UID access using a POSIX ACL. The
    # group bits in stat show the ACL mask, not permission for the root group.
    native_acl = False
    if os.name == 'posix' and value.st_uid == 0 and mode == 0o440:
        import struct
        uid = os.geteuid()
        expected = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in (
            (1, 4, 0xffffffff), (2, 4, uid), (4, 0, 0xffffffff), (16, 4, 0xffffffff), (32, 0, 0xffffffff)))
        try: native_acl = os.getxattr(path, 'system.posix_acl_access') == expected
        except (AttributeError, OSError): pass
    if (not stat.S_ISREG(value.st_mode) or path.is_symlink() or value.st_nlink != 1
            or (mode & 0o077 and not native_acl) or value.st_size > 512):
        raise ValueError('private_systemd_credential_required')
    return path.read_text().strip()
