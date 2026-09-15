"""Synthetic decision journey, using preserved public wording and normal services."""
from contextlib import closing, contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from tests.candidate_continuity_support import synthetic_state, _insert_copy, capture

FIXTURES = Path(__file__).parent / 'fixtures'


@contextmanager
def publish_demo_certificate(directory):
    """Keep only the generated public localhost certificate for explicit test trust."""
    from unittest.mock import patch
    from scripts import durable_google_login_app as launcher
    original = launcher._write_ephemeral_certificate
    def capture_public_certificate(tls_directory):
        result = original(tls_directory)
        (Path(directory)/'demo-tls-cert.pem').write_bytes(Path(result[0]).read_bytes())
        return result
    with patch.object(launcher, '_write_ephemeral_certificate', side_effect=capture_public_certificate):
        yield


def verified_https_request(state, method, target, *, headers=(), body=None):
    """Same disposable HTTP adapter with explicit certificate and hostname checks."""
    import http.client, ssl
    from urllib.parse import urlsplit
    from tests.durable_google_login_browser_test_support import BrowserHttpResponse
    origin = urlsplit(state.public_origin)
    assert origin.scheme == 'https' and origin.hostname == 'localhost' and origin.port != 8802
    assert target.startswith('/') and not target.startswith('//')
    context = ssl.create_default_context(cafile=str(state.directory/'demo-tls-cert.pem'))
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    connection = http.client.HTTPSConnection('localhost', origin.port, context=context, timeout=30)
    try:
        connection.request(method, target, body=body, headers={'Host':origin.netloc, **dict(headers)})
        response = connection.getresponse()
        return BrowserHttpResponse(response.status, tuple(response.getheaders()), response.read())
    finally:
        connection.close()

TOOL_BODY = ('## Responsibilities\n\nReview and evaluate AI-generated content.\n\n'
             '## Requirements\n\nExperience with Python\n\n'
             '## Engagement\n\nLocation: Remote\nType: Hourly Contract\nCommitment: 20 hours per week\nPay: USD 25 per hour')


def demo_profile():
    from wahojobs.profiles.canonical_v2 import _material_field_paths, validate_canonical_profile_v2
    from tests.test_professional_background_components import confirmed
    p = json.loads((FIXTURES/'source_requirement_fidelity'/'profiles.json').read_text(encoding='utf-8-sig'))['evaluator_br']
    p['identity']['display_name'] = 'Alex — synthetic candidate'
    for key in ('normalized',):
        if 'Python' not in p['skills'].setdefault(key, []): p['skills'][key].append('Python')
    p['provenance']['field_sources'] = []
    for path in _material_field_paths(p): confirmed(p, path)
    return validate_canonical_profile_v2(p)


@contextmanager
def decision_state(*, port=None):
    with synthetic_state(port=port, candidate_profile=demo_profile(),
                         posting_title='AI Content Evaluation with Python', posting_body=TOOL_BODY) as state:
        with closing(sqlite3.connect(state.database_path)) as c, c:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            c.execute("UPDATE companies SET name='Practice opportunities' WHERE id=7001")
            company = dict(c.execute('SELECT * FROM companies WHERE id=7001').fetchone())
            canonical = dict(c.execute('SELECT * FROM canonical_opportunities WHERE id=7002').fetchone())
            posting = dict(c.execute('SELECT * FROM jobs WHERE id=7003').fetchone())
            # A new posting has no inherited source-capture authority.
            posting = {k:posting[k] for k in ('id','company_id','canonical_opportunity_id','external_id','title',
                'location','department','expertise','commitment','url','source_hash','first_seen_at','last_seen_at',
                'is_active','opportunity_kind','availability_basis','include_in_live_market_estimate')}
            crawl = dict(c.execute('SELECT * FROM crawl_runs WHERE company_id=7001 LIMIT 1').fetchone())
            cards = json.loads((FIXTURES/'card_source_wording.json').read_text(encoding='utf-8-sig'))
            full = (FIXTURES/'source_requirement_fidelity'/'accepted-generalist.txt').read_text(encoding='utf-8')
            sources = [dict(provider='alignerr', company='Alignerr', title='Generalist', body=full,
                            format='text/markdown', external='583b0f74-43d6-4382-a132-0e9fd41daf8c',
                            url='https://www.alignerr.com/jobs/583b0f74-43d6-4382-a132-0e9fd41daf8c', metadata={}),
                *[dict(provider='mercor', company='Mercor', title='Scientific Computing' if s['job_id']==11242 else 'Biology Expert Network',
                       body=s['body'], format=s['body_format'], external=s['external_id'], url=s['url'],
                       metadata=json.loads(s['metadata_json'])) for s in cards if s['job_id'] in (11242,1039)],
                dict(provider='synthetic-general', company='Practice opportunities', title='AI Generalist',
                     body='## Responsibilities\n\nReview and evaluate AI-generated content.\n\n## Requirements\n\nNo prior AI experience required',
                     format='text/markdown', external='general-evaluation', url='https://jobs.example.test/general-evaluation', metadata={})]
            for i, source in enumerate(sources):
                company_id, canonical_id, job_id = (9201 if i==2 else 9200+i), 9300+i, 9400+i
                if i!=2:
                    _insert_copy(c,'companies',company,id=company_id,name=source['company'],slug=source['provider'])
                _insert_copy(c,'canonical_opportunities',canonical,id=canonical_id,company_id=company_id,
                    canonical_key='decision-'+str(i),canonical_title=source['title'],normalized_title=source['title'].lower(),variant_count=1)
                _insert_copy(c,'jobs',posting,id=job_id,company_id=company_id,canonical_opportunity_id=canonical_id,
                    title=source['title'],external_id=source['external'],url=source['url'],source_hash='decision-'+str(i))
                if i!=2: _insert_copy(c,'crawl_runs',crawl,id=9500+i,company_id=company_id)
                from wahojobs.crawler.types import JobCandidate
                from wahojobs.source_capture import SourceCaptureContext
                from wahojobs.db.repository import upsert_job_source_content
                candidate = JobCandidate(external_id=source['external'],title=source['title'],location='Remote',url=source['url'],
                    source_hash='decision-'+str(i),source_body=source['body'],source_body_format=source['format'],source_metadata=source['metadata'])
                upsert_job_source_content(c,job_id,source['provider'],'preserved-public-wording-synthetic-demo',candidate,state.clock().isoformat(),
                    capture_context=SourceCaptureContext(None,'success',False,True,True,False,1,1,1,0,'synthetic preserved-source acceptance','decision-v1'))
            c.commit()
        marker = json.loads((state.directory/'continuity-demo.json').read_text())
        marker['candidate_decision_v1'] = True
        marker['guide'] = 'Compare Matches, open AI Content Evaluation with Python, Update profile, review Python experience, confirm, return, Save/Applied, My Jobs.'
        (state.directory/'continuity-demo.json').write_text(json.dumps(marker),encoding='utf-8')
        yield state


def observe(state):
    from tests.test_candidate_continuity_client import persisted_state
    result = persisted_state(state)
    with closing(sqlite3.connect(state.database_path.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        for key,table in (('profiles','product_profiles'),('revisions','product_profile_revisions'),('profile_sources','product_profile_sources')):
            result[key]=[dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
    return result
