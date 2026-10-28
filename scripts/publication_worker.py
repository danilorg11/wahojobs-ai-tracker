"""Pinned publication worker. JSON in/out; no shell, credentials or data dumps.

files: owned conditional file/pointer replacement and reverse in place;
sqlite: transactional keyed changes and a receipt in the SAME database;
module: an existing domain publisher exposing the same five operations;
vercel: immutable production deployment promotion through the existing CLI.
"""
from __future__ import annotations
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time

import routine_release as r

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = 'wahojobs-publication-v2'
AUDIT_TABLE = '_wahojobs_publication_v2'


def q(identifier):
    r.require(identifier and identifier.replace('_', '').isalnum(), 'invalid_sql_identifier')
    return '"'+identifier+'"'


def validate(request):
    p = request['payload']
    r.require(request['protocol'] == PROTOCOL and request['action'] in ('observe', 'apply', 'check', 'recover', 'cleanup'), 'unsupported_worker_request')
    r.require(len(request['operation']) == 32 and len(request['signature']) == 64, 'operation_binding_required')
    r.require(p['environment'] in ('local', 'production'), 'environment_required')
    if p['environment'] == 'local':
        paths = [p['state_directory'], *p.get('targets', [])]
        r.require(all(Path(path).resolve().is_relative_to(ROOT/'outputs') for path in paths), 'local_mutation_outside_outputs')
        r.require(p['kind'] != 'vercel', 'real_vercel_not_a_local_fixture')
    else:
        authority = Path(os.environ['WAHOJOBS_PUBLICATION_AUTHORITY'])
        info = authority.lstat()
        r.require(stat.S_ISREG(info.st_mode) and not authority.is_symlink(), 'regular_authority_required')
        if os.name != 'nt':
            r.require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in (0o400,0o600), 'private_owned_authority_required')
        grant = r.read(authority)
        current = datetime.now(timezone.utc)
        r.require(grant['status'] == 'granted' and grant['human_decision_reference'], 'actual_owner_grant_required')
        r.require(grant['plan_sha256'] == request['plan_sha256'] and request['signature'] in grant['signatures'], 'authority_scope_mismatch')
        r.require(datetime.fromisoformat(grant['not_before']) <= current < datetime.fromisoformat(grant['expires_at']), 'authority_not_current')
        r.require(set(p['resources']) <= set(grant['resources']), 'authority_resource_mismatch')
    return p


