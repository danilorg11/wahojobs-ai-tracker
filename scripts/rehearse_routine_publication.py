"""Real company frontend build + local HTTP publication/failure/restart rehearsal.

No cloud/SSH/account credentials. Source and previous evidence are read only.
The Vercel provider is simulated; its timings are not live Vercel measurements.
"""
from __future__ import annotations
import argparse
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import routine_release as r

WORKSPACE = Path(__file__).resolve().parents[1]
WEBSITE = WORKSPACE/'.worktrees/remote-company-catalog'
EXISTING = WORKSPACE/'outputs/remote-companies-20261021'
DEPS = Path('C:/Users/danrg/Documents/Codex/2026-10-01/task/website-integration/node_modules')
NODE = 'C:/Program Files/nodejs/node.exe'
CLI_FIXTURE = WORKSPACE/'tests/fixtures/routine_vercel_cli.py'
SCOPE = WORKSPACE/'.worktrees/catalogue-ui-jobposting-audit-20261016/exports/jobposting-continuity-20261016/release-31301c2/build-network-scope.cjs'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def junction(target, link):
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def commit(repo, label):
    r.git(repo, 'add', '.')
    r.git(repo, '-c', 'user.name=Offline release rehearsal', '-c', 'user.email=fixture@example.invalid',
          '-c', 'commit.gpgsign=false', 'commit', '-qm', label)
    return r.git(repo, 'rev-parse', 'HEAD').decode().strip()


def request(port, path, host=None):
    connection = HTTPConnection('127.0.0.1', port, timeout=10)
    connection.request('GET', path, headers={'Host': host} if host else {})
    response = connection.getresponse()
    body = response.read(2_097_153)
    connection.close()
    return response.status, body


class Loopback(r.Vercel):
    def smoke(self, host, probes):
        rows = []
        for spec in probes:
            connection = HTTPConnection('127.0.0.1', int(self.config['vercel_environment']['WAHOJOBS_RELEASE_FIXTURE_PORT']), timeout=10)
            connection.request('GET', spec['path'], headers={'Host': host})
            response = connection.getresponse()
            try:
                rows.append(r.probe_response(response, spec))
            finally:
                connection.close()
        return rows

    def promote(self, target):
        value = super().promote(target)
        if os.environ.get('WAHOJOBS_REHEARSAL_ABORT') == '1' and target == 'dpl_next':
            raise KeyboardInterrupt('injected process exit after provider commit')
        return value


def child(plan, run):
    c = r.read(plan)['config']
    # Only the rehearsal process redirects the canonical registry to its fixture.
    r.LEGACY_REGISTRY = Path(c['coordination_registry'])
    print(json.dumps(r.publish(plan, c['candidate_deployment'], run, r.digest(Path(plan).read_bytes()), Loopback(c))))


