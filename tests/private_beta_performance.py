"""Bounded local HTTPS performance rehearsal; synthetic sessions, no external auth.

Five distinct confirmed fixture personas, two durable sessions each. At most two
concurrent requests; the invitation cohort size is not an expensive-work parallelism.
Manual profile confirmation/correction wire timings come from separate native-form
client evidence. These local samples are not a hosting SLA.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import time
from types import SimpleNamespace
from html.parser import HTMLParser
from urllib.parse import urlencode

from tests.matching_delivery_support import DeliveryFixture
from tests.private_beta_matching_support import persona,seed_inventory,CLOCK
from tests.candidate_continuity_support import reserve_port
from tests.candidate_decision_support import publish_demo_certificate,verified_https_request
from tests.durable_google_login_browser_test_support import _running_https_browser_handler
from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
from wahojobs.request_diagnostics import RequestDiagnostics
from wahojobs import accounts


def summaries(samples):
    result={}
    for operation in sorted({s['operation'] for s in samples}):
        rows=[s for s in samples if s['operation']==operation]
        values=sorted(s['ms'] for s in rows)
        result[operation]=dict(n=len(values),p50_ms=values[math.ceil(.5*len(values))-1],
            p95_ms=values[math.ceil(.95*len(values))-1],min_ms=values[0],max_ms=values[-1],
            errors=sum(s['status']!=200 for s in rows))
    return result


class GeneratedActionForms(HTMLParser):
    """Read actual generated hidden fields; do not invent CSRF or owner input."""
    def __init__(self):
        super().__init__(); self.forms=[]; self.current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form': self.current={'target':attrs.get('action'),'fields':{}}
        elif tag=='input' and self.current is not None and attrs.get('type')=='hidden' and attrs.get('name'):
            self.current['fields'][attrs['name']]=attrs.get('value','')
    def handle_endtag(self,tag):
        if tag=='form' and self.current is not None:
            self.forms.append(self.current);self.current=None


def measure(output):
    names=['beginner_bilingual_generalist','multilingual_language_specialist',
           'customer_support_worker','software_engineer','biology_researcher']
    profiles=[persona(name) for name in names]
    port=reserve_port(); origin='https://localhost:'+str(port)
    fixture=DeliveryFixture(profiles[0],'Intentionally inactive setup control',now=CLOCK,
        candidate_profiles=profiles,public_origin=origin)
    samples=[];setup=[];diagnostics=RequestDiagnostics(capacity=1000)
    try:
        with closing(sqlite3.connect(fixture.path)) as c,c:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            c.execute('UPDATE jobs SET is_active=0 WHERE company_id=900001')
            c.execute('UPDATE canonical_opportunities SET is_active=0 WHERE company_id=900001')
            inventory=seed_inventory(c)
            sessions=[]; csrf_secrets={}
            for owner,state in enumerate(fixture.states):
                account=c.execute('SELECT b.user_id FROM product_profiles p JOIN principal_account_bindings b USING(principal_id) WHERE p.profile_id=?',
                    (state['profile_id'],)).fetchone()[0]
                second=accounts.create_session(c,user_id=account,idle_ttl=timedelta(hours=1),
                    absolute_ttl=timedelta(days=1),idempotency_key='beta-perf-second-'+str(owner),now=CLOCK)
                sessions.extend([(owner,state['session']),(owner,second.session_token)])
                csrf_secrets[owner]=second.csrf_secret
        state=SimpleNamespace(directory=fixture.path.parent,public_origin=origin)
        config=SimpleNamespace(bind_host='127.0.0.1',bind_port=port)
        def request(item):
            operation,target,owner,token=item
            started=time.perf_counter()
            response=verified_https_request(state,'GET',target,headers=(('Cookie','wahojobs_session='+token),))
            return dict(operation=operation,owner=owner,status=response.status,
                ms=(time.perf_counter()-started)*1000,bytes=len(response.body),
                request_id=response.header_values('X-Wahojobs-Request-ID')[0])
        with publish_demo_certificate(state.directory), _running_https_browser_handler(config,
                make_durable_product_browser_handler(fixture.product,diagnostics=diagnostics)):
            for owner,token in sessions[::2]:
                samples.append(request(('cold_matches','/find-matches',owner,token)))
            for owner,token in sessions[1::2]:
                detail=verified_https_request(state,'GET','/job/opportunity-970012?variant=960012',
                    headers=(('Cookie','wahojobs_session='+token),))
                assert detail.status==200
                parser=GeneratedActionForms();parser.feed(detail.body.decode('utf-8'))
                form=next(f for f in parser.forms if f['fields'].get('action')=='save')
                saved=verified_https_request(state,'POST',form['target'],
                    headers=(('Cookie','wahojobs_session='+token+'; __Host-wahojobs_session_csrf='+csrf_secrets[owner]),('Origin',origin),
                        ('Sec-Fetch-Site','same-origin'),('Content-Type','application/x-www-form-urlencoded')),
                    body=urlencode(form['fields']).encode())
                assert saved.status==303,saved.status
                returned=verified_https_request(state,'GET',saved.header_values('Location')[0],
                    headers=(('Cookie','wahojobs_session='+token),))
                assert returned.status==200
                setup.append(dict(owner=owner,detail_status=detail.status,save_status=saved.status))
            work=[]
            for _ in range(3):
                for owner,token in sessions:
                    work.extend([('warm_matches','/find-matches',owner,token),
                        ('exact_detail','/job/opportunity-970012?variant=960012',owner,token),
                        ('my_jobs','/tracker',owner,token)])
            with ThreadPoolExecutor(max_workers=2) as pool:
                samples.extend(pool.map(request,work))
        summary=summaries(samples)
        objectives=dict(cold_matches=5000,warm_matches=2000,exact_detail=1000,my_jobs=1000)
        report=dict(environment=dict(python=platform.python_version(),platform=platform.platform(),
                processor=platform.processor(),logical_cpus=os.cpu_count(),sqlite=sqlite3.sqlite_version),
            clock=CLOCK.isoformat(),source_authority='same preselected preserved-source/synthetic cohort; simulated acceptance',
            inventory_postings=len(inventory),personas=names,owners=5,sessions=10,max_concurrent_requests=2,
            my_jobs_scope='one saved Generalist posting per owner, saved through actual generated HTTPS forms',
            setup_operations=setup,
            confirmation_auth_scope='performance sessions are synthetic durable fixtures; fresh invited native-form tests are separate',
            quantile='nearest-rank; first request per owner n=5 p95 equals max; one runtime, only first request is globally cold',
            cold_scope='first request per owner in one process; not five independent cold process starts',
            summary=summary,objectives_ms=objectives,
            objectives_met=all(v['p95_ms']<=objectives[k] and v['errors']==0 for k,v in summary.items()),
            samples=samples,diagnostics=[dict(request_id=r.request_id,status=r.status,outcome=r.outcome,
                elapsed_ms=r.elapsed_ms,observed_at=r.observed_at) for r in diagnostics.snapshot()],
            limits='16 rich postings is bounded representative coverage; not a full production inventory or hosting SLA')
        Path(output).write_text(json.dumps(report,indent=2),encoding='utf-8')
        return report
    finally:
        fixture.close()


if __name__=='__main__':
    import sys
    result=measure(sys.argv[1])
    print(json.dumps({k:result[k] for k in ('summary','objectives_met')}))
