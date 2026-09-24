"""Bounded beta-host public evidence capture for Handshake, Outlier and Surge.

This collects raw public bodies outside maintenance. It neither publishes nor
marks any source ready. A pending attempt blocks later capture until audited.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wahojobs.crawler.providers import handshake, surge

ROOT = Path(__file__).resolve().parents[1]
LEDGER = Path('/var/lib/wahojobs-beta/remaining-source-coverage-v1/three-source-task-ledger')
HOST = 'wahojobs-private-beta-rehearsal-20260917'
LIMITS = {'handshake': 100, 'outlier': 100, 'surge': 100}
AGGREGATE = 200
SOURCE_SECONDS = {'handshake': 360, 'outlier': 60, 'surge': 360}
REQUEST_SECONDS = {'handshake': 60, 'outlier': 30, 'surge': 30}
MAX_BODY = 8_000_000
VERSION = 'remaining_three_source_capture_v1'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def source_deadline(_signum, _frame):
    raise TimeoutError('capture_source_deadline')


def write_once(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def exact_url(url, source, *, modules=(), chunks=(), details=()):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
            or parsed.query or parsed.fragment):
        raise ValueError('capture_url_out_of_scope')
    if source == 'handshake':
        return (url == 'https://joinhandshake.com/ai/opportunities/'
                or url in modules or url in chunks)
    if source == 'outlier':
        return (url == 'https://app.outlier.ai/internal/experts/job-board/jobs'
                or url in details)
    if source == 'surge':
        return (url in ('https://surgehq.ai/workforce', 'https://surgehq.ai/fellowship')
                or url in details)
    return False


def attempts_used():
    used = {name: 0 for name in LIMITS}
    runs = LEDGER / 'runs'
    if not runs.exists():
        return used
    for run in runs.iterdir():
        if run.is_symlink() or not run.is_dir():
            raise ValueError('unsafe_capture_run')
        plan = json.loads((run / 'plan.json').read_text())
        source = plan['source']
        if source not in LIMITS or plan['version'] != VERSION:
            raise ValueError('capture_plan_mismatch')
        reservations = list(run.glob('attempt-*.json'))
        used[source] += len(reservations)
        if not (run / 'receipt.json').is_file():
            raise ValueError('unfinished_capture_requires_audit')
        for reservation in reservations:
            ordinal = int(reservation.stem.removeprefix('attempt-'))
            if not (run / f'completion-{ordinal:03d}.json').is_file():
                raise ValueError('uncertain_capture_attempt_requires_audit')
    if any(used[name] > LIMITS[name] for name in LIMITS) or sum(used.values()) > AGGREGATE:
        raise ValueError('capture_budget_exceeded')
    return used


def retained_responses(source, run_name):
    if not re.fullmatch(r'\d{8}T\d{12}Z-' + source, run_name):
        raise ValueError('capture_resume_run_invalid')
    run = LEDGER / 'runs' / run_name
    plan = json.loads((run / 'plan.json').read_text())
    receipt = json.loads((run / 'receipt.json').read_text())
    if (plan['source'] != source or plan['version'] != VERSION
            or receipt['source'] != source):
        raise ValueError('capture_resume_source_mismatch')
    retained = {}
    for completion in sorted(run.glob('completion-*.json')):
        row = json.loads(completion.read_text())
        if row['status'] != 200 or row['final_url'] != row['requested_url'] or not row['complete']:
            continue
        body = (run / row['body_file']).read_bytes()
        if sha256(body).hexdigest() != row['body_sha256']:
            raise ValueError('capture_resume_hash_mismatch')
        retained[row['requested_url']] = body
    return retained


@contextmanager
def task_lock():
    import fcntl
    LEDGER.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = LEDGER / '.task-lock'
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe_capture_lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Recorder:
    def __init__(self, source, run, prior, retained=None):
        self.source, self.run, self.prior = source, run, prior
        self.retained = retained or {}
        self.reused = []
        self.started = time.monotonic()
        self.ordinal = 0
        self.modules = set()
        self.chunks = set()
        self.details = set()

    def request(self, url, *, method='GET', body=None, headers=None):
        if not exact_url(url, self.source, modules=self.modules, chunks=self.chunks,
                         details=self.details):
            raise ValueError('capture_url_out_of_scope')
        expected_method = ('POST' if self.source == 'outlier' and not self.details else 'GET')
        if method != expected_method:
            raise ValueError('capture_method_out_of_scope')
        if body != (b'{}' if method == 'POST' else None):
            raise ValueError('capture_body_out_of_scope')
        if url in self.retained:
            self.reused.append(url)
            return self.retained[url]
        if (self.prior[self.source] + self.ordinal >= LIMITS[self.source]
                or sum(self.prior.values()) + self.ordinal >= AGGREGATE):
            raise ValueError('capture_request_cap_reached')
        remaining = SOURCE_SECONDS[self.source] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError('capture_source_deadline')
        self.ordinal += 1
        n = self.ordinal
        write_once(self.run / f'attempt-{n:03d}.json', dict(version=VERSION,
            ordinal=n, source=self.source, requested_url=url, method=method,
            requested_at=stamp()))
        started = time.monotonic()
        request = Request(url, data=body, headers=headers or {}, method=method)
        status, final_url, response_headers, raw, error = None, None, None, b'', None
        try:
            with build_opener(NoRedirect()).open(request,
                    timeout=min(REQUEST_SECONDS[self.source], remaining)) as response:
                status = response.status
                final_url = response.geturl()
                response_headers = response.headers
                raw = response.read(MAX_BODY + 1)
        except HTTPError as exc:
            status, final_url, response_headers = exc.code, exc.geturl(), exc.headers
            raw = exc.read(MAX_BODY + 1)
        except (URLError, TimeoutError, OSError) as exc:
            error = type(exc).__name__
        body_name = f'response-{n:03d}.raw'
        with (self.run / body_name).open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        selected_headers = {key:response_headers.get(key) for key in
            ('Content-Type','Location','Date','ETag') if response_headers is not None
            and response_headers.get(key) is not None}
        write_once(self.run / f'completion-{n:03d}.json', dict(version=VERSION,
            ordinal=n, source=self.source, requested_url=url, final_url=final_url,
            status=status, error_type=error, response_headers=selected_headers,
            body_file=body_name, body_bytes=len(raw), body_sha256=sha256(raw).hexdigest(),
            complete=len(raw) <= MAX_BODY, completed_at=stamp(),
            elapsed_seconds=round(time.monotonic()-started,3)))
        if self.source == 'outlier' and method == 'GET' and status == 302:
            old = re.fullmatch(
                r'https://app\.outlier\.ai/en/expert/opportunities/(\d+)', url)
            target = response_headers.get('Location') if response_headers else None
            if (old is not None and target ==
                    'https://app.outlier.ai/opportunities/'+old.group(1)):
                self.details.add(target)
                return self.request(target, headers=headers)
        if (error or status != 200 or final_url != url or len(raw) > MAX_BODY):
            raise ValueError(f'capture_response_not_qualified:{status}:{error}')
        return raw


def capture_handshake(recorder):
    url = 'https://joinhandshake.com/ai/opportunities/'
    page = recorder.request(url, headers=handshake.REQUEST_HEADERS).decode('utf-8','replace')
    modules = handshake.extract_framer_module_urls(page)
    if not modules or len(modules) > handshake.MAX_MODULES:
        raise ValueError('handshake_module_count_invalid')
    recorder.modules.update(modules)
    collections = {}
    for module in modules:
        text = recorder.request(module, headers=handshake.REQUEST_HEADERS).decode('utf-8','replace')
        names = (handshake.OPPORTUNITIES_COLLECTION, handshake.SUBJECT_FILTERS_COLLECTION,
                 handshake.DEGREE_FILTERS_COLLECTION)
        declared = [name for name in names if f'displayName:`{name}`' in text]
        if len(declared) > 1:
            raise ValueError('handshake_module_ambiguous')
        if declared:
            name = declared[0]
            if name in collections:
                raise ValueError('handshake_duplicate_collection')
            collections[name] = handshake.extract_collection_chunk_urls(text, linked_module_url=module)
    if handshake.OPPORTUNITIES_COLLECTION not in collections:
        raise ValueError('handshake_opportunity_collection_missing')
    recorder.chunks.update(chunk for group in collections.values() for chunk in group)
    counts = {}
    for name, chunks in collections.items():
        records = []
        for chunk in chunks:
            data = recorder.request(chunk, headers=handshake.REQUEST_HEADERS)
            decoder = handshake.FramerCmsDecoder(data)
            records.extend(decoder.read_records())
            if decoder.offset != len(data):
                raise ValueError('handshake_chunk_trailing_bytes')
        counts[name] = len(records)
        if name == handshake.OPPORTUNITIES_COLLECTION:
            visible = [r for r in records if r.get(handshake.FIELD_SHOW_JOB) is True]
            if any(not handshake.should_include_record(r) for r in visible):
                raise ValueError('handshake_visible_record_invalid')
            ids = [str(r[handshake.FIELD_ID]) for r in visible]
            slugs = [str(r[handshake.FIELD_SLUG]) for r in visible]
            if len(ids) != len(set(ids)) or len(slugs) != len(set(slugs)):
                raise ValueError('handshake_visible_duplicate')
            counts['visible_opportunities'] = len(visible)
    return {'modules':len(modules),'collections':{k:len(v) for k,v in collections.items()},
            'record_counts':counts}


def capture_outlier(recorder):
    url = 'https://app.outlier.ai/internal/experts/job-board/jobs'
    raw = recorder.request(url, method='POST', body=b'{}', headers={
        'User-Agent':'Mozilla/5.0 (compatible; WahojobsTracker/0.1)',
        'Accept':'application/json','Content-Type':'application/json',
        'Origin':'https://app.outlier.ai','Referer':'https://app.outlier.ai/opportunities'})
    value = json.loads(raw)
    if (type(value) is not dict or type(value.get('jobs')) is not list
            or value.get('page') != 1 or value.get('totalPages') != 1
            or value.get('totalCount') != len(value['jobs']) or not value['jobs']):
        raise ValueError('outlier_envelope_incomplete')
    for job in value['jobs']:
        identity = str(job.get('id')) if type(job) is dict else ''
        url = job.get('absolute_url') if type(job) is dict else None
        if (type(url) is not str or not identity.isdecimal()
                or url != 'https://app.outlier.ai/en/expert/opportunities/'+identity):
            raise ValueError('outlier_record_url_out_of_scope')
        recorder.details.add(url)
    for url in sorted(recorder.details):
        recorder.request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; WahojobsTracker/0.1)',
                                       'Accept':'text/html,application/xhtml+xml'})
    return {'root_type':'dict','keys':sorted(value), 'jobs':len(value['jobs']),
            'detail_responses':len(recorder.details)}


def capture_surge(recorder):
    base = 'https://surgehq.ai'
    index = recorder.request(base+'/workforce', headers=surge.REQUEST_HEADERS).decode('utf-8','replace')
    records = surge.extract_workforce_records(index, base+'/workforce')
    if not records or len(records) > 18:
        raise ValueError('surge_index_count_invalid')
    for record in records:
        parsed = urlsplit(record.url)
        if (parsed.scheme != 'https' or parsed.netloc != 'surgehq.ai'
                or parsed.query or parsed.fragment
                or re.fullmatch(r'/workforce/[A-Za-z0-9-]+',parsed.path) is None
                or parsed.path.rsplit('/',1)[-1] != record.slug):
            raise ValueError('surge_index_detail_out_of_scope')
    recorder.details.update(record.url for record in records)
    fellowship = recorder.request(base+'/fellowship',headers=surge.REQUEST_HEADERS).decode('utf-8','replace')
    surge.parse_fellowship_page(base+'/fellowship',fellowship)
    accepted = []
    rejected = {}
    for record in records:
        page = recorder.request(record.url,headers=surge.REQUEST_HEADERS).decode('utf-8','replace')
        try:
            job = surge.parse_workforce_detail(record,page)
            accepted.append(job.external_id)
        except (ValueError, RuntimeError) as exc:
            rejected[record.slug] = type(exc).__name__+':'+str(exc)[:120]
    if rejected:
        raise ValueError('surge_detail_contract_rejected:'+str(len(rejected)))
    return {'index_roles':len(records),'parsed_role_ids':accepted,'fellowship_observed':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',choices=tuple(LIMITS),required=True)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--resume-run')
    args = parser.parse_args()
    if (os.geteuid() != 0 or socket.gethostname() != HOST
            or not re.fullmatch(r'[a-f0-9]{40}',args.commit)
            or ROOT.resolve(strict=True) != Path('/opt/wahojobs-beta/releases')/args.commit):
        raise ValueError('pinned_beta_host_and_release_required')
    if LEDGER.is_symlink():
        raise ValueError('capture_ledger_symlink_forbidden')
    with task_lock():
        runs = LEDGER/'runs'
        runs.mkdir(mode=0o700,exist_ok=True)
        prior = attempts_used()
        retained = retained_responses(args.source, args.resume_run) if args.resume_run else {}
        if prior[args.source] >= LIMITS[args.source] or sum(prior.values()) >= AGGREGATE:
            raise ValueError('task_capture_budget_exhausted')
        run = runs/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+args.source)
        run.mkdir(mode=0o700)
        write_once(run/'plan.json',dict(version=VERSION,source=args.source,
            code_commit=args.commit,execution_host=HOST,started_at=stamp(),
            limits={'source':LIMITS[args.source],'aggregate':AGGREGATE,
                    'source_seconds':SOURCE_SECONDS[args.source]},
            prior_attempts=prior,parser_sha256={name:sha256((ROOT/name).read_bytes()).hexdigest()
                for name in ('wahojobs/crawler/providers/handshake.py',
                             'wahojobs/crawler/providers/outlier.py',
                             'wahojobs/crawler/providers/surge.py')},
            resumed_run=args.resume_run))
        recorder=Recorder(args.source,run,prior,retained)
        result,error=None,None
        previous_alarm = signal.signal(signal.SIGALRM, source_deadline)
        signal.setitimer(signal.ITIMER_REAL, SOURCE_SECONDS[args.source])
        try:
            result={'handshake':capture_handshake,'outlier':capture_outlier,
                    'surge':capture_surge}[args.source](recorder)
        except Exception as exc:
            error=type(exc).__name__+':'+str(exc)[:160]
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_alarm)
        receipt=dict(version=VERSION,source=args.source,code_commit=args.commit,
            finished_at=stamp(),elapsed_seconds=round(time.monotonic()-recorder.started,3),
            http_attempts=recorder.ordinal,status='captured' if error is None else 'partial_or_failed',
            parse_summary=result,error=error,no_publication=True,
            reused_prior_response_urls=recorder.reused)
        write_once(run/'receipt.json',receipt)
        print(json.dumps(receipt,sort_keys=True))
        return 0 if error is None else 1


if __name__=='__main__':
    raise SystemExit(main())