def rehearsal(output):
    output = Path(output).resolve()
    r.require(output.is_relative_to(WORKSPACE/'outputs') and not (output/'website/.git').exists()
              and not (output/'config.json').exists(), 'new_workspace_output_or_incomplete_source_setup_required')
    output.mkdir(parents=True, exist_ok=True)
    tick, started = time.monotonic(), r.now()
    source = output/'website'; source.mkdir(exist_ok=True)
    archive = r.git(WEBSITE, 'archive', '8ea2cc58c99c1b4bd8979d7354486fd903a75cb0')
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        package.extractall(source, filter='data')
    if not (source/'node_modules').is_dir():
        junction(DEPS, source/'node_modules')
    r.git(source, 'init', '-q')
    accepted = commit(source, 'unchanged reviewed company source (offline fixture)')
    css = source/'src/components/CompanyAIJobs/styles.module.css'
    css.write_bytes(css.read_bytes().replace(b'scroll-margin-top: 24px;', b'scroll-margin-top: 32px;'))
    candidate = commit(source, 'independent company anchor spacing (offline fixture)')
    registry = output/'registry.json'
    historical = dict(historical=dict(actual_debit=None, protected_capacity='712820', historical_ceiling='769600'))
    r.once(registry, historical)
    r.LEGACY_REGISTRY = registry
    visual = output/'visual'; visual.mkdir()
    for name in ('fixture-preload.cjs', 'fixture-data.json', 'decode-html.cjs'):
        (visual/name).write_bytes((EXISTING/'visual'/name).read_bytes())
    audit = output/'audit'; audit.mkdir()
    for name in ('jobs-page1.html', 'public-job-mercor-sample.html'):
        (audit/name).write_bytes((EXISTING/'audit'/name).read_bytes())
    launcher = (EXISTING/'visual/start-fixture-server.cjs').read_text(encoding='utf-8')
    launcher = launcher.replace("path.resolve(__dirname, '../../../.worktrees/remote-company-catalog')", 'process.env.WAHOJOBS_REHEARSAL_SOURCE')
    (visual/'start.cjs').write_text(launcher, encoding='utf-8')
    app_port = free_port()
    provider_port = free_port()
    environment = dict(BASE_API_URL='http://104.248.236.93', MAIN_DOMAIN='www.wahojobs.com',
        AI_SUBDOMAIN='worksmarter.wahojobs.com', NEXTAUTH_URL='https://www.wahojobs.com',
        NEXTAUTH_SECRET='synthetic-local-build-only-not-production', NEXT_PUBLIC_WAHOJOBS_CANDIDATE_ENABLED='1', NODE_ENV='production',
        BUILD_SCOPE_TRACE=str(output/'build-public-network.jsonl'), NODE_OPTIONS='--require "'+SCOPE.as_posix()+'"')
    c = dict(repository=str(source), candidate_commit=candidate, accepted_commit=accepted,
        project='prj_rehearsal', team='team_rehearsal', scope='fixture',
        candidate_deployment='dpl_next', accepted_deployment='dpl_base',
        aliases=['www.example.test', 'old.example.test'], release_seconds=900,
        coordination_registry=str(registry), coordinator_directory=str(WORKSPACE/'outputs/publication-state'),
        vercel_command=[sys.executable, '-B', str(CLI_FIXTURE)],
        vercel_environment={'WAHOJOBS_RELEASE_FIXTURE_PORT': str(provider_port)},
        check_environment=environment,
        tool_files=[sys.executable, NODE, str(DEPS/'next/dist/bin/next'), str(DEPS/'next/package.json'), str(SCOPE), str(CLI_FIXTURE)],
        checks=[dict(name='regression', inputs=['src/**', 'tests/**'], command=[NODE, '--test', *[str(p) for p in sorted((source/'tests').glob('*.test.mjs'))]], timeout_seconds=120),
                dict(name='build', inputs=['**', '*'], command=[NODE, str(DEPS/'next/dist/bin/next'), 'build'], timeout_seconds=600)],
        probes=[dict(path='/company/mercor', status=200, contains=['Mercor', 'AI Training Jobs', '/jobs/opportunity-'],
                     absent=['AI training opportunities are temporarily unavailable']),
                dict(path='/remote-companies?search=Mercor', status=200, contains=['Remote Companies'])],
        accepted_probes=[dict(path='/company/mercor', status=200, contains=['Mercor', 'AI Training Jobs'])])
    config_path = output/'config.json'; r.once(config_path, c)
    return exercise(output, c)


