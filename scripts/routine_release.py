"""Prepare offline; promote one prebuilt Vercel website; observe, never replay.

No SSH, database, provider configuration, access renewal or migration capability.
The CLI needs an exact plan hash supplied AFTER the owner's production decision.
Local files are audit/coordination, not credentials or proof of human consent.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

VERSION = 1
HERE = Path(__file__).resolve()
LEGACY_REGISTRY = Path('C:/Users/danrg/Documents/Codex/2026-09-30/task-2/WORKOS-COORDINATION-20261007-STATE-AND-GAPS.json')
ACTIVE_FLAGS = ('hold_active', 'reservation_active', 'maintenance_reservation_hold', 'private_access_active')


class Stop(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Stop(reason)


def digest(raw):
    return sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_bytes())


def once(path, value):
    """Create only: a crash leaves the intent, not a permission to try again."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], timeout=30)


def tree(repo, ref):
    result = {}
    for record in git(repo, 'ls-tree', '-r', '-z', ref).split(b'\0'):
        if not record:
            continue
        meta, path = record.split(b'\t')
        mode, kind, blob = meta.decode().split()
        require(kind == 'blob' and mode in ('100644', '100755'), 'submodule_or_symlink_requires_coordinated_review')
        result[path.decode('utf-8')] = blob
    return result


