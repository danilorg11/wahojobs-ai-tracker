"""Apply one reviewed, dated content-only observation under the deployment gate.

This is an explicit operational step, never imported by ordinary navigation.
The owner-approved manifest pins the existing acceptance and public response.
No schema repair, crawl, closure authority or model invocation is performed.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wahojobs.crawler.provider_details import DetailResponse, reprocess_saved_detail
from wahojobs.db.repository import verify_job_source_acceptance_integrity


def apply_reviewed_detail(connection, manifest, body):
    required={'version','job_id','expected_accepted_capture_id','response_url','observed_at','response_sha256'}
    if set(manifest)!=required or manifest['version']!=1 or sha256(body).hexdigest()!=manifest['response_sha256']:
        raise ValueError('reviewed_detail_manifest_mismatch')
    if len(body)>2_000_000 or connection.in_transaction:
        raise ValueError('reviewed_detail_requires_bounded_response_and_own_transaction')
    jid=manifest['job_id']
    acceptance=connection.execute('SELECT accepted_capture_id FROM job_source_content_acceptances WHERE job_id=?',(jid,)).fetchone()
    if not acceptance or acceptance[0]!=manifest['expected_accepted_capture_id']:
        raise ValueError('reviewed_detail_acceptance_drift')
    before=dict(connection.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
    source=dict(connection.execute('SELECT * FROM job_source_contents WHERE job_id=?',(jid,)).fetchone())
    previous=dict(connection.execute('SELECT * FROM job_source_content_captures WHERE id=?',(acceptance[0],)).fetchone())
    response=DetailResponse(manifest['response_url'],body,manifest['observed_at'])
    with connection:
        outcome=reprocess_saved_detail(connection,jid,response)
        if not outcome.accepted:
            raise ValueError('reviewed_detail_not_accepted')
        verify_job_source_acceptance_integrity(connection,jid)
        after=dict(connection.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        if any(before[k]!=after[k] for k in before if k!='updated_at'):
            raise ValueError('reviewed_detail_changed_listing_or_lifecycle')
        current=dict(connection.execute('SELECT * FROM job_source_contents WHERE job_id=?',(jid,)).fetchone())
        if current['body']!=source['body'] or current['body_format']!=source['body_format']:
            raise ValueError('reviewed_detail_changed_description')
        if previous!=dict(connection.execute('SELECT * FROM job_source_content_captures WHERE id=?',(acceptance[0],)).fetchone()):
            raise ValueError('reviewed_detail_changed_retained_evidence')
    return dict(job_id=jid,previous_capture_id=acceptance[0],accepted_capture_id=outcome.capture_id,
        observed_at=response.observed_at,response_sha256=manifest['response_sha256'],
        description_preserved=True,listing_and_lifecycle_preserved=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--response',required=True)
    parser.add_argument('--approved-reviewed-detail',action='store_true',required=True)
    args=parser.parse_args()
    with sqlite3.connect(Path(args.database).resolve().as_uri()+'?mode=rw',uri=True) as db:
        db.row_factory=sqlite3.Row
        result=apply_reviewed_detail(db,json.loads(Path(args.manifest).read_text(encoding='utf8')),Path(args.response).read_bytes())
    print(json.dumps(result))


if __name__=='__main__':main()