def exercise(output, c):
    output = Path(output).resolve()
    r.require(output.is_relative_to(WORKSPACE/'outputs'), 'local_rehearsal_required')
    execution = output/('cycle-'+str(time.time_ns())); execution.mkdir()
    c = json.loads(json.dumps(c))
    c['check_environment']['BUILD_SCOPE_TRACE'] = str(execution/'build-public-network.jsonl')
    source, registry, visual = output/'website', output/'registry.json', output/'visual'
    tick, started = time.monotonic(), r.now()
    candidate, accepted = c['candidate_commit'], c['accepted_commit']
    historical = r.read(registry)
    r.require(not any(isinstance(v, dict) and v.get('hold_active') for v in historical.values()), 'finish_prior_rehearsal_operation_first')
    r.LEGACY_REGISTRY = registry
    app_port = free_port()
    provider_port = int(c['vercel_environment']['WAHOJOBS_RELEASE_FIXTURE_PORT'])
    config_path = execution/'config.json'; r.once(config_path, c)
    # The existing navigation regression compares with this exact historical Git
    # commit. Preserve its real objects instead of weakening that test in a clone.
    reference = '29f499d86e109e0d19c54d3e2db35e222178e6ab'
    ids = [reference, r.git(WEBSITE, 'rev-parse', reference+'^{tree}').decode().strip()]
    ids += [line.split()[2] for line in r.git(WEBSITE, 'ls-tree', '-r', '-t', reference).decode().splitlines()]
    packed = subprocess.run(['git', '-C', str(WEBSITE), 'pack-objects', '--stdout'],
        input=('\n'.join(ids)+'\n').encode(), capture_output=True, check=True, timeout=30).stdout
    subprocess.run(['git', '-C', str(source), 'index-pack', '--stdin'], input=packed,
                   capture_output=True, check=True, timeout=30)
    first_tick = time.monotonic()
    first = r.prepare(config_path, output/'cache', execution/'plan.json')
    qualification_seconds = time.monotonic()-first_tick
    cache_tick = time.monotonic()
    reused = r.prepare(config_path, output/'cache', execution/'plan-reused.json')
    cache_seconds = time.monotonic()-cache_tick
    log = (execution/'next-server.log').open('xb')
    app_env = r.clean_environment(dict(WAHOJOBS_FIXTURE_PORT=str(app_port), WAHOJOBS_REHEARSAL_SOURCE=str(source), NODE_ENV='production'))
    app = subprocess.Popen([NODE, str(visual/'start.cjs')], cwd=source, env=app_env, stdout=log, stderr=subprocess.STDOUT)
    db = execution/'preserved.sqlite'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE records (value TEXT)'); connection.execute('INSERT INTO records VALUES (?)', ('before release',))
    state = dict(selected='dpl_base', mode='normal', actions=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value, status=200):
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def do_POST(self):
            target = self.path.removeprefix('/promote/')
            r.require(target in ('dpl_next', 'dpl_base'), 'fixture_target')
            state['actions'].append(target); state['selected'] = target
            if target == 'dpl_next':
                with sqlite3.connect(db) as connection:
                    connection.execute('INSERT INTO records VALUES (?)', ('new data during publication',))
            self.respond(dict(selected=target), 500 if state['mode'] == 'lost_response' and target == 'dpl_next' else 200)

        def do_GET(self):
            if self.path.startswith('/v9/projects/'):
                self.respond(dict(id=c['project'], accountId=c['team'],
                    lastAliasRequest=dict(jobStatus='succeeded', toDeploymentId=state['selected'], type='promote')))
            elif self.path.startswith('/v13/deployments/'):
                target = self.path.rsplit('/', 1)[-1]
                identifier = state['selected'] if target in c['aliases'] else target
                self.respond(dict(id=identifier, projectId=c['project'], ownerId=c['team'], readyState='READY',
                    target='production', meta=dict(githubCommitSha=accepted if identifier == 'dpl_base' else candidate),
                    url=identifier+'.vercel.app'))
            elif state['mode'] == 'bad_live' and state['selected'] == 'dpl_next' and self.headers.get('Host') in c['aliases']:
                self.respond(b'Injected live failure', 503)
            else:
                status, body = request(app_port, self.path)
                self.respond(body, status)

    server = ThreadingHTTPServer(('127.0.0.1', provider_port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    scenarios = []
    try:
        startup_deadline = time.monotonic()+45
        while True:
            try:
                if request(app_port, '/__fixture/status')[0] == 200:
                    break
            except OSError:
                pass
            r.require(app.poll() is None and time.monotonic() < startup_deadline, 'real_next_fixture_failed_to_start')
            time.sleep(.2)
        for name in ('normal', 'bad_live', 'lost_response', 'process_restart'):
            state.update(selected='dpl_base', mode=name, actions=[])
            run = execution/name
            began = time.monotonic()
            command = [sys.executable, '-B', str(Path(__file__).resolve()), '_publish', str(execution/'plan.json'), str(run)]
            env = r.clean_environment({'WAHOJOBS_REHEARSAL_ABORT': '1'} if name == 'process_restart' else {})
            first_process = subprocess.run(command, env=env, capture_output=True, timeout=90)
            if name == 'process_restart':
                r.require(first_process.returncode != 0 and state['actions'] == ['dpl_next'], 'injected_process_exit_not_observed')
                r.require(any(isinstance(row, dict) and row.get('hold_active') for row in r.read(registry).values()), 'unknown_hold_missing')
                first_process = subprocess.run(command, env=r.clean_environment({}), capture_output=True, timeout=90)
            r.require(first_process.returncode == 0, 'rehearsal_scenario_failed: '+first_process.stdout.decode(errors='replace'))
            result = json.loads(first_process.stdout)
            r.require(result['state'] == ('recovered' if name == 'bad_live' else 'accepted'), 'wrong_scenario_exit')
            r.require(state['actions'] == (['dpl_next', 'dpl_base'] if name == 'bad_live' else ['dpl_next']), 'duplicate_cloud_action')
            r.require(r.read(registry) == historical, 'historical_accounting_or_hold_not_preserved')
            scenarios.append(dict(scenario=name, seconds=time.monotonic()-began, result=result['state'], actions=list(state['actions'])))
        with sqlite3.connect(db) as connection:
            rows = connection.execute('SELECT count(*) FROM records').fetchone()[0]
        r.require(rows == 5, 'data_lost_during_return')
        report = dict(started_at=started, completed_at=r.now(), seconds=time.monotonic()-tick,
            qualification_seconds=qualification_seconds, reused_qualification_seconds=cache_seconds,
            original_plan=first, reused_plan=reused, scenarios=scenarios, data_rows_preserved=rows,
            real_next_build=True, real_next_http=True, provider='loopback simulation, not live Vercel',
            production_changed=False, private_access=False, human_interventions_during_rehearsal=0,
            original_company_source='8ea2cc58c99c1b4bd8979d7354486fd903a75cb0',
            change='CompanyAIJobs scroll margin 24px to 32px in disposable source only')
        r.once(execution/'report.json', report)
        return report
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        app.terminate()
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill(); app.wait(timeout=5)
        log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'continue', '_publish']); parser.add_argument('path'); parser.add_argument('run', nargs='?')
    args = parser.parse_args()
    if args.action == '_publish':
        child(args.path, args.run)
    elif args.action == 'run':
        print(json.dumps(rehearsal(args.path), indent=2))
    else:
        print(json.dumps(exercise(Path(args.path), r.read(Path(args.path)/'config.json')), indent=2))