def classify(paths):
    """Closed, deliberately small fast lane; unknown code is never auto-approved."""
    if not paths:
        return 'no_change'
    for name in paths:
        if re.search(r'(?i)(auth|login|account|session|cookie|middleware|gateway|workos|ownership|payment|stripe|consent)', name):
            return 'coordinated'
        if (name == 'src/config/aiCompanyProfiles.json'
                or (name.startswith('public/assets/companies/') and Path(name).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.avif'))
                or (name.startswith(('src/components/', 'src/template/')) and name.endswith(('.css', '.scss')))):
            continue
        return 'coordinated'
    return 'routine'


def clean_environment(extra):
    keep = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT',
            'PROGRAMFILES', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOMEDRIVE',
            'HOMEPATH', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    return dict(env, NEXT_TELEMETRY_DISABLED='1', **extra)


def qualify(config, cache):
    repo = Path(config['repository']).resolve()
    commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
    require(commit == config['candidate_commit'], 'checkout_must_match_candidate')
    require(not git(repo, 'status', '--porcelain', '--untracked-files=all').strip(), 'qualification_requires_clean_checkout')
    files = tree(repo, commit)
    rows = []
    require({g['name'] for g in config['checks']} >= {'regression', 'build'}, 'regression_and_build_required')
    for group in config['checks']:
        selected = {p: h for p, h in files.items() if any(fnmatchcase(p, g) for g in group['inputs'])
                    or p in ('package.json', 'yarn.lock', 'package-lock.json', '.yarnrc.yml')}
        require(selected, 'empty_check_inputs')
        # Executable/runtime pins and lockfiles participate in every cache key.
        tools = {str(Path(p).resolve()): digest(Path(p).read_bytes()) for p in config['tool_files']}
        fingerprint = digest(encoded(dict(files=selected, tools=tools, command=group['command'],
            environment=config.get('check_environment', {}), engine=digest(HERE.read_bytes()))))
        original = Path(cache).resolve() / (fingerprint + '.json')
        reused = original.exists()
        if reused:
            receipt = read(original)
            require(receipt['passed'] is True and receipt['input_sha256'] == fingerprint, 'invalid_cached_qualification')
            log = original.with_suffix('.log')
            require(digest(log.read_bytes()) == receipt['log_sha256'], 'qualification_log_changed')
        else:
            original.parent.mkdir(parents=True, exist_ok=True)
            started, tick = now(), time.monotonic()
            log = original.with_suffix('.log')
            with log.open('xb') as stream:
                try:
                    process = subprocess.run(group['command'], cwd=repo,
                        env=clean_environment(config.get('check_environment', {})),
                        stdout=stream, stderr=subprocess.STDOUT, timeout=group['timeout_seconds'], shell=False)
                    passed = process.returncode == 0
                except (subprocess.TimeoutExpired, OSError) as error:
                    stream.write(('\nQualification stopped: '+type(error).__name__+'\n').encode())
                    passed = False
            receipt = dict(started_at=started, completed_at=now(), seconds=time.monotonic()-tick,
                input_sha256=fingerprint, passed=passed, log_sha256=digest(log.read_bytes()))
            if not receipt['passed']:
                failed = original.with_name(original.stem+'.failed-'+str(time.time_ns())+'.json')
                log.rename(failed.with_suffix('.log'))
                once(failed, receipt)
            else:
                once(original, receipt)
            require(receipt['passed'], 'qualification_failed_see_original_log')
        rows.append(dict(name=group['name'], original=str(original), sha256=digest(original.read_bytes()),
                         reused=reused, input_sha256=fingerprint))
    # Detect edits made while a compiler/test was running.
    require(git(repo, 'rev-parse', 'HEAD').decode().strip() == commit
            and not git(repo, 'status', '--porcelain', '--untracked-files=all').strip(), 'source_changed_during_qualification')
    return rows


def prepare(config_path, cache, output):
    config = read(config_path)
    repo = Path(config['repository']).resolve()
    for key in ('candidate_commit', 'accepted_commit'):
        require(re.fullmatch('[a-f0-9]{40}', config[key]), 'full_git_commit_required')
    git(repo, 'merge-base', '--is-ancestor', config['accepted_commit'], config['candidate_commit'])
    paths = git(repo, 'diff', '--name-only', '-z', config['accepted_commit'], config['candidate_commit']).decode().strip('\0').split('\0')
    paths = [p for p in paths if p]
    risk = classify(paths)
    require(risk == 'routine', 'coordinated_change_use_component_procedure: ' + ', '.join(paths))
    if 'src/config/aiCompanyProfiles.json' in paths:
        filename = 'src/config/aiCompanyProfiles.json'
        before = json.loads(git(repo, 'show', config['accepted_commit']+':'+filename))
        after = json.loads(git(repo, 'show', config['candidate_commit']+':'+filename))
        identity = lambda rows: [{k: v for k, v in row.items() if k not in ('description', 'sources')} for row in rows]
        require(identity(before) == identity(after), 'company_identity_or_link_contract_requires_coordinated_review')
    runtime = [p for p in paths if not p.startswith(('tests/', 'docs/', '.github/'))]
    build = next(g for g in config['checks'] if g['name'] == 'build')
    require(all(any(fnmatchcase(p, g) for g in build['inputs']) for p in runtime), 'build_inputs_must_cover_entire_change')
    qualifications = qualify(config, cache)
    plan = dict(version=VERSION, prepared_at=now(), classification=risk, config=config,
        source_tree=git(repo, 'rev-parse', config['candidate_commit']+'^{tree}').decode().strip(),
        changed_files=paths, qualifications=qualifications, engine_sha256=digest(HERE.read_bytes()),
        private_access_required=False, host_maintenance_debit_seconds=0,
        tool_pins={str(Path(p).resolve()): digest(Path(p).read_bytes()) for p in config['tool_files']})
    validate_plan(plan)
    once(output, plan)
    return dict(plan=str(Path(output).resolve()), plan_sha256=digest(Path(output).read_bytes()),
                qualification_reused=[r['name'] for r in qualifications if r['reused']])


def validate_plan(plan):
    require(plan['version'] == VERSION and plan['classification'] == classify(plan['changed_files']) == 'routine', 'routine_plan_required')
    require(plan['engine_sha256'] == digest(HERE.read_bytes()), 'release_engine_changed_reprepare')
    c = plan['config']
    require(re.fullmatch(r'prj_[A-Za-z0-9]+', c['project']) and re.fullmatch(r'team_[A-Za-z0-9]+', c['team']), 'exact_project_and_team_required')
    require(re.fullmatch(r'dpl_[A-Za-z0-9]+', c['accepted_deployment']), 'accepted_deployment_required')
    require(re.fullmatch(r'dpl_[A-Za-z0-9]+', c['candidate_deployment'])
            and c['candidate_deployment'] != c['accepted_deployment'], 'exact_distinct_candidate_deployment_required')
    require(c['candidate_commit'] != c['accepted_commit'], 'distinct_sources_required')
    require(1 <= len(c['aliases']) <= 4 and len(set(c['aliases'])) == len(c['aliases']), 'bounded_unique_aliases_required')
    require(all(re.fullmatch('[a-z0-9.-]+', host) for host in c['aliases']), 'invalid_alias')
    for key in ('probes', 'accepted_probes'):
        require(1 <= len(c[key]) <= 6, 'one_to_six_probes_required')
        for probe in c[key]:
            require(probe['path'].startswith('/') and not probe['path'].startswith('//')
                    and not urlsplit(probe['path']).netloc and '#' not in probe['path'], 'relative_probe_required')
            require(type(probe['status']) is int and probe.get('contains'), 'semantic_probe_required')
    require(480 <= c['release_seconds'] <= 900, 'complete_routine_envelope_480_to_900_seconds')
    require(c['release_seconds'] >= bounds(c)['complete'], 'release_envelope_shorter_than_serial_path')
    require(c['coordination_registry'] == str(LEGACY_REGISTRY), 'canonical_legacy_coordinator_required')
    require(c['coordinator_directory'] == str(HERE.parent.parent / 'outputs' / 'publication-state'), 'canonical_resource_coordinator_required')


def bounds(c):
    aliases = 15 * len(c['aliases'])
    live, accepted = 12 * len(c['probes']), 12 * len(c['accepted_probes'])
    preflight = 15 + aliases + 15 + accepted + live + aliases + 15
    forward = 60 + aliases + 15 + live + aliases
    recovery = aliases + 15 + 15 + 60 + aliases + 15 + accepted
    return dict(preflight=preflight, forward=forward, recovery=recovery,
                complete=preflight+forward+recovery+30)


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Stop('resource_writer_busy') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def require_legacy_clear(path, own_key=None, own_row=None):
    registry = read(path)  # Missing/inaccessible/invalid registry fails closed.
    require(isinstance(registry, dict), 'invalid_legacy_registry')
    for key, row in registry.items():
        if key == own_key:
            require(row == own_row, 'coordination_reservation_changed')
            continue
        if isinstance(row, dict):
            require(not any(row.get(key) is True for key in ACTIVE_FLAGS), 'legacy_resource_hold_requires_custodian_release')
    return registry


def replace_registry(path, registry):
    """Caller holds the legacy writer inode. Only one owned row may change."""
    path = Path(path)
    pending = path.with_name(path.name+'.routine-'+str(time.time_ns())+'.pending')
    once(pending, registry)
    os.replace(pending, path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Vercel:
    def __init__(self, config):
        self.config = config

    def cli(self, *args, timeout=15):
        c = self.config
        process = subprocess.run([*c['vercel_command'], *args, '--scope', c['scope'],
            '--non-interactive', '--no-color'], capture_output=True,
            timeout=timeout, shell=False, env=dict(os.environ, **c.get('vercel_environment', {})))
        require(process.returncode == 0, 'vercel_cli_failed_outcome_requires_observation')
        return process.stdout

    def metadata(self, target):
        require(re.fullmatch('[A-Za-z0-9_.-]+', target), 'invalid_deployment_identifier')
        d = json.loads(self.cli('api', '/v13/deployments/'+target, '--method', 'GET', '--raw'))
        return dict(id=d['id'], project=d['projectId'], team=d['ownerId'], ready=d['readyState'],
                    target=d.get('target'), commit=d.get('meta', {}).get('githubCommitSha'), url=d['url'])

    def promote(self, deployment):
        # Vercel assigns the project's configured production domains together.
        # No per-domain alias updates, environment writes or new cloud builds.
        return digest(self.cli('promote', deployment, '--yes', '--timeout', '45s', timeout=60))

    def promotion(self, target=None):
        p = json.loads(self.cli('api', '/v9/projects/'+self.config['project'], '--method', 'GET', '--raw'))
        require(p['id'] == self.config['project'] and p['accountId'] == self.config['team'], 'promotion_project_identity_changed')
        require(not p.get('rollingRelease'), 'rolling_release_requires_coordinated_procedure')
        job = p.get('lastAliasRequest') or {}
        require(job.get('jobStatus') not in ('pending', 'in-progress'), 'promotion_still_pending_observe_only')
        if target is not None:
            require(job.get('jobStatus') == 'succeeded' and job.get('toDeploymentId') == target
                    and job.get('type') == 'promote', 'promotion_terminal_success_not_observed')

    def smoke(self, host, probes):
        results = []
        # Six bounded subprocesses: <=72s, no drip-fed body can extend the parent.
        for spec in probes:
            process = subprocess.run([sys.executable, '-B', str(HERE), '_probe',
                'https://'+host+spec['path'], json.dumps(spec)], capture_output=True, timeout=12)
            require(process.returncode == 0, 'semantic_smoke_failed')
            results.append(json.loads(process.stdout))
        return results


def probe_response(response, spec):
    body = response.read(2_097_153)
    require(len(body) <= 2_097_152 and response.status == spec['status'], 'probe_status_or_size')
    text = body.decode('utf-8', errors='strict')
    require(all(s in text for s in spec['contains']) and not any(s in text for s in spec.get('absent', [])), 'probe_content')
    require(all(response.getheader(k) == v for k, v in spec.get('headers', {}).items()), 'probe_headers')
    return dict(path=spec['path'], status=response.status, bytes=len(body), sha256=digest(body), observed_at=now())


def probe(url, spec):
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password, 'https_probe_required')
    connection = HTTPSConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        connection.request('GET', parsed.path + ('?'+parsed.query if parsed.query else ''),
            headers={'User-Agent': 'WahojobsRoutineRelease/1', 'Cache-Control': 'no-cache'})
        response = connection.getresponse()  # Deliberately does not follow redirects.
        return probe_response(response, spec)
    finally:
        connection.close()


class Publisher:
    """One durable forward intent and, if required, one owned compatible return."""
    def __init__(self, plan, plan_hash, run, adapter):
        self.plan, self.config, self.plan_hash = plan, plan['config'], plan_hash
        self.run, self.adapter = Path(run).resolve(), adapter
        self.started = time.monotonic()
        self.deadline = self.started + self.config['release_seconds']

    def budget(self, seconds):
        require(time.monotonic()+seconds <= self.deadline, 'whole_remaining_procedure_does_not_fit')

    def event(self, label, value):
        once(self.run/(label+'.json'), dict(at=now(), plan_sha256=self.plan_hash, **value))

    def exact(self, target, commit, identifier=None):
        d = self.adapter.metadata(target)
        require(d['project'] == self.config['project'] and d['team'] == self.config['team']
                and d['commit'] == commit and d['ready'] == 'READY' and d['target'] == 'production'
                and (identifier is None or d['id'] == identifier), 'deployment_identity_or_readiness_changed')
        return d

    def selection(self, candidate):
        current = [self.adapter.metadata(host) for host in self.config['aliases']]
        for d in current:
            require(d['project'] == self.config['project'] and d['team'] == self.config['team'], 'foreign_project_or_team')
        ids = {d['id'] for d in current}
        allowed = {candidate['id'], self.config['accepted_deployment']}
        require(ids <= allowed, 'foreign_successor_do_not_overwrite')
        if len(ids) != 1:
            raise Stop('mixed_alias_outcome_observe_existing_operation_only')
        selected = current[0]['id']
        expected = self.config['candidate_commit'] if selected == candidate['id'] else self.config['accepted_commit']
        require(all(d['commit'] == expected and d['ready'] == 'READY' for d in current), 'alias_source_changed')
        return selected

    def finish(self, state):
        self.event('terminal', dict(state=state, seconds=time.monotonic()-self.started,
                                   private_access=False, host_maintenance_debit_seconds=0))
        return read(self.run/'terminal.json')

    def execute(self, deployment):
        self.run.mkdir(parents=True, exist_ok=True)
        if (self.run/'terminal.json').exists():
            return read(self.run/'terminal.json')
        candidate = self.exact(deployment, self.config['candidate_commit'])
        require(candidate['id'] != self.config['accepted_deployment'], 'distinct_deployments_required')
        existing = self.run/'binding.json'
        binding = dict(plan_sha256=self.plan_hash, deployment=candidate['id'])
        if existing.exists():
            require(read(existing) == binding, 'run_is_bound_to_another_operation')
        else:
            once(existing, binding)
        selected = self.selection(candidate)
        if (self.run/'return-intent.json').exists():
            if selected == self.config['accepted_deployment']:
                self.adapter.promotion(self.config['accepted_deployment'])
                self.adapter.smoke(self.config['aliases'][0], self.config['accepted_probes'])
                return self.finish('recovered')
            raise Stop('return_outcome_unknown_observe_only_never_replay')
        forward = self.run/'forward-intent.json'
        if forward.exists():
            require(selected == candidate['id'], 'forward_outcome_unknown_observe_only_never_replay')
        else:
            b = bounds(self.config)
            self.budget(b['complete']-15-15*len(self.config['aliases']))
            require(selected == self.config['accepted_deployment'], 'accepted_version_changed')
            self.exact(self.config['accepted_deployment'], self.config['accepted_commit'], self.config['accepted_deployment'])
            self.adapter.smoke(self.config['aliases'][0], self.config['accepted_probes'])
            self.adapter.smoke(candidate['url'], self.config['probes'])
            # Reobserve after potentially slow checks, immediately before intent.
            require(self.selection(candidate) == self.config['accepted_deployment'], 'accepted_version_changed')
            self.adapter.promotion()
            self.budget(b['forward']+b['recovery']+30)
            self.event('forward-intent', dict(candidate=candidate['id'], accepted=self.config['accepted_deployment']))
            try:
                output_hash = self.adapter.promote(candidate['id'])
                self.event('forward-result', dict(output_sha256=output_hash))
            except Exception:
                # A timeout/nonzero CLI is not proof that the cloud did nothing.
                self.event('forward-uncertain', dict(outcome='observe_aliases'))
            selected = self.selection(candidate)
            require(selected == candidate['id'], 'forward_outcome_unknown_observe_only_never_replay')
        # Alias convergence alone does not prove that the provider's asynchronous
        # promotion has finished. Keep the hold until its exact job is terminal.
        self.adapter.promotion(candidate['id'])
        try:
            b = bounds(self.config)
            self.budget(12*len(self.config['probes'])+15*len(self.config['aliases'])+b['recovery']+30)
            checks = self.adapter.smoke(self.config['aliases'][0], self.config['probes'])
        except Exception:
            self.budget(bounds(self.config)['recovery']+15)
            require(self.selection(candidate) == candidate['id'], 'candidate_no_longer_owned')
            self.adapter.promotion(candidate['id'])
            self.exact(self.config['accepted_deployment'], self.config['accepted_commit'], self.config['accepted_deployment'])
            self.event('return-intent', dict(deployment=self.config['accepted_deployment']))
            try:
                output_hash = self.adapter.promote(self.config['accepted_deployment'])
                self.event('return-result', dict(output_sha256=output_hash))
            except Exception:
                self.event('return-uncertain', dict(outcome='observe_aliases'))
            require(self.selection(candidate) == self.config['accepted_deployment'], 'return_outcome_unknown_observe_only_never_replay')
            self.adapter.promotion(self.config['accepted_deployment'])
            self.adapter.smoke(self.config['aliases'][0], self.config['accepted_probes'])
            return self.finish('recovered')
        require(self.selection(candidate) == candidate['id'], 'successor_changed_during_smoke')
        self.event('live-checks-'+str(time.time_ns()), dict(checks=checks))
        return self.finish('accepted')


def publish(plan_path, deployment, run, approved_plan, adapter=None):
    # Compatibility entry point only. Historical receipts stay readable; after
    # handoff all new production effects go through the common coordinator.
    transition=HERE.parents[1]/'outputs/publication-state/transition.json'
    if transition.exists():
        require(read(transition)['state'] != 'active', 'legacy_publisher_retired_use_scripts_publication_py')
    raw = Path(plan_path).read_bytes()
    require(digest(raw) == approved_plan, 'exact_owner_approved_plan_hash_required')
    plan = json.loads(raw)
    validate_plan(plan)
    c = plan['config']
    require(deployment == c['candidate_deployment'], 'deployment_not_bound_to_approved_plan')
    # Every resume verifies immutable qualifications, without rerunning them.
    for row in plan['qualifications']:
        require(digest(Path(row['original']).read_bytes()) == row['sha256']
                and read(row['original'])['passed'], 'qualification_changed')
        require(digest(Path(row['original']).with_suffix('.log').read_bytes())
                == read(row['original'])['log_sha256'], 'qualification_log_changed')
    require(all(digest(Path(p).read_bytes()) == h for p, h in plan['tool_pins'].items()), 'qualified_toolchain_changed')
    root = Path(c['coordinator_directory'])
    resource = root/(c['team']+'-'+c['project'])
    # Same inode as all old writers. We never alter their registry or accounting.
    legacy = Path(c['coordination_registry'])
    with file_lock(legacy.with_name(legacy.name+'.writer.lock')):
        with file_lock(resource.with_suffix('.lock')):
            run = Path(run).resolve()
            owner = dict(plan_sha256=approved_plan, run=str(run), deployment=deployment,
                resource=c['team']+'/'+c['project'], hold_active=True, exclusive=True)
            key = 'routine_'+c['team']+'_'+c['project']
            registry = require_legacy_clear(legacy, key, owner)
            if (run/'terminal.json').exists():
                require(read(run/'binding.json') == dict(plan_sha256=approved_plan, deployment=deployment), 'terminal_operation_mismatch')
                if key in registry:
                    del registry[key]; replace_registry(legacy, registry)
                return read(run/'terminal.json')
            if key not in registry:
                require(not (run/'registry-bind-intent.json').exists(), 'reservation_outcome_unknown_reconcile_original_no_rebind')
                once(run/'registry-bind-intent.json', dict(row=owner, before_sha256=digest(legacy.read_bytes())))
                registry[key] = owner
                replace_registry(legacy, registry)
            try:
                result = Publisher(plan, approved_plan, run, adapter or Vercel(c)).execute(deployment)
            except BaseException as error:
                # Known failures before any mutation need no durable reservation.
                if not (run/'forward-intent.json').exists():
                    if not (run/'preflight-stopped.json').exists():
                        once(run/'preflight-stopped.json', dict(at=now(), state='unchanged',
                             no_forward_intent=True, error_class=type(error).__name__, plan_sha256=approved_plan))
                    registry = require_legacy_clear(legacy, key, owner)
                    del registry[key]; replace_registry(legacy, registry)
                raise
            registry = require_legacy_clear(legacy, key, owner)
            del registry[key]; replace_registry(legacy, registry)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('classify'); p.add_argument('repository'); p.add_argument('base'); p.add_argument('candidate')
    p = sub.add_parser('prepare'); p.add_argument('config'); p.add_argument('--cache', required=True); p.add_argument('--out', required=True)
    p = sub.add_parser('publish'); p.add_argument('plan'); p.add_argument('--deployment', required=True); p.add_argument('--run', required=True); p.add_argument('--approved-plan', required=True)
    p = sub.add_parser('_probe'); p.add_argument('url'); p.add_argument('spec')
    args = parser.parse_args()
    if args.action == 'classify':
        paths = git(args.repository, 'diff', '--name-only', args.base, args.candidate).decode().splitlines()
        return dict(classification=classify(paths), files=paths)
    if args.action == 'prepare':
        return prepare(args.config, args.cache, args.out)
    if args.action == '_probe':
        return probe(args.url, json.loads(args.spec))
    return publish(args.plan, args.deployment, args.run, args.approved_plan)


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as error:
        # Do not print provider output, environment or credential-bearing errors.
        print(json.dumps(dict(stopped=True, reason=str(error) if isinstance(error, Stop) else type(error).__name__)))
        raise SystemExit(1)
