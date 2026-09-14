"""Disposable delivery through production composition and real synthetic sessions."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from tests.workos_authkit_test_support import build_m008
from tests.persistent_profiles_repository_test_support import account_context
from tests.test_accepted_title_uncertainty import profile
from tests.test_professional_background_components import confirmed
from wahojobs import accounts, authenticated_profile_matches as browser
from wahojobs.authenticated_variant_details import variant_detail_url
from wahojobs.crawler.types import JobCandidate
from wahojobs.db.repository import upsert_job_source_content
from wahojobs.database_lifetime_ownership import (
    acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_DURABLE_RUNTIME)
from wahojobs.persistent_profiles import CreatePersistentProfileCommand, ConfirmedAboutYouTextSourceDraft
from wahojobs.persistent_profiles_repository import PersistentProfileRepository
from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2, _material_field_paths
from wahojobs.profiles.education_entries import project_education_entries_to_legacy
from wahojobs.public_job_canary import PublicJobCanaryRoutingGate
from wahojobs.source_capture import SourceCaptureContext
from wahojobs.workos_authkit_staging import _build_profile_integration, _StagingDatabaseConnections

NOW = datetime(2026, 9, 10, 23, 57, 43, tzinfo=timezone.utc)
JOB = 900003
TITLE = 'Uncatalogued assessment position'


def degree_profile(field='marketing', *, role=None, years=None):
    p = profile(role, 6 if role else None)
    if field == 'no_degree':
        p['education']['education_level'] = 'no_degree'
    elif field:
        entries = [dict(kind='bachelor', qualification='Bachelor degree in '+field, field=field,
                        institution='', status='completed', completion_year=None)]
        p['education'] = dict(project_education_entries_to_legacy(entries), entries=entries)
    if years is not None:
        p['experience']['years_by_domain'] = [dict(domain='marketing', years=years)]
    p['provenance']['field_sources'] = []
    for path in _material_field_paths(p): confirmed(p, path)
    return validate_canonical_profile_v2(p)


def body(field='marketing', *, extra='Working proficiency in Python', alternative=True):
    clauses = [f'5+ years of relevant professional experience in {field}']
    if alternative: clauses.append(f'Alternatively, a degree in {field} is sufficient.')
    if extra: clauses.append(extra)
    return 'Key Responsibilities\n\nEvaluate AI outputs and provide feedback.\n\nRequirements\n\n' + '\n\n'.join(clauses)


class DeliveryFixture:
    def __init__(self, candidate, source_body, *, inventory=None, title=TITLE, sibling=False, source_age_hours=0):
        self.temporary = tempfile.TemporaryDirectory(prefix='matching-delivery-')
        self.path = Path(self.temporary.name)/'synthetic.sqlite3'
        self.now = NOW
        if inventory:
            with closing(sqlite3.connect(Path(inventory).as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(self.path)) as dst:
                src.execute('PRAGMA query_only=ON'); src.backup(dst)
        else:
            from scripts.public_job_identity_migration import apply_public_job_identity_migration
            from scripts.ai_profile_import_migration import apply_ai_profile_import_migration
            from scripts.resumable_ai_profile_intake_migration import apply_resumable_ai_profile_intake_migration
            with closing(build_m008(self.path)) as c:
                apply_public_job_identity_migration(c); apply_ai_profile_import_migration(c); apply_resumable_ai_profile_intake_migration(c)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            stamp=(NOW-timedelta(hours=source_age_hours)).isoformat()
            c.execute('INSERT INTO companies(id,name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES (900001,?,?,?,?,?,?)',
                      ('Synthetic delivery comparison','synthetic-delivery','https://example.test/','core','live_feed','count_live'))
            c.execute('INSERT INTO canonical_opportunities(id,company_id,canonical_key,canonical_title,normalized_title,source_category,first_seen_at,last_seen_at,is_active,variant_count) VALUES (900002,900001,?,?,?,?,?,?,1,?)',
                      ('delivery-contract',title,title.lower(),'',stamp,stamp,2 if sibling else 1))
            for job_id in (JOB, JOB+1) if sibling else (JOB,):
                c.execute('INSERT INTO jobs(id,company_id,canonical_opportunity_id,external_id,title,location,department,expertise,commitment,url,source_hash,first_seen_at,last_seen_at,is_active,opportunity_kind,availability_basis,include_in_live_market_estimate) VALUES (?,900001,900002,?,?,\'Remote\',\'\',\'\',\'\',?,?,?,?,1,\'live_posting\',\'api_feed\',1)',
                          (job_id,'synthetic-'+str(job_id),title,'https://example.test/'+str(job_id),'synthetic-hash-'+str(job_id),stamp,stamp))
            c.execute('INSERT INTO crawl_runs(id,company_id,status,started_at,finished_at,jobs_found_count,used_sample_data) VALUES (900010,900001,\'success\',?,?,1,0)',(stamp,stamp))
            self._capture(c,source_body,JOB)
            self.states=[]
            for index in (0,1):
                owner=account_context(c,str(95000+index))
                account=c.execute('SELECT user_id FROM principal_account_bindings WHERE principal_id=?',(owner.principal_id,)).fetchone()[0]
                command=CreatePersistentProfileCommand.prepare(principal=owner,canonical_profile_v2=deepcopy(candidate),
                    sources=(ConfirmedAboutYouTextSourceDraft('SYNTHETIC delivery contract profile\n'+json.dumps(candidate),NOW),),
                    normalizer_version='fixture',reviewer_version='synthetic_delivery',actor_type='authenticated_user',
                    reason_code='profile.create',idempotency_key='delivery-profile-'+str(index),accepted_at=NOW)
                created=PersistentProfileRepository().create(c,command)
                session=accounts.create_session(c,user_id=account,idle_ttl=timedelta(hours=1),absolute_ttl=timedelta(days=1),
                    idempotency_key='delivery-session-'+str(index),now=NOW)
                self.states.append(dict(profile_id=created.profile_id,session=session.session_token))
        self.ownership=acquire_database_lifetime_ownership(self.path,role=ROLE_DURABLE_RUNTIME)
        self.connections=_StagingDatabaseConnections(self.path,self.ownership)
        self.product=_build_profile_integration(self.connections,SimpleNamespace(environment_namespace='private_beta',
            public_origin='https://localhost:8843',public_job_canary_gate=PublicJobCanaryRoutingGate.disabled()),lambda:self.now)
        self.integration=self.product._matches_integration

    def _capture(self,c,text,job_id):
        row=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        candidate=JobCandidate(external_id=row['external_id'],title=row['title'],location=row['location'],url=row['url'],
            source_hash=row['source_hash'],source_body=text,source_body_format='text/plain',source_metadata={})
        upsert_job_source_content(c,job_id,'synthetic-delivery','fixture',candidate,self.now.isoformat(),
            capture_context=SourceCaptureContext(crawl_run_id=None,provider_outcome='success',used_sample_data=False,
                snapshot_complete=True,pagination_complete=True,empty_snapshot_validated=False,raw_record_count=1,
                normalized_record_count=1,candidate_count=1,rejected_record_count=0,payload_shape='synthetic',schema_fingerprint='synthetic'))

    def get(self,target='/find-matches',*,owner=0):
        headers=[('Host','localhost:8843')]
        if owner is not None: headers.append(('Cookie','wahojobs_session='+self.states[owner]['session']))
        return self.product.handle('GET',target,tuple(headers))

    def current(self):
        response=self.get()
        assert response.status==200,response.body
        run=next(reversed(self.integration._registry._runs.values()))
        context=run.recommendation_context
        match=next((m for rows in context['matches'].values() for m in rows if m['canonical_opportunity_id']==900002),None)
        return response,run,context,match

    def detail(self,run,job_id=JOB):
        from wahojobs import authenticated_source_detail as detail
        observed=[];original=detail.prepare_detail_display
        def observe(job,p):
            result=original(job,p);observed.append(deepcopy(result));return result
        with patch.object(detail,'prepare_detail_display',side_effect=observe):
            response=self.get(variant_detail_url(dict(job_id=job_id,canonical_opportunity_id=900002),run_id=run.match_run_id))
        assert response.status==200,response.body
        return response,observed[0]

    def close(self):
        self.product.close();self.connections.close()
        release_database_lifetime_ownership(self.ownership,role=ROLE_DURABLE_RUNTIME,database_path=self.path)
        self.temporary.cleanup()
