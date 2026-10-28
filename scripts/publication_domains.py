"""Adapters for actual queued WahoJobs work; retain each domain's authority.

Content repair reuses the existing transaction and offline lifetime lease. It
does not invent a new SQL repair or rewrite source verification timestamps.
Company API checks boot the exact selected source and exercise its HTTP ingress.
"""
from contextlib import closing
from hashlib import sha256
from http.client import HTTPConnection
from http.server import HTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading
from unittest.mock import Mock, patch

import routine_release as r


def protected(connection):
    return {table:sha256(repr([tuple(row) for row in connection.execute('SELECT * FROM '+table+' ORDER BY id')]).encode()).hexdigest()
            for table in ('jobs','canonical_opportunities','crawl_runs','job_events')}


def seed_content_repair(source, destination):
    sys.path.insert(0, str(source))
    from tests import test_content_recovery_plan as fixture
    from wahojobs import catalog_content_repair as repair, mindrift_content
    destination = Path(destination); destination.mkdir(parents=True)
    database = destination/'content.sqlite3'
    rows = [row for row in json.loads((fixture.EVIDENCE/'complete-recovery-plan.json').read_text(encoding='utf-8'))['records']
            if row['classification'] == 'retained_complete']
    with closing(fixture.database(database,'mindrift')) as conn:
        conn.execute("INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES ('Alignerr','alignerr','https://www.alignerr.com/jobs','core','live_feed','count_live')")
        conn.commit()
        for provider in ('mindrift','alignerr'):
            fixture.publish(conn,provider,[fixture.retained_candidate(row) for row in rows if row['provider'].lower() == provider])
        records=[]
        for row in rows:
            job=conn.execute('SELECT id FROM jobs WHERE external_id=?',(row['external_id'],)).fetchone()[0]
            original=row['retained_complete_inputs'][0]
            records.append(dict(job_id=job,url=mindrift_content.detail_url(row['external_id']) if row['provider']=='Mindrift' else row['official_url'],
                body_path=str(fixture.EVIDENCE/original['retained_response_relative_path']),observed_at=original['observed_at'],
                status=original['status'],response_sha256=original['response_sha256']))
        domain_plan=repair.build_plan(conn,database,records)
        baseline=protected(conn)
        conn.execute('CREATE TABLE publication_fixture_user_state(id INTEGER PRIMARY KEY,value TEXT)')
        conn.execute("INSERT INTO publication_fixture_user_state VALUES (1,'original user state')"); conn.commit()
    r.once(destination/'domain-plan.json',domain_plan)
    r.once(destination/'protected-before.json',baseline)
    return dict(database=str(database),plan=str(destination/'domain-plan.json'),protected=str(destination/'protected-before.json'),
                retained_records=len(records),source_dates=sorted({row['observed_at'] for row in records}))


def content(action, request, root):
    p=request['payload']; sys.path.insert(0,p['source'])
    database=Path(p['database']).resolve()
    r.require(database.is_file() and not Path(p['database']).is_symlink(),'existing_exact_domain_database_required')
    r.require(str(database) in [str(Path(path).resolve()) for path in p['targets']],'undeclared_domain_database')
    if p['environment']=='local':
        r.require(database.is_relative_to(Path(__file__).resolve().parents[1]/'outputs'),'local_domain_database_outside_outputs')
    from wahojobs import catalog_content_repair as repair
    from wahojobs.db.connection import get_connection
    from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership,release_database_lifetime_ownership,ROLE_OFFLINE_OPERATOR
    plan=r.read(p['domain_plan'])
    result=lambda state,**kw:dict(operation=request['operation'],signature=request['signature'],state=state,**kw)
    with closing(get_connection(Path(p['database']))) as conn:
        def observation():
            states=[]
            from wahojobs.crawler.provider_details import DETAIL_KEY
            from wahojobs import mindrift_content
            for record in plan['records']:
                job,source,binding=repair._binding(conn,record['job_id'])
                if (job['external_id']!=record['external_id'] or job['url']!=record['official_url']
                        or job['location']!=record['location'] or job['title']!=record['title']
                        or job['canonical_opportunity_id']!=record['canonical_id']):
                    return 'foreign'
                key=mindrift_content.KEY if record['provider']=='mindrift' else DETAIL_KEY
                metadata=json.loads(source['metadata_json']).get(key,{})
                accepted=all(metadata.get(k)==record[k] for k in ('response_sha256','observed_at','url'))
                states.append('after' if accepted else 'before' if binding==record['binding'] else 'foreign')
            if 'foreign' in states:return 'foreign'
            if set(states)=={'before'}:return 'before'
            # Already accepted exact source responses are a valid no-op, not a
            # reason to re-fetch or to overwrite their original source clocks.
            if not (root/'domain-dispatch.json').exists():return 'before'
            if set(states)!={'after'}:return 'unknown'
            if (root/'compatible-recovery.json').exists():return 'recovered'
            return 'accepted' if (root/'accepted.json').exists() else 'applied'
        state=observation()
        if action in ('observe','cleanup'):
            return result(state,retained_records=len(plan['records']),http_requests=0,availability_renewed=False)
        if action=='apply':
            r.require(state=='before' and not (root/'domain-dispatch.json').exists(),'repair_not_admissible_or_already_dispatched')
            r.once(root/'domain-dispatch.json',dict(plan_id=plan['plan_id']))
            lease=acquire_database_lifetime_ownership(Path(p['database']),role=ROLE_OFFLINE_OPERATOR)
            try:
                with patch('socket.create_connection',side_effect=AssertionError('retained repair forbids network')):
                    actual=repair.execute_plan(conn,Path(p['database']),plan,ownership=lease,authorized=True)
            finally:
                release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=Path(p['database']))
            r.once(root/'domain-result.json',actual)
            return result('applied',retained_records=actual['variants'],http_requests=actual['http_requests'])
        if action=='recover' and state=='before':
            return result('recovered',recovery='uncommitted transaction left original rows intact')
        r.require(state in ('applied','accepted','recovered'),'repair_outcome_requires_reconciliation')
        if not (root/'accepted.json').exists():
            r.require(protected(conn)==r.read(p['protected_before']),'source_lifecycle_or_availability_changed')
        for record in plan['records']:
            repair._binding(conn,record['job_id']) # Includes the original acceptance-chain verifier.
        if not (root/'accepted.json').exists():
            r.once(root/'accepted.json',dict(at=r.now(),plan_id=plan['plan_id'],records=len(plan['records']),
                source_dates_preserved=True,availability_renewed=False,model_calls=0,http_requests=0))
        if action=='recover' and not (root/'compatible-recovery.json').exists():
            r.once(root/'compatible-recovery.json',dict(at=r.now(),plan_id=plan['plan_id'],preserved_current_data=True))
        return result('recovered' if action=='recover' else 'accepted',retained_records=len(plan['records']),http_requests=0,availability_renewed=False,
            recovery='retain committed accepted content; compatible source reader; never restore old database' if action=='recover' else None)


