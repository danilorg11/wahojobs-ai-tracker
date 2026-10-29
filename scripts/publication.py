"""One publication protocol for websites, services, data, providers and infrastructure.

Preparation has no production effects. Workers expose observe/apply/check/recover;
the coordinator records intent before dispatch and never repeats an uncertain
dispatch. A coordinated change is a dependency graph, not a rejected class.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from fnmatch import fnmatchcase
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import routine_release as legacy

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT/'outputs/publication-state'
FORMAT = 'wahojobs-publication-v2'
QUALIFICATION_VERSION = 1
LOADED_ENGINE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
FAMILIES = {
    'content': {'source', 'render', 'recovery'},
    'frontend': {'source', 'build', 'contract', 'recovery'},
    'backend': {'source', 'contract', 'startup', 'preservation', 'recovery'},
    'data': {'source', 'data-dry-run', 'preservation', 'resume', 'recovery'},
    'integration': {'source', 'provider-contract', 'preservation', 'resume', 'recovery'},
    'infrastructure': {'source', 'configuration', 'startup', 'recovery'},
    'accounts': {'source', 'ownership', 'session', 'recovery'},
}
ACTIVE = ('reserved', 'intent', 'running', 'unknown', 'applied', 'recovering')

Stop, require, encoded, digest, read, now, once = (legacy.Stop, legacy.require,
    legacy.encoded, legacy.digest, legacy.read, legacy.now, legacy.once)


def conflicts(left, right):
    """An infrastructure claim on a host conflicts with its service children."""
    return left == right or left.startswith(right.rstrip('/')+'/') or right.startswith(left.rstrip('/')+'/')


class Coordinator:
    def __init__(self, directory=STATE, *, legacy_registry=legacy.LEGACY_REGISTRY, scoped=False):
        self.directory, self.legacy_registry, self.scoped = Path(directory), Path(legacy_registry), scoped
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory/'protocol.sqlite3'
        with self.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, signature TEXT NOT NULL, plan TEXT NOT NULL,
                step TEXT NOT NULL, state TEXT NOT NULL, resources TEXT NOT NULL,
                owner TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, operation TEXT NOT NULL, at TEXT NOT NULL,
                kind TEXT NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS completions (
                signature TEXT PRIMARY KEY, operation TEXT NOT NULL, receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accepted_effects (
                effect TEXT PRIMARY KEY, operation TEXT NOT NULL, signature TEXT NOT NULL,
                receipt TEXT NOT NULL, source_step TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def row(self, operation):
        with self.connection() as db:
            value = db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone()
            return dict(value) if value else None

    def event(self, operation, kind, value, *, state=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO events(operation,at,kind,value) VALUES (?,?,?,?)',
                       (operation, now(), kind, json.dumps(value, sort_keys=True)))
            if state:
                db.execute('UPDATE operations SET state=?,updated=? WHERE id=?', (state, now(), operation))

    def events(self, operation):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM events WHERE operation=? ORDER BY sequence', (operation,))]

    @contextmanager
    def claim(self, operation, signature, plan, step, resources):
        # Only one driver of an operation; this does not block disjoint operations.
        with legacy.file_lock(self.directory/'drivers'/(operation+'.lock')):
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT * FROM operations WHERE id=?', (operation,)).fetchone()
                if existing:
                    require(existing['signature'] == signature and existing['plan'] == plan
                            and json.loads(existing['resources']) == resources, 'operation_binding_changed')
                if not existing or existing['state'] not in ('accepted', 'recovered'):
                    for other in db.execute('SELECT * FROM operations WHERE state IN ('+','.join('?'*len(ACTIVE))+')', ACTIVE):
                        if other['id'] == operation:
                            continue
                        require(not any(conflicts(a, b) for a in resources for b in json.loads(other['resources'])),
                                'shared_resource_busy:'+other['id'])
                if not existing:
                    db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)',
                               (operation, signature, plan, step, 'reserved', json.dumps(resources), str(os.getpid()), now()))
            yield

    def legacy_enter(self, operation, signature, resources):
        if self.scoped:
            return
        # Bridge makes an unresolved new operation visible to the old publishers.
        # The common old lock protects only this short registry transaction.
        path = self.legacy_registry
        key = 'publication_v2_'+operation
        value = dict(hold_active=True, exclusive=True, operation=operation, signature=signature, resources=resources)
        with legacy.file_lock(path.with_name(path.name+'.writer.lock')):
            rows = legacy.require_legacy_clear(path, key, value)
            if key not in rows:
                rows[key] = value
                legacy.replace_registry(path, rows)

    def legacy_exit(self, operation):
        if self.scoped:
            return
        path = self.legacy_registry
        with legacy.file_lock(path.with_name(path.name+'.writer.lock')):
            rows = read(path)
            key = 'publication_v2_'+operation
            require(key in rows and rows[key].get('operation') == operation, 'legacy_reservation_ownership_lost')
            del rows[key]
            legacy.replace_registry(path, rows)

    def complete(self, operation, signature, state, receipt):
        # Completion and its reuse index commit together. A process exit cannot
        # leave accepted work invisible to a later qualified plan.
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO events(operation,at,kind,value) VALUES (?,?,?,?)',
                (operation,now(),'terminal',json.dumps(receipt,sort_keys=True)))
            db.execute('UPDATE operations SET state=?,updated=? WHERE id=?',(state,now(),operation))
            if state=='accepted':
                db.execute('INSERT OR REPLACE INTO completions VALUES (?,?,?)',
                           (signature, operation, json.dumps(receipt, sort_keys=True)))

    def completion(self, signature):
        with self.connection() as db:
            row = db.execute('SELECT * FROM completions WHERE signature=?', (signature,)).fetchone()
            return dict(row) if row else None

    def register_effects(self, plan_path, expected_hash):
        raw=Path(plan_path).read_bytes();require(digest(raw)==expected_hash,'reuse_plan_changed')
        plan=json.loads(raw)
        for step in plan['steps']:
            original=self.completion(step['signature'])
            if original and json.loads(original['receipt'])['plan_sha256']==expected_hash:
                with self.connection() as db:
                    db.execute('INSERT OR REPLACE INTO accepted_effects VALUES (?,?,?,?,?)',
                        (effect_signature(step),original['operation'],step['signature'],original['receipt'],json.dumps(step,sort_keys=True)))

    def accepted_effect(self, step):
        with self.connection() as db:
            row=db.execute('SELECT * FROM accepted_effects WHERE effect=?',(effect_signature(step),)).fetchone()
            return dict(row) if row else None


def run_check(check, cache):
    """Cache immutable inputs, independently of scheduling and publication code."""
    files = {str(Path(p).resolve()): digest(Path(p).read_bytes()) for p in check['files']}
    tools = {str(Path(p).resolve()): digest(Path(p).read_bytes()) for p in check['tools']}
    key = digest(encoded(dict(version=QUALIFICATION_VERSION, files=files, tools=tools,
        command=check['command'], cwd=str(Path(check['cwd']).resolve()), environment=check.get('environment', {}))))
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    original = cache/(key+'.json')
    with legacy.file_lock(cache/(key+'.lock')):
        if original.exists():
            receipt = read(original)
            require(receipt['passed'] and receipt['key'] == key, 'qualification_not_successful')
            require(digest(Path(receipt['log']).read_bytes()) == receipt['log_sha256'], 'qualification_log_changed')
            require(all(digest(Path(p).read_bytes()) == h for p, h in receipt.get('outputs', {}).items()), 'qualified_artifact_changed')
            verify_output_manifests(receipt)
            return dict(id=check['id'], categories=check['categories'], original=str(original),
                        sha256=digest(original.read_bytes()), reused=True)
        attempt = cache/(key+'-'+str(time.time_ns()))
        log = attempt.with_suffix('.log')
        tick, started = time.monotonic(), now()
        passed = False
        with log.open('xb') as stream:
            try:
                result = subprocess.run(check['command'], cwd=check['cwd'],
                    env=legacy.clean_environment(check.get('environment', {})),
                    stdout=stream, stderr=subprocess.STDOUT, shell=False, timeout=check['seconds'])
                passed = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired) as error:
                stream.write(('\n'+type(error).__name__+'\n').encode())
        # Do not accept a source edited while the check was running.
        require(all(digest(Path(p).read_bytes()) == h for p, h in files.items()), 'qualification_input_changed_during_check')
        receipt = dict(key=key, passed=passed, started_at=started, completed_at=now(),
            seconds=time.monotonic()-tick, files=files, tools=tools, command=check['command'],
            log=str(log.resolve()), log_sha256=digest(log.read_bytes()),
            outputs={str(Path(p).resolve()): digest(Path(p).read_bytes()) for p in check.get('outputs', [])} if passed else {})
        receipt['output_manifests'] = check.get('output_manifests', [])
        if passed:
            verify_output_manifests(receipt)
        once(original if passed else attempt.with_suffix('.failed.json'), receipt)
        require(passed, 'qualification_failed:'+str(log))
        return dict(id=check['id'], categories=check['categories'], original=str(original),
                    sha256=digest(original.read_bytes()), reused=False)


def verify_output_manifests(receipt):
    for manifest in receipt.get('output_manifests', []):
        require(str(Path(manifest).resolve()) in receipt['outputs'], 'unbound_artifact_manifest')
        require(all(digest(Path(path).read_bytes()) == value for path, value in read(manifest).items()), 'artifact_manifest_content_changed')


def step_signature(step):
    # A changed dependency's exact signature is incorporated by prepare().
    return digest(encoded(step))


def effect_signature(step):
    """Intent identity is separate from its qualified procedure/check revisions.

    This permits a fixed adapter or added test to re-check an already accepted
    effect, without publishing it again. Arbitrary domain adapters conservatively
    bind their whole payload unless they supply a reviewed explicit effect.
    """
    payload=step['worker']['payload'];kind=payload['kind']
    if kind=='files':
        effect={key:payload.get(key) for key in ('environment','kind','targets','files')}
    elif kind=='vercel':
        c=payload['configuration']
        effect={key:c[key] for key in ('project','team','aliases','accepted_deployment','accepted_commit','candidate_deployment','candidate_commit')}
    elif kind=='sqlite':
        effect={key:payload.get(key) for key in ('environment','kind','database','changes','allowed_tables')}
    elif (payload.get('domain') in ('beta-batch','retained-content','company-assets')
          and Path(payload.get('module','')).name in ('publication_beta.py','publication_domains.py','publication_s3.py')):
        # These built-in domains describe their actual targets/inputs in the
        # payload. Controller fixes are a new qualified procedure, not a reason
        # to repeat an already accepted code/data/provider mutation.
        procedure={'module','module_sha256','state_directory','hooks','phase_floor_seconds','locks','native_gate'}
        def intent(value):
            if isinstance(value,dict):return {k:intent(v) for k,v in value.items() if k not in procedure}
            if isinstance(value,list):return [intent(v) for v in value]
            return value
        effect=step.get('qualified_effect',intent(payload))
    else:
        effect=step.get('qualified_effect',payload)
    return digest(encoded(dict(id=step['id'],family=step['family'],resources=step['resources'],
        effects=step.get('effects'),acceptance_contract=step.get('human_acceptance'),
        compatibility=step['compatibility'],effect=effect)))


def validate(config):
    require(config['format'] == FORMAT and config['environment'] in ('local', 'production'), 'unsupported_publication_plan')
    require(config.get('steps') and len({s['id'] for s in config['steps']}) == len(config['steps']), 'unique_nonempty_steps_required')
    previous = set()
    checks = {c['id']: c for c in config['checks']}
    require(len(checks) == len(config['checks']), 'duplicate_check')
    for step in config['steps']:
        require(step['family'] in FAMILIES, 'unknown_change_family')
        require(set(step['needs']) <= previous, 'steps_must_be_in_dependency_order')
        require(step['resources'] and all(r.startswith('/') and '..' not in r.split('/') for r in step['resources']), 'canonical_resources_required')
        require(len(set(step['resources'])) == len(step['resources']), 'duplicate_resource')
        categories = set().union(*(set(checks[c]['categories']) for c in step['checks']))
        require(FAMILIES[step['family']] <= categories, 'missing_risk_checks:'+step['id'])
        if step.get('effects', {}).get('provider_login'):
            require('real-provider-acceptance' in categories or step.get('human_acceptance'), 'provider_acceptance_path_required')
        human=step.get('human_acceptance')
        if human and human.get('mode','before_apply')=='inside_finite_domain':
            require(step['worker']['payload']['kind']=='module' and 'finite-exit' in categories
                and step['worker']['payload'].get('finite_account_exit') is True,
                'qualified_independent_finite_account_exit_required')
        elif human:
            require(human.get('mode','before_apply')=='before_apply','unknown_acceptance_mode')
        for label in ('observe', 'apply', 'check', 'recover', 'cleanup'):
            require(type(step['seconds'][label]) is int and step['seconds'][label] > 0, 'positive_phase_bound_required')
        require(step.get('recovery_reason') and step.get('duration_reason'), 'concrete_duration_and_recovery_basis_required')
        require(step['worker']['command'] and step['worker']['files'], 'pinned_executable_worker_required')
        require(step['worker']['payload']['environment'] == config['environment'], 'worker_environment_mismatch')
        require(sorted(step['worker']['payload']['resources']) == sorted(step['resources']), 'worker_resources_not_bound')
        require(step['compatibility'] == 'standalone', 'coupled_phases_must_use_one_transactional_domain_step')
        require(step['access']['kind'] in ('none', 'provider-existing', 'private-host'), 'access_path_required')
        for action, floor in phase_floors(step).items():
            require(step['seconds'][action] >= floor, 'enclosing_deadline_shorter_than_worker_path:'+step['id']+':'+action)
        require(not step.get('effects', {}).get('database_restore'), 'snapshot_restore_is_not_publication_recovery')
        if config['environment'] == 'local':
            require(step['worker']['payload']['environment'] == 'local'
                    and all(Path(p).resolve().is_relative_to(ROOT/'outputs') for p in step['worker']['payload']['targets']), 'local_target_outside_workspace')
        previous.add(step['id'])


def phase_floors(step):
    p = step['worker']['payload']
    if p['kind'] == 'vercel':
        c = p['configuration']; aliases = len(c['aliases'])*15
        live, accepted = len(c['probes'])*12, len(c['accepted_probes'])*12
        return dict(observe=aliases+20, apply=aliases+15+live+65,
                    check=aliases+20+live, recover=aliases*2+110+accepted, cleanup=aliases+20)
    if p['kind'] == 'files':
        hooks = p.get('hooks', {})
        total = lambda *names: sum(h['seconds'] for n in names for h in hooks.get(n, []))
        io = p.get('mutation_io_seconds', 5)
        return dict(observe=io, apply=total('preflight', 'stop', 'start')+io+5,
                    check=total('check')+5, recover=total('stop', 'start', 'recovered_check')+io+5,
                    cleanup=total('cleanup')+5)
    # Domain contracts bind their enclosing phase floors to their reviewed source.
    floors=dict(p['phase_floor_seconds'])
    if p['kind']=='module':
        hooks=p.get('hooks',{})
        total=lambda *names:sum(h['seconds'] for name in names for h in hooks.get(name,[]))
        floors['apply']+=total('preflight','stop','start')
        floors['recover']+=total('preflight','stop','start','recovered_check')
        floors['check']+=total('check')
        floors['cleanup']+=total('cleanup')
    return floors


def prepare(config_path, output, cache=STATE/'qualifications'):
    require(digest(Path(__file__).read_bytes())==LOADED_ENGINE_SHA256,'restart_changed_publication_engine_preserve_receipts')
    config = read(config_path)
    validate(config)
    qualifications = [run_check(check, cache) for check in config['checks']]
    require(digest(Path(__file__).read_bytes())==LOADED_ENGINE_SHA256,'restart_changed_publication_engine_preserve_receipts')
    signatures = {}
    steps = []
    for item in config['steps']:
        step = json.loads(json.dumps(item))
        step['dependency_signatures'] = {name: signatures[name] for name in step['needs']}
        # Bind the actual qualification inputs, rather than their display labels.
        step['qualification_keys'] = {q['id']: read(q['original'])['key']
                                      for q in qualifications if q['id'] in step['checks']}
        step['worker_pins'] = {str(Path(p).resolve()): digest(Path(p).read_bytes()) for p in step['worker']['files']}
        step['signature'] = step_signature(step)
        signatures[step['id']] = step['signature']
        steps.append(step)
    plan = dict(format=FORMAT, environment=config['environment'], title=config['title'],
        prepared_at=now(), steps=steps, qualifications=qualifications,
        engine_sha256=digest(Path(__file__).read_bytes()), coordinator=str(Path(config.get('coordinator',STATE)).resolve()),
        accounting=config.get('accounting'), transition=config.get('transition'),
        reuse_plans=[dict(path=str(Path(path).resolve()),sha256=digest(Path(path).read_bytes())) for path in config.get('reuse_plans',[])],
        sources=config.get('sources', []))
    once(output, plan)
    return dict(plan=str(Path(output).resolve()), sha256=digest(Path(output).read_bytes()),
        reused_checks=[q['id'] for q in qualifications if q['reused']],
        serial_upper_seconds=sum(envelope(s) for s in steps))


def envelope(step):
    s = step['seconds']
    # initial observation, dispatch, reconcile, check, possible recovery + its
    # reconcile/check, cleanup; 10% + 5s supervisor contingency, no daily window.
    path = s['observe']*3 + s['apply'] + s['check']*2 + s['recover'] + s['cleanup']
    return path + max(5, (path+9)//10)


class Worker:
    def __init__(self, step, operation, plan_hash):
        self.step, self.operation, self.plan_hash = step, operation, plan_hash

    def call(self, action):
        require(all(digest(Path(p).read_bytes()) == h for p, h in self.step['worker_pins'].items()), 'worker_changed_after_preparation')
        request = dict(protocol=FORMAT, action=action, operation=self.operation, plan_sha256=self.plan_hash,
                       signature=self.step['signature'], payload=self.step['worker']['payload'])
        seconds = self.step['seconds'].get(action, self.step['seconds']['observe'])
        request['phase_budget_seconds'] = seconds
        if self.step.get('adoption'):request['adoption']=self.step['adoption']
        result = subprocess.run(self.step['worker']['command'], input=encoded(request),
            cwd=self.step['worker'].get('cwd', str(ROOT)), capture_output=True, shell=False,
            timeout=seconds, env=dict(os.environ, **self.step['worker'].get('environment', {})))
        require(len(result.stdout) <= 262144 and result.returncode == 0, 'worker_response_failed_or_unknown')
        value = json.loads(result.stdout)
        require(value['operation'] == self.operation and value['signature'] == self.step['signature'], 'worker_response_not_bound')
        require(value['state'] in ('before', 'running', 'applied', 'accepted', 'recovered', 'failed', 'foreign', 'unknown'), 'invalid_worker_state')
        return value


def verify_qualifications(plan):
    for q in plan['qualifications']:
        original = Path(q['original']); raw = original.read_bytes()
        require(digest(raw) == q['sha256'], 'qualification_receipt_changed')
        receipt = json.loads(raw)
        require(receipt['passed'] and digest(Path(receipt['log']).read_bytes()) == receipt['log_sha256'], 'qualification_invalid')
        verify_output_manifests(receipt)
        for mapping in ('files', 'tools', 'outputs'):
            require(all(digest(Path(p).read_bytes()) == h for p, h in receipt[mapping].items()), 'qualification_inputs_or_artifacts_changed')


def native_binding(receipt, step):
    if receipt.get('worker_binding'):return receipt['worker_binding']
    while receipt.get('reused_original'):receipt=receipt['reused_original']
    return dict(operation=receipt['operation'],plan_sha256=receipt['plan_sha256'],
        signature=receipt.get('worker',{}).get('signature',step['signature']),
        state_directory=step['worker']['payload']['state_directory'])


def observing_worker(step,receipt,plan_hash,worker_factory):
    binding=native_binding(receipt,step);old_step=json.loads(json.dumps(step))
    old_step['signature']=binding['signature'];old_step['worker']['payload']['state_directory']=binding['state_directory']
    old_step['adoption']=dict(current_plan_sha256=plan_hash,authorized_signature=step['signature'],
        original_operation=binding['operation'],original_signature=binding['signature'],read_only=True)
    return worker_factory(old_step,binding['operation'],binding['plan_sha256'])


def verify_acceptance(step):
    if step.get('human_acceptance'):
        actual=read(step['human_acceptance']['receipt'])
        require(actual.get('accepted') is True and actual.get('human_reference')
            and (actual.get('signature')==step['signature'] or actual.get('effect_signature')==effect_signature(step)),
            'genuine_required_acceptance_not_observed')


def drive(plan, plan_hash, step, coordinator, *, worker_factory=Worker):
    operation = digest(encoded([plan_hash, step['id']]))[:32]
    signature, resources = step['signature'], step['resources']
    worker = worker_factory(step, operation, plan_hash)
    deadline = time.monotonic()+envelope(step)
    with coordinator.claim(operation, signature, plan_hash, step['id'], resources):
        current = coordinator.row(operation)
        if current['state'] in ('accepted', 'recovered'):
            terminal=json.loads(next(e['value'] for e in reversed(coordinator.events(operation)) if e['kind']=='terminal'))
            worker=observing_worker(step,terminal,plan_hash,worker_factory)
            observed = worker.call('observe')
            require(observed['state'] == current['state'], 'completed_operation_target_changed')
            if current['state'] == 'accepted':
                require(worker.call('check')['state'] == 'accepted', 'completed_operation_no_longer_accepted')
                verify_acceptance(step)
            # A crash may happen after recording completion but before removing
            # our bridge row. Reconcile only that exact owned row.
            if not coordinator.scoped:
                with legacy.file_lock(coordinator.legacy_registry.with_name(coordinator.legacy_registry.name+'.writer.lock')):
                    rows = read(coordinator.legacy_registry)
                    key = 'publication_v2_'+operation
                    if key in rows:
                        require(rows[key].get('signature') == signature, 'terminal_bridge_ownership_changed')
                        del rows[key]; legacy.replace_registry(coordinator.legacy_registry, rows)
            return dict(step=step['id'], operation=operation, state=current['state'], reused_operation=True)
        # Do not hold a legacy/global reservation during qualification or waits
        # for another resource. Unknown prior outcomes retain their own row.
        entered = False
        try:
            coordinator.legacy_enter(operation, signature, resources)
            entered = True
            prior = coordinator.completion(signature) or coordinator.accepted_effect(step)
            if prior and prior['operation'] != operation:
                original = json.loads(prior['receipt'])
                old_worker=observing_worker(step,original,plan_hash,worker_factory)
                actual = old_worker.call('observe')
                require(actual['state'] == 'accepted' and old_worker.call('check')['state'] == 'accepted', 'previously_accepted_step_changed')
                verify_acceptance(step)
                receipt = dict(step=step['id'], operation=operation, state='accepted', at=now(),
                    plan_sha256=plan_hash, reused_original=json.loads(prior['receipt']), fresh_check=actual,
                    worker_binding=native_binding(original,step))
                coordinator.complete(operation, signature, 'accepted', receipt)
                coordinator.legacy_exit(operation)
                return receipt
            observed = worker.call('observe')
            events = coordinator.events(operation)
            returning = any(e['kind'] == 'recover-intent' for e in events)
            dispatched = any(e['kind'] == 'apply-intent' for e in events)
            if observed['state'] == 'foreign':
                raise Stop('foreign_successor_or_identity_changed')
            if observed['state'] in ('running', 'unknown'):
                coordinator.event(operation, 'observed', observed, state='unknown')
                return dict(step=step['id'], operation=operation, state='unknown')
            if returning:
                require(observed['state'] == 'recovered', 'recovery_outcome_unknown_never_replay')
            elif not dispatched:
                require(observed['state'] == 'before', 'unowned_candidate_or_prior_effect')
                require(time.monotonic()+envelope(step)-step['seconds']['observe'] <= deadline, 'whole_remaining_procedure_does_not_fit')
                coordinator.event(operation, 'apply-intent', observed, state='intent')
                try:
                    coordinator.event(operation, 'apply-response', worker.call('apply'), state='running')
                except Exception as error:
                    coordinator.event(operation, 'apply-response-missing', dict(error_class=type(error).__name__), state='unknown')
                observed = worker.call('observe')
            elif observed['state'] == 'before':
                raise Stop('dispatch_outcome_unknown_never_replay')
            if observed['state'] == 'before':
                raise Stop('dispatch_outcome_unknown_never_replay')
            if observed['state'] in ('running', 'unknown'):
                coordinator.event(operation, 'pending', observed, state='unknown')
                return dict(step=step['id'], operation=operation, state='unknown')
            if observed['state'] in ('applied', 'accepted') or observed['state'] == 'failed' and observed.get('recovery_required') is not False:
                try:
                    require(observed['state'] != 'failed', 'owned_partial_apply_requires_return')
                    checked = worker.call('check')
                    require(checked['state'] == 'accepted', 'candidate_check_not_accepted')
                    verify_acceptance(step)
                    observed = checked
                except Exception as error:
                    require(time.monotonic()+step['seconds']['recover']+step['seconds']['observe']+step['seconds']['check']+step['seconds']['cleanup'] <= deadline,
                            'full_recovery_no_longer_fits_keep_reservation')
                    # Observe ownership again; an error is never reason to undo a successor.
                    own = worker.call('observe')
                    require(own['state'] in ('applied', 'accepted', 'failed'), 'candidate_no_longer_owned')
                    coordinator.event(operation, 'recover-intent', dict(reason=type(error).__name__), state='recovering')
                    try:
                        coordinator.event(operation, 'recover-response', worker.call('recover'))
                    except Exception as error:
                        coordinator.event(operation, 'recover-response-missing', dict(error_class=type(error).__name__), state='unknown')
                    observed = worker.call('observe')
            elif observed['state'] == 'failed' and not returning:
                # A worker may have completed its own qualified compensation.
                require(observed.get('recovery_required') is False, 'known_failure_requires_reviewed_recovery')
                observed['state'] = 'recovered'
            require(observed['state'] in ('accepted', 'recovered'), 'operation_not_terminal_keep_reservation')
            cleanup = worker.call('cleanup')
            require(cleanup['state'] == observed['state'], 'cleanup_not_terminal_keep_reservation')
            receipt = dict(step=step['id'], operation=operation, state=observed['state'], at=now(),
                           plan_sha256=plan_hash, worker=observed, cleanup=cleanup,
                           worker_binding=dict(operation=operation,plan_sha256=plan_hash,signature=signature,
                               state_directory=step['worker']['payload']['state_directory']))
            coordinator.complete(operation, signature, observed['state'], receipt)
            coordinator.legacy_exit(operation)
            return receipt
        except BaseException:
            # Only a positively observed unchanged target, with no apply intent,
            # permits a known preflight exit. Never erase an uncertain hold.
            if not any(e['kind'] == 'apply-intent' for e in coordinator.events(operation)):
                coordinator.event(operation, 'preflight-stopped', dict(no_dispatch=True), state='unchanged')
                if entered:
                    coordinator.legacy_exit(operation)
            raise


def execute(plan_path, approved_hash=None, *, local=False, coordinator=None, worker_factory=Worker):
    raw = Path(plan_path).read_bytes(); plan = json.loads(raw); plan_hash = digest(raw)
    require(plan['format'] == FORMAT and plan['engine_sha256'] == LOADED_ENGINE_SHA256
        and LOADED_ENGINE_SHA256 == digest(Path(__file__).read_bytes()), 'prepared_engine_changed')
    require(local == (plan['environment'] == 'local'), 'environment_boundary_mismatch')
    control_state=Path(plan.get('coordinator',STATE))
    if not local:
        require(approved_hash == plan_hash, 'exact_final_production_approval_required')
        require(read(control_state/'transition.json')['state'] == 'active', 'publisher_transition_not_activated')
    verify_qualifications(plan)
    coordinator = coordinator or Coordinator(control_state,scoped=local or read(control_state/'transition.json')['mode'] == 'scoped')
    for reference in plan.get('reuse_plans',[]):coordinator.register_effects(reference['path'],reference['sha256'])
    coordinator.register_effects(plan_path,plan_hash)
    states, results = {}, []
    for step in plan['steps']:
        if not all(states.get(dep) == 'accepted' for dep in step['needs']):
            results.append(dict(step=step['id'], state='waiting_for_dependency'))
            continue
        try:
            if not local:
                authorize(plan, plan_hash, step, states)
            result = drive(plan, plan_hash, step, coordinator, worker_factory=worker_factory)
        except Exception as error:
            result = dict(step=step['id'], state='attention', reason=str(error) if isinstance(error, Stop) else type(error).__name__)
        states[step['id']] = result['state']; results.append(result)
    coordinator.register_effects(plan_path,plan_hash)
    return dict(plan_sha256=plan_hash, results=results, complete=all(s == 'accepted' for s in states.values()) and len(states) == len(plan['steps']))


def remaining_procedure(plan,step,states):
    """Time admission follows real dependencies/resources, not unrelated queues."""
    pending=[s for s in plan['steps'] if states.get(s['id'])!='accepted']
    selected={step['id']}
    while True:
        previous=set(selected)
        for other in pending:
            for current in pending:
                if current['id'] not in selected:continue
                if (other['id'] in current['needs'] or current['id'] in other['needs']
                    or any(conflicts(a,b) for a in current['resources'] for b in other['resources'])):
                    selected.add(other['id'])
        if selected==previous:break
    return sum(envelope(s) for s in pending if s['id'] in selected)


def authorize(plan, plan_hash, step, states):
    grant = read(os.environ['WAHOJOBS_PUBLICATION_AUTHORITY'])
    require(grant['status'] == 'granted' and grant['plan_sha256'] == plan_hash
            and grant['human_decision_reference'], 'concrete_owner_grant_required')
    require(set(step['resources']) <= set(grant['resources']) and step['signature'] in grant['signatures'], 'owner_grant_scope_mismatch')
    current = datetime.now(timezone.utc)
    start, end = (datetime.fromisoformat(grant[k]) for k in ('not_before', 'expires_at'))
    require(start <= current < end, 'owner_grant_not_current')
    remaining = remaining_procedure(plan,step,states)
    require((end-current).total_seconds() >= remaining, 'whole_remaining_publication_and_recovery_do_not_fit')
    if step['access']['kind'] == 'private-host':
        a = grant['host_maintenance']
        require(a['historical_ledger_references'] and 'historical_actual_debit' in a
                and a['historical_actual_debit'] is not False,
                'preserved_historical_accounting_required')
        amounts = [Decimal(a[k]) for k in ('carried_protective_capacity','new_full_allowance','approved_ceiling')]
        require(all(n.is_finite() and n >= 0 for n in amounts), 'finite_nonnegative_maintenance_capacity_required')
        require(amounts[0]+amounts[1] <= amounts[2], 'maintenance_ceiling_exceeded')
        require(amounts[1] >= Decimal(remaining), 'qualified_operation_allowance_too_small')
        opened=datetime.fromisoformat(a['window_started_at'])
        require(start<=opened<=current,'actual_private_window_start_required')
        require(Decimal(str((current-opened).total_seconds()))+Decimal(remaining)<=amounts[1],
            'elapsed_plus_full_remaining_exceeds_maintenance_allowance')
    if step.get('human_acceptance',{}).get('mode','before_apply')=='before_apply':
        verify_acceptance(step)


def authority_proposal(plan_path,output):
    raw=Path(plan_path).read_bytes();plan=json.loads(raw)
    verify_qualifications(plan)
    scopes={s['signature']:digest(encoded(s['worker']['payload'])) for s in plan['steps']}
    readonly={key:[value] for key,value in scopes.items()}
    for reference in plan.get('reuse_plans',[]):
        require(digest(Path(reference['path']).read_bytes())==reference['sha256'],'reuse_plan_changed')
        old=read(reference['path'])
        for step in plan['steps']:
            for previous in old['steps']:
                if effect_signature(step)==effect_signature(previous):
                    payload=json.loads(json.dumps(step['worker']['payload']))
                    payload['state_directory']=previous['worker']['payload']['state_directory']
                    readonly[step['signature']].append(digest(encoded(payload)))
    value=dict(status='proposal_not_a_grant',plan_sha256=digest(raw),human_decision_reference=None,
        not_before=None,expires_at=None,resources=sorted({r for s in plan['steps'] for r in s['resources']}),
        signatures=list(scopes),payload_sha256=scopes,read_only_payload_sha256=readonly,
        host_maintenance=dict(historical_ledger_references=[],historical_actual_debit=None,window_started_at=None,
            carried_protective_capacity=None,new_full_allowance=None,approved_ceiling=None))
    once(output,value);return dict(proposal=str(Path(output).resolve()),granted=False,plan_sha256=value['plan_sha256'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare'); p.add_argument('config'); p.add_argument('--out', required=True); p.add_argument('--cache', default=str(STATE/'qualifications'))
    p = sub.add_parser('run'); p.add_argument('plan'); p.add_argument('--approved-plan'); p.add_argument('--authority'); p.add_argument('--local', action='store_true')
    p = sub.add_parser('authority-proposal');p.add_argument('plan');p.add_argument('--out',required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        return prepare(args.config, args.out, args.cache)
    if args.action=='authority-proposal':return authority_proposal(args.plan,args.out)
    if args.authority:
        os.environ['WAHOJOBS_PUBLICATION_AUTHORITY'] = str(Path(args.authority).resolve())
    return execute(args.plan, args.approved_plan, local=args.local)


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as error:
        print(json.dumps(dict(stopped=True, reason=str(error) if isinstance(error, Stop) else type(error).__name__)))
        raise SystemExit(1)