class Worker:
    def __init__(self, request):
        self.request, self.p = request, validate(request)
        self.root = Path(self.p['state_directory'])/request['operation']
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            self.root.chmod(0o700)
        binding = {key: request[key] for key in ('operation', 'signature', 'plan_sha256')}
        if (self.root/'binding.json').exists():
            r.require(r.read(self.root/'binding.json') == binding, 'worker_operation_rebound')
        else:
            r.once(self.root/'binding.json', binding)

    def result(self, state, **data):
        return dict(operation=self.request['operation'], signature=self.request['signature'], state=state, **data)

    def write_state(self, state, **data):
        value = self.result(state, at=r.now(), **data)
        pending = self.root/('state-'+str(time.time_ns())+'.pending')
        r.once(pending, value)
        os.replace(pending, self.root/'state.json')
        return value

    def event(self, name, value):
        r.once(self.root/(name+'.json'), dict(at=r.now(), **value))

    def hooks(self, name):
        for hook in self.p.get('hooks', {}).get(name, []):
            tick = time.monotonic()
            result = subprocess.run(hook['command'], input=None, capture_output=True, shell=False,
                cwd=hook.get('cwd'), env=dict(os.environ, **hook.get('environment', {})), timeout=hook['seconds'])
            self.event(name+'-'+str(time.time_ns()), dict(seconds=time.monotonic()-tick,
                returncode=result.returncode, stdout_sha256=r.digest(result.stdout), stderr_sha256=r.digest(result.stderr)))
            r.require(result.returncode == 0, 'qualified_hook_failed:'+name)

    def target(self, item):
        path = Path(item['path'])
        if self.p['environment'] == 'local':
            r.require(path.resolve().is_relative_to(ROOT/'outputs'), 'file_target_outside_local_outputs')
        r.require(str(path) in self.p['targets'], 'undeclared_write_target')
        return path

    def actual(self, item):
        path = self.target(item)
        if item.get('type') == 'symlink':
            return dict(type='symlink', target=os.readlink(path)) if path.is_symlink() else None
        r.require(not path.is_symlink(), 'unexpected_target_symlink')
        if item.get('type') == 'json-key':
            return dict(type='json-key',value=r.read(path)[item['key']])
        return dict(type='file', sha256=r.digest(path.read_bytes())) if path.exists() else None

    def expected_after(self, item):
        if item.get('type') == 'symlink':
            return dict(type='symlink', target=item['after_target'])
        if item.get('type') == 'json-key':
            return dict(type='json-key',value=item['after_value'])
        return dict(type='file', sha256=item['artifact_sha256'])

    def replace(self, item, *, reverse=False):
        path = self.target(item)
        saved = self.root/('before-'+r.digest(str(path).encode()))
        manifest = r.read(saved.with_suffix('.json'))
        if reverse and manifest['identity'] is None:
            path.unlink()
            return
        pending = path.with_name(path.name+'.publication-'+self.request['operation']+'.pending')
        r.require(not pending.exists() and not pending.is_symlink(), 'pending_file_requires_reconciliation')
        if item.get('type') == 'symlink':
            pending.symlink_to(manifest['identity']['target'] if reverse else item['after_target'], target_is_directory=True)
        else:
            if item.get('type') == 'json-key':
                value=r.read(path)
                value[item['key']]=item['before']['value'] if reverse else item['after_value']
                raw=r.encoded(value)
            else:
                raw = saved.read_bytes() if reverse else Path(item['artifact']).read_bytes()
            if not reverse and item.get('type') != 'json-key':
                r.require(r.digest(raw) == item['artifact_sha256'], 'immutable_artifact_changed')
            with pending.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            if os.name != 'nt':
                os.chmod(pending, manifest['mode'] if manifest['identity'] is not None else item.get('mode', 0o600))
                if manifest['identity'] is not None:
                    os.chown(pending, manifest['uid'], manifest['gid'])
        os.replace(pending, path)

    def files_observe(self):
        identities = [self.actual(item) for item in self.p['files']]
        before = [item['before'] for item in self.p['files']]
        after = [self.expected_after(item) for item in self.p['files']]
        state = r.read(self.root/'state.json') if (self.root/'state.json').exists() else {}
        if any(actual not in (old, new) for actual, old, new in zip(identities, before, after)):
            return self.result('foreign')
        if identities == after:
            return self.result('accepted' if state.get('state') == 'accepted' else 'applied')
        if identities == before:
            if (self.root/'recover-intent.json').exists() and state.get('state') == 'recovered':
                return self.result('recovered')
            if state.get('state') == 'failed':
                return self.result('failed', recovery_required=False)
            return self.result('before')
        return self.result('failed', recovery_required=True, owned_partial=True)

    def files(self, action):
        if action == 'observe':
            return self.files_observe()
        if action == 'apply':
            r.require(not (self.root/'apply-intent.json').exists(), 'worker_apply_never_replayed')
            r.require(self.files_observe()['state'] == 'before', 'accepted_files_changed')
            self.hooks('preflight')
            for item in self.p['files']:
                path = self.target(item)
                r.require(self.actual(item) == item['before'], 'current_file_changed')
                saved = self.root/('before-'+r.digest(str(path).encode()))
                info = path.lstat() if path.exists() or path.is_symlink() else None
                if info and item.get('type') != 'symlink':
                    with saved.open('xb') as stream:
                        stream.write(path.read_bytes()); stream.flush(); os.fsync(stream.fileno())
                    if os.name != 'nt': saved.chmod(0o600)
                r.once(saved.with_suffix('.json'), dict(identity=item['before'],
                    mode=stat.S_IMODE(info.st_mode) if info else 0o600,
                    uid=info.st_uid if info else None, gid=info.st_gid if info else None))
            self.event('apply-intent', dict(targets=self.p['targets']))
            try:
                self.hooks('stop')
                # Recheck every target before changing the first target.
                r.require(self.files_observe()['state'] == 'before', 'target_changed_after_stop')
                for item in self.p['files']:
                    self.replace(item)
                self.hooks('start')
                return self.write_state('applied')
            except BaseException:
                self.write_state('failed')
                raise
        if action == 'check':
            r.require(self.files_observe()['state'] in ('applied', 'accepted'), 'candidate_not_selected')
            self.hooks('check')
            if (self.root/'state.json').exists() and r.read(self.root/'state.json')['state'] == 'accepted':
                return self.files_observe()
            return self.write_state('accepted')
        if action == 'recover':
            observed = self.files_observe()
            r.require(observed['state'] in ('applied', 'accepted', 'failed'), 'return_not_owned')
            r.require(not (self.root/'recover-intent.json').exists(), 'worker_return_never_replayed')
            self.event('recover-intent', {})
            self.hooks('stop')
            # All targets were compared before any reverse; a foreign successor stops.
            r.require(self.files_observe()['state'] != 'foreign', 'foreign_before_recovery')
            for item in reversed(self.p['files']):
                if self.actual(item) == self.expected_after(item):
                    self.replace(item, reverse=True)
            self.hooks('start')
            self.hooks('recovered_check')
            return self.write_state('recovered')
        self.hooks('cleanup')
        return self.files_observe()

    @contextmanager
    def sqlite_connection(self):
        database = Path(self.p['database'])
        r.require(str(database) in self.p['targets'] and database.is_file() and not database.is_symlink(), 'declared_existing_database_required')
        if self.p['environment'] == 'local':
            r.require(database.resolve().is_relative_to(ROOT/'outputs'), 'database_outside_local_outputs')
        db = sqlite3.connect(database, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit_row(self, db):
        exists = db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (AUDIT_TABLE,)).fetchone()
        if not exists:
            return None
        row = db.execute('SELECT * FROM '+q(AUDIT_TABLE)+' WHERE operation=?', (self.request['operation'],)).fetchone()
        if row:
            r.require(row['signature'] == self.request['signature'], 'database_operation_signature_changed')
        return row

    def row_values(self, db, change):
        keys = change['key']
        row = db.execute('SELECT * FROM '+q(change['table'])+' WHERE '+' AND '.join(q(k)+'=?' for k in keys), tuple(keys.values())).fetchall()
        r.require(len(row) <= 1, 'unique_data_identity_required')
        return dict(row[0]) if row else None

    def change(self, db, change, before, after):
        actual = self.row_values(db, change)
        if before is None:
            r.require(actual is None, 'insert_identity_already_exists')
        else:
            r.require(actual is not None and all(actual.get(k) == v for k, v in before.items()), 'data_changed_by_successor')
        if after is None:
            db.execute('DELETE FROM '+q(change['table'])+' WHERE '+' AND '.join(q(k)+'=?' for k in change['key']), tuple(change['key'].values()))
        elif before is None:
            values = dict(change['key'], **after)
            db.execute('INSERT INTO '+q(change['table'])+' ('+','.join(q(k) for k in values)+') VALUES ('+','.join('?'*len(values))+')', tuple(values.values()))
        else:
            db.execute('UPDATE '+q(change['table'])+' SET '+','.join(q(k)+'=?' for k in after)+' WHERE '+' AND '.join(q(k)+'=?' for k in change['key']), (*after.values(), *change['key'].values()))

    def sqlite(self, action):
        with self.sqlite_connection() as db:
            row = self.audit_row(db)
            if action in ('observe', 'cleanup'):
                if not row:
                    return self.result('before')
                for change in json.loads(row['changes']):
                    expected = change['before'] if row['state'] == 'recovered' else change['after']
                    actual = self.row_values(db, change)
                    if (expected is None and actual is not None or expected is not None
                            and (actual is None or any(actual.get(k) != v for k, v in expected.items()))):
                        return self.result('foreign')
                return self.result(row['state'])
            if action == 'apply':
                r.require(row is None, 'data_transaction_already_admitted')
                r.require(self.p['allow_audit_schema'] is True, 'reviewed_audit_schema_required')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS '+q(AUDIT_TABLE)+' (operation TEXT PRIMARY KEY, signature TEXT NOT NULL, state TEXT NOT NULL, changes TEXT NOT NULL)')
                for change in self.p['changes']:
                    r.require(change['table'] in self.p['allowed_tables'] and change['table'] != AUDIT_TABLE, 'table_outside_data_scope')
                    self.change(db, change, change['before'], change['after'])
                db.execute('INSERT INTO '+q(AUDIT_TABLE)+' VALUES (?,?,?,?)',
                    (self.request['operation'], self.request['signature'], 'applied', json.dumps(self.p['changes'], sort_keys=True)))
                return self.result('applied')
            r.require(row is not None, 'data_receipt_missing')
            if action == 'check':
                r.require(self.sqlite('observe')['state'] in ('applied','accepted'), 'data_changed_before_acceptance')
                r.require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'data_foreign_key_failure')
                self.hooks('check')
                if row['state'] != 'accepted':
                    db.execute('UPDATE '+q(AUDIT_TABLE)+' SET state=\'accepted\' WHERE operation=?', (self.request['operation'],))
                return self.result('accepted')
            r.require(row['state'] in ('applied', 'accepted'), 'data_return_already_done_or_unowned')
            db.execute('BEGIN IMMEDIATE')
            for change in reversed(json.loads(row['changes'])):
                self.change(db, change, change['after'], change['before'])
            db.execute('UPDATE '+q(AUDIT_TABLE)+' SET state=\'recovered\' WHERE operation=?', (self.request['operation'],))
            return self.result('recovered')

    def module(self, action):
        path = Path(self.p['module'])
        r.require(r.digest(path.read_bytes()) == self.p['module_sha256'], 'domain_adapter_changed')
        spec = importlib.util.spec_from_file_location('publication_domain', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if action in ('apply','recover'):
            self.hooks('preflight')
            self.hooks('stop')
            try:
                value = module.publication(action, self.request, self.root)
            finally:
                # Starting the accepted compatible runtime is also required if
                # the domain transaction aborts. Failure remains nonterminal.
                self.hooks('start')
        else:
            value = module.publication(action, self.request, self.root)
        if action in ('check','recover'):self.hooks('check' if action=='check' else 'recovered_check')
        if action=='cleanup':self.hooks('cleanup')
        r.require(value['operation'] == self.request['operation'] and value['signature'] == self.request['signature'], 'domain_response_not_bound')
        return value

    def vercel(self, action):
        c = self.p['configuration']; client = r.Vercel(c)
        ids = [client.metadata(alias) for alias in c['aliases']]
        r.require(all(d['project'] == c['project'] and d['team'] == c['team'] for d in ids), 'vercel_identity_changed')
        selected = {d['id'] for d in ids}
        before, after = c['accepted_deployment'], c['candidate_deployment']
        if not selected <= {before, after}:
            return self.result('foreign')
        returning = (self.root/'recover-intent.json').exists()
        if action == 'observe':
            try:
                client.promotion(before if returning else after if (self.root/'apply-intent.json').exists() else None)
            except r.Stop:
                return self.result('running' if (self.root/'apply-intent.json').exists() else 'unknown')
            if selected == {before}:
                return self.result('recovered' if returning else 'before')
            if selected == {after}:
                state = r.read(self.root/'state.json').get('state') if (self.root/'state.json').exists() else None
                return self.result('accepted' if state == 'accepted' else 'applied')
            return self.result('unknown')
        if action == 'apply':
            r.require(selected == {before} and not (self.root/'apply-intent.json').exists(), 'vercel_forward_not_admissible')
            candidate = client.metadata(after)
            r.require(candidate['commit'] == c['candidate_commit'] and candidate['ready'] == 'READY'
                      and candidate['target'] == 'production' and candidate['project'] == c['project']
                      and candidate['team'] == c['team'], 'vercel_candidate_not_exact')
            r.require(all(d['commit'] == c['accepted_commit'] and d['ready'] == 'READY' for d in ids), 'vercel_accepted_not_exact')
            client.smoke(candidate['url'], c['probes'])
            self.event('apply-intent', dict(deployment=after))
            client.promote(after)
            return self.result('applied')
        if action == 'check':
            r.require(selected == {after}, 'vercel_candidate_no_longer_selected')
            client.promotion(after); client.smoke(c['aliases'][0], c['probes'])
            if (self.root/'state.json').exists() and r.read(self.root/'state.json')['state'] == 'accepted':
                return self.result('accepted')
            return self.write_state('accepted')
        if action == 'recover':
            r.require(selected == {after} and not returning, 'vercel_return_not_owned')
            client.promotion(after)
            accepted = client.metadata(before)
            r.require(accepted['commit'] == c['accepted_commit'] and accepted['ready'] == 'READY'
                      and accepted['project'] == c['project'] and accepted['team'] == c['team'], 'vercel_return_not_exact')
            self.event('recover-intent', dict(deployment=before))
            client.promote(before); client.promotion(before)
            client.smoke(c['aliases'][0], c['accepted_probes'])
            return self.write_state('recovered')
        return self.vercel('observe')

    def call(self):
        kind = self.p['kind']
        r.require(kind in ('files', 'sqlite', 'module', 'vercel'), 'unsupported_worker_kind')
        with ExitStack() as stack:
            stack.enter_context(r.file_lock(self.root/'worker.lock'))
            for lock in self.p.get('locks', []):
                if self.p['environment'] == 'local':
                    r.require(Path(lock).resolve().is_relative_to(ROOT/'outputs'), 'lock_outside_local_outputs')
                stack.enter_context(r.file_lock(lock))
            if self.p.get('native_gate') and self.request['action'] in ('apply','recover'):
                gate=self.p['native_gate']
                r.require(r.digest(Path(gate['module']).read_bytes())==gate['sha256'],'native_gate_changed')
                spec=importlib.util.spec_from_file_location('native_publication_gate',gate['module'])
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                stack.enter_context(module.operation_gate(gate['database'],require_existing=True))
            return getattr(self, kind)(self.request['action'])


def main():
    raw = sys.stdin.buffer.read(1_048_577)
    r.require(len(raw) <= 1_048_576, 'bounded_worker_request_required')
    return Worker(json.loads(raw)).call()


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as error:
        print(json.dumps(dict(error=str(error) if isinstance(error, r.Stop) else type(error).__name__)))
        raise SystemExit(1)
