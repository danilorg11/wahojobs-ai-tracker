"""Exact public source replay and labelled synthetic contrasts; no owner records."""
from copy import deepcopy
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode
from unittest.mock import patch

from tests.matching_delivery_support import DeliveryFixture, JOB
from tests.test_accepted_title_uncertainty import profile
from tests.candidate_continuity_support import Page
from wahojobs.crawler.provider_details import DetailResponse, reprocess_saved_detail
from wahojobs.crawler.types import JobCandidate
from wahojobs.db.repository import upsert_job_source_content
from wahojobs.source_capture import SourceCaptureContext

ROOT=Path(__file__).parent/'fixtures'/'geographic_provenance'
PROVENANCE=json.loads((ROOT/'provenance.json').read_text(encoding='utf-8'))
CLOCK=datetime.fromisoformat('2026-09-14T17:40:00+00:00')


class GeographyFixture(DeliveryFixture):
    def __init__(self, *, body=None, posting=None, sibling=False, html=False):
        self.changed_body=body;self.posting=posting;self.html=html
        from wahojobs import accounts
        created=[];original=accounts.create_session
        def observe(*args,**kwargs):
            result=original(*args,**kwargs);created.append(result);return result
        with patch.object(accounts,'create_session',side_effect=observe):
            super().__init__(profile(), '', title='Generalist', sibling=sibling, now=CLOCK)
        for state in self.states:
            state['csrf']=next(s.csrf_secret for s in created if s.session_token==state['session'])
        if sibling:
            with self.connections.writable_connection_provider() as c:
                c.row_factory=sqlite3.Row
                with c:
                    self._capture(c,'',JOB+1)

    def _capture(self,c,text,job_id):
        import re
        packet=json.loads(re.search(rb'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                   (ROOT/'response.raw.html').read_bytes(),re.S).group(1))
        record=packet['props']['pageProps']['job']
        original=job_id==JOB and self.changed_body is None and self.posting is None and not self.html
        if not original:
            # New synthetic response, never a historical response rebound to another posting.
            record['id']='00000000-0000-4000-8000-'+str(job_id).zfill(12)
            if self.changed_body is not None:record['longDescription']=self.changed_body
            record['location']=('Canada' if job_id!=JOB else self.posting) or 'Vietnam'
            record.pop('htmlLongDescription',None)
            if self.html:
                record['htmlLongDescription']=record.pop('longDescription')
        url='https://www.alignerr.com/jobs/'+record['id']
        company=c.execute("SELECT id FROM companies WHERE slug='alignerr'").fetchone()[0]
        c.execute('UPDATE canonical_opportunities SET company_id=? WHERE id=900002',(company,))
        c.execute('UPDATE crawl_runs SET company_id=? WHERE id=900010',(company,))
        c.execute('UPDATE jobs SET company_id=?,external_id=?,url=? WHERE id=?',(company,record['id'],url,job_id))
        row=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        catalog=PROVENANCE['catalog']['source']
        item=JobCandidate(title=row['title'],external_id=row['external_id'],url=url,location='Remote',
                          source_hash=row['source_hash'],source_body=catalog['body'],source_body_format='text/plain',
                          source_metadata=json.loads(catalog['metadata_json']))
        context=SourceCaptureContext(None,'success',False,True,True,False,1,1,1,0,'synthetic catalog','synthetic-v1')
        upsert_job_source_content(c,job_id,'alignerr','alignerr-marketplace',item,
                                  '2026-09-13T13:47:57+00:00',capture_context=context)
        raw=(ROOT/'response.raw.html').read_bytes() if original else (
            '<script id="__NEXT_DATA__">'+json.dumps(packet)+'</script>').encode()
        response=DetailResponse(url,raw,PROVENANCE['original']['observed_at'] if original else CLOCK.isoformat())
        result=reprocess_saved_detail(c,job_id,response)
        assert result.accepted

    def save_from_detail(self,response):
        form=Page(response.body).action('save');data=urlencode(form['fields']).encode()
        cookie='wahojobs_session='+self.states[0]['session']+'; __Host-wahojobs_session_csrf='+self.states[0]['csrf']
        headers=(('Host','localhost:8843'),('Cookie',cookie),('Origin','https://localhost:8843'),
                 ('Sec-Fetch-Site','same-origin'),('Content-Type','application/x-www-form-urlencoded'),
                 ('Content-Length',str(len(data))),('Accept','application/json'))
        return self.product.handle('POST',form['target'],headers,BytesIO(data))
