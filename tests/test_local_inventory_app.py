"""Configured authenticated app reads inventory built by the ordinary crawler."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from html import unescape
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.test_local_inventory_refresh import run_saved, CASES, DESCRIPTION, GEOGRAPHY, ROOT
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.durable_google_login_browser_test_support import (
    temporary_browser_login_state, loopback_and_in_memory_provider_only,
    cookie_header, cookie_values, form_body, https_request, provider_callback_for,
    running_https_production_launcher_app,
)
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import ALIGNERR_SEED, MERCOR_SEED, MICRO1_SEED, with_source_classification_defaults
from wahojobs.durable_google_login_runtime import build_durable_google_login_runtime


def seed_sources(path):
    with closing(get_connection(path)) as conn, conn:
        for seed in (ALIGNERR_SEED,MERCOR_SEED,MICRO1_SEED):
            conn.execute('INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES(:name,:slug,:careers_url,:source_tier,:inventory_model,:market_count_policy)',with_source_classification_defaults(seed))


def account_state(path):
    with closing(get_connection(path)) as conn:
        # Include every account/profile/ownership/session/consent table, not just
        # row counts; inventory updates must not edit a confirmed profile.
        prefixes=('account','auth_','users','product_','consent','ownership','legacy_owner','google_oidc')
        tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0].startswith(prefixes)]
        return {table:[tuple(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in tables}


def seed_biology_profile(state):
    from wahojobs.persistent_profiles import TrustedPrincipalContext,CreatePersistentProfileCommand,ConfirmedAboutYouTextSourceDraft
    from wahojobs.persistent_profiles_repository import create_persistent_profile
    principal=TrustedPrincipalContext(principal_id=state.principal_id,environment_namespace='test',principal_type='account_native',lifecycle_status='active',claim_policy='account_native',exclusive_account_binding=True,eligibility_mode='account_native',active_owner_binding=True)
    now=state.clock()
    command=CreatePersistentProfileCommand.prepare(principal=principal,canonical_profile_v2=DESCRIPTION['profile'],sources=(ConfirmedAboutYouTextSourceDraft('Approved synthetic Portugal biology profile, not real candidate data.',now),),normalizer_version='approved_cohort_v2',reviewer_version=None,actor_type='authenticated_user',reason_code='profile.create',idempotency_key='refresh-app-profile',accepted_at=now)
    with closing(get_connection(state.database_path)) as conn:
        state.profile_id=create_persistent_profile(conn,command).profile_id


class LocalInventoryAppTests(unittest.TestCase):
    def test_configured_https_app_reads_refreshed_details_and_preserves_accounts(self):
        with temporary_browser_login_state(port=8794,seed_existing_profile=False) as state, loopback_and_in_memory_provider_only(), \
                patch('wahojobs.tracking.service.tracking_openai_client',side_effect=AssertionError('model forbidden')):
            from tests.google_oidc_gateway_test_support import ManualClock
            state.clock=ManualClock(datetime.fromisoformat('2026-09-05T13:00:00+00:00'))
            seed_sources(state.database_path);seed_biology_profile(state)
            before=account_state(state.database_path)
            for source in ('alignerr','micro1'):
                run_saved(state.database_path,source,[c for c in CASES if c['provider']==source])
            # Captured talent body with synthetic lifecycle envelope, explicitly
            # distinct from the two complete captured Mercor regression records.
            network=next(r for r in json.loads((ROOT/'card_source_wording.json').read_text(encoding='utf-8')) if r['job_id']==1039)
            talent=dict(deepcopy(DESCRIPTION['record']),listingId=network['external_id'],title='Biologist Talent Network',description=network['body'],commitment='part-time')
            run_saved(state.database_path,'mercor',records=[DESCRIPTION['record'],GEOGRAPHY['record'],talent])
            self.assertEqual(account_state(state.database_path),before)
            document=json.loads(state.configuration_path.read_text());self.assertEqual(Path(document['database_path']),state.database_path)
            runtime=build_durable_google_login_runtime(state.configuration_path,_clock=state.clock,_gateway_factory=state.gateway_factory)
            try:
                with running_https_production_launcher_app(runtime):
                    cookies={}
                    def request(method,target,body=None):
                        headers=[('Cookie',cookie_header(cookies))] if cookies else []
                        if method=='POST':headers.extend([('Origin',state.public_origin),('Sec-Fetch-Site','same-origin'),('Content-Type','application/x-www-form-urlencoded'),('Content-Length',str(len(body)))])
                        result=https_request(state,method,target,headers=tuple(headers),body=body,response_read_timeout_seconds=45)
                        cookies.update({k:v for k,v in cookie_values(result).items() if v})
                        return result
                    self.assertEqual(request('GET','/find-matches').status,401)
                    self.assertEqual(request('GET','/login').status,200)
                    started=request('POST','/auth/google/start',form_body(csrf=cookies['__Host-wahojobs_login_csrf']))
                    self.assertEqual(started.status,303)
                    callback=provider_callback_for(state,started.header_values('Location')[0])
                    target=urlsplit(callback);self.assertEqual(request('GET',target.path+'?'+target.query).status,303)
                    matches=request('GET','/find-matches');self.assertEqual(matches.status,200)
                    self.assertIn(b'Biologist Talent Network',matches.body)
                    self.assertIn(b'future consideration',matches.body)
                    self.assertNotIn(DESCRIPTION['record']['title'],unescape(matches.body.decode()))
                    with closing(get_connection(state.database_path)) as conn:
                        jobs=[dict(r) for r in conn.execute('SELECT * FROM jobs')]
                    for case in CASES:
                        row=next(r for r in jobs if r['external_id']==case['external_id'])
                        path=f"/job/opportunity-{row['canonical_opportunity_id']}?variant={row['id']}"
                        detail=request('GET',path)
                        self.assertEqual(detail.status,200,(path,detail.status))
                        self.assertIn(case['title'],unescape(detail.body.decode()))
                        self.assertIn(case['url'],unescape(detail.body.decode()))
                        self.assertIn(b'Qualifications' if case['provider']=='micro1' else b'Requirements' if case['rank']==3 else b'Who You Are',detail.body)
                        if case['rank']==4:
                            self.assertIn(b'Based in the U.S., Canada, U.K., Australia, or New Zealand',detail.body)
                            self.assertNotIn(path.encode(),matches.body)
                        if case['rank']==1:self.assertIn(path.encode(),matches.body)
                    row=next(r for r in jobs if r['external_id']==DESCRIPTION['record']['listingId'])
                    detail=request('GET',f"/job/opportunity-{row['canonical_opportunity_id']}?variant={row['id']}")
                    self.assertEqual(detail.status,200);self.assertIn(b'U.S. only',detail.body)
                    unauth=https_request(state,'GET',f"/job/opportunity-{row['canonical_opportunity_id']}?variant={row['id']}")
                    self.assertEqual(unauth.status,401)
                    self.assertEqual(request('POST','/job/opportunity-1',b'').status,405)
                    # Offline refresh must refuse this configured running target.
                    from wahojobs.crawler.pipeline import run_crawl
                    from wahojobs.database_lifetime_ownership import DatabaseLifetimeOwnershipError
                    with self.assertRaises(DatabaseLifetimeOwnershipError):run_crawl('mercor',db_path=state.database_path)
            finally:runtime.close()

    def test_old_context_cannot_resurrect_geography_after_ordinary_refresh(self):
        with loopback_and_in_memory_provider_only(),patch('wahojobs.tracking.service.tracking_openai_client',side_effect=AssertionError('model forbidden')):
            f=SyntheticMatcherFixture();self.addCleanup(f.close)
            f.profile=deepcopy(DESCRIPTION['profile']);f.now=datetime.fromisoformat(DESCRIPTION['observed_at'])
            seed_sources(f.path)
            record=deepcopy(DESCRIPTION['record'])
            # Synthetic before-state removes only the captured explicit clause.
            record['description']=record['description'].replace('- U.S. only','')
            run_saved(f.path,'mercor',records=[record])
            before=f.get();self.assertEqual(before.status,200)
            run=f.last_run();context=run.recommendation_context
            self.assertIn(record['title'],unescape(before.body.decode()))
            run_saved(f.path,'mercor',records=[DESCRIPTION['record']])
            after=f.get('/find-matches?run='+run.match_run_id)
            self.assertEqual(after.status,200);self.assertNotIn(record['title'],unescape(after.body.decode()))
            self.assertIsNot(f.last_run().recommendation_context,context)
            from wahojobs.authenticated_profile_matches import _primary_presentation_matches
            ids=[m['job_id'] for m in _primary_presentation_matches(f.last_run().recommendation_context)]
            self.assertEqual(f.get().status,200)
            self.assertEqual(ids,[m['job_id'] for m in _primary_presentation_matches(f.last_run().recommendation_context)])


if __name__=='__main__':unittest.main()