def company_api_probe(source, expected):
    """Real HTTP to the selected reader; all source inventory is synthetic."""
    sys.path.insert(0,str(source))
    from datetime import datetime
    from tests.test_public_job_page import seed_public_job, OBSERVED_AT
    from tests.test_public_catalog_reader import BODY
    from wahojobs import public_jobs_catalog as catalog
    from wahojobs.public_catalog_reader import PublicCatalogReader,preparation_metadata
    from wahojobs.remote_beta import make_remote_handler, PROXY_HEADER
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory=sqlite3.Row;seed_public_job(db)
        db.execute("UPDATE job_source_contents SET provider='acme-ai', body=?",(BODY,));db.commit()
        metadata,generation=preparation_metadata(db)
        reader=PublicCatalogReader(catalog.load_public_jobs(db,now=datetime.fromisoformat(OBSERVED_AT)),
            clock=lambda:datetime.fromisoformat(OBSERVED_AT),metadata=metadata,generation=generation,indexable=True)
        runtime=Mock(public_origin='https://beta.wahojobs.com')
        runtime.browser_integration.handle.side_effect=AssertionError('account runtime reached')
        server=HTTPServer(('127.0.0.1',0),make_remote_handler(runtime,'a'*64,public_catalog=reader,catalog_key='b'*64))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            connection=HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            connection.request('GET','/_catalog/jobs/companies/acme-ai?page=1',
                headers={'Host':'beta.wahojobs.com',PROXY_HEADER:'a'*64,'X-Wahojobs-Catalog-Key':'b'*64})
            response=connection.getresponse();body=response.read(262145);connection.close()
            r.require(response.status==expected,'selected_company_api_contract_not_met')
            if expected==200:
                value=json.loads(body);r.require(value['company']['opportunity_count']==1,'canonical_count_wrong')
            return dict(status=response.status,bytes=len(body),real_http=True,synthetic_inventory=True)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)


def publication(action,request,root):
    p=request['payload']
    if p['domain']=='retained-content':return content(action,request,root)
    raise r.Stop('unregistered_domain_adapter')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    a=sub.add_parser('seed-content');a.add_argument('source');a.add_argument('destination')
    a=sub.add_parser('api-probe');a.add_argument('selector');a.add_argument('--expected',type=int,default=200)
    a=sub.add_parser('configuration');a.add_argument('file')
    args=parser.parse_args()
    if args.action=='seed-content':value=seed_content_repair(Path(args.source),Path(args.destination))
    elif args.action=='api-probe':value=company_api_probe(Path(args.selector).read_text().strip(),args.expected)
    else:
        raw=Path(args.file).read_text();r.require('TimeoutStartSec=660' in raw,'startup_budget_lost')
        r.require('KillMode=control-group' in raw and 'ProtectSystem=strict' in raw,'service_safety_lost')
        value=dict(configuration_validated=True,linux_systemd_exercised=False)
    print(json.dumps(value))
