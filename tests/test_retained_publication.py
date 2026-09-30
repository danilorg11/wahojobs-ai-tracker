"""No-network same-day recovery with immutable observation dates and claims."""
from contextlib import closing, contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
import sqlite3
import unittest
from unittest.mock import Mock, patch

from tests import test_staged_daily_inventory as fixtures
from tests.test_daily_source_coverage import Transport, offline
from wahojobs import evidence_maintenance as maintenance, retained_publication as recovery
from wahojobs.crawler import pipeline, staged_observation as staged
from wahojobs.database_lifetime_ownership import (
    acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)


class RetainedRecoveryTests(unittest.TestCase):
    setUp=fixtures.StagedIntegrationTests.setUp
    prepare=fixtures.StagedIntegrationTests.prepare

    def grant_fixture(self, cross_version=False):
        at,target=self.prepare('retained')
        with offline(at,Transport()),patch.object(maintenance,'contract_fingerprint',
                return_value='sealed-origin-contract' if cross_version else maintenance.contract_fingerprint()):
            from wahojobs import daily_inventory as daily
            daily.collect_phase(self.config,'retained','collect-appen')
            value,report=staged.load(target,'appen',run_id='retained',code_commit=self.config['code_commit'],journal_root=self.journal)
        later=at+timedelta(hours=10)
        origin=self.root/self.config['code_commit'];origin.mkdir()
        grant=dict(authorized=True,reviewed_run_id='reviewed-recovery',issued_at=later.isoformat(),
            expires_at=(later+timedelta(hours=2)).isoformat(),database=maintenance.database_identity(self.db),
            origin_release=str(origin),origin_commit=self.config['code_commit'],source_contract_sha256='reviewed-source',
            current_contract_fingerprint=maintenance.contract_fingerprint(),collections={value.collection_plan_id:dict(
                source=value.source,run_id='retained',contract_fingerprint=report['plan']['contract_fingerprint'],
                journal_hash=value.journal_hash,started_at=value.started_at,completed_at=value.completed_at)})
        return later,target,grant,value,report

    @contextmanager
    def scope(self,grant,lease,later):
        with ExitStack() as stack:
            clock=stack.enter_context(patch.object(recovery,'datetime',wraps=datetime))
            clock.now.return_value=later
            stack.enter_context(patch.object(recovery,'source_contract',return_value='reviewed-source'))
            stack.enter_context(recovery.retained_publication_scope(grant,database=self.db,ownership=lease))
            yield

    def test_exact_recovery_uses_original_times_and_no_collection_or_model(self):
        later,target,grant,value,_=self.grant_fixture()
        args=dict(run_id='retained',code_commit=self.config['code_commit'],journal_root=self.journal)
        transport=Transport()
        with offline(later,transport),self.assertRaisesRegex(ValueError,'not_admissible'):
            staged.load(target,'appen',**args)
        lease=acquire_database_lifetime_ownership(self.db,role=ROLE_OFFLINE_OPERATOR)
        try:
            with offline(later,transport),self.scope(grant,lease,later),patch.dict(pipeline.CRAWLERS,
                    appen=Mock(side_effect=AssertionError('no new collection'))):
                observation,_=staged.load(target,'appen',**args,consume=True)
                plan=maintenance.build_plan(self.db,['appen'],now=later,http_limit=1,detail_limit=0,
                    details=None,phase='source',daily_discovery=True,staged_baseline=True)
                result=maintenance.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,
                    ownership=lease,observation=observation)
                self.assertEqual(result['events'][-1]['data']['request_usage']['http_transactions'],0)
                with self.assertRaisesRegex(ValueError,'attempted_or_superseded'):
                    staged.validate_observation(observation,'appen')
        finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=self.db)
        self.assertEqual(transport.calls,[])
        with closing(sqlite3.connect(self.db)) as db:
            actual=db.execute("SELECT cr.started_at,cr.finished_at FROM crawl_runs cr JOIN companies c "
                "ON c.id=cr.company_id WHERE c.slug='appen' ORDER BY cr.id DESC LIMIT 1").fetchone()
        self.assertEqual(actual,(value.started_at,value.completed_at))

    def test_expired_widened_or_changed_contract_grants_fail(self):
        later,_,grant,_,_=self.grant_fixture()
        lease=acquire_database_lifetime_ownership(self.db,role=ROLE_OFFLINE_OPERATOR)
        try:
            for change in (dict(expires_at=later.isoformat()),
                    dict(expires_at=(later+timedelta(hours=3)).isoformat()),
                    dict(current_contract_fingerprint='changed'),dict(source_contract_sha256='changed'),
                    dict(authorized=False)):
                with self.subTest(change=change),self.assertRaises(ValueError):
                    with self.scope(dict(grant,**change),lease,later):pass
        finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=self.db)

    def test_wrong_sealed_hash_time_source_and_age_fail(self):
        later,_,grant,value,report=self.grant_fixture()
        lease=acquire_database_lifetime_ownership(self.db,role=ROLE_OFFLINE_OPERATOR)
        try:
            for change in (dict(journal_hash='0'*64),dict(started_at=later.isoformat()),dict(source='mercor')):
                bad=deepcopy(grant);bad['collections'][value.collection_plan_id].update(change)
                with self.subTest(change=change),offline(later,Transport()),self.scope(bad,lease,later),self.assertRaises(ValueError):
                    staged.validate_observation(value,'appen')
            stale=later+timedelta(hours=15);grant['issued_at']=stale.isoformat()
            grant['expires_at']=(stale+timedelta(hours=1)).isoformat()
            with offline(stale,Transport()),self.scope(grant,lease,stale),self.assertRaisesRegex(ValueError,'not_admissible'):
                staged.validate_observation(value,'appen')
        finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=self.db)

    def test_consumed_claim_and_newer_attempt_fail_without_writes(self):
        later,target,grant,value,report=self.grant_fixture()
        args=dict(run_id='retained',code_commit=self.config['code_commit'],journal_root=self.journal,consume=True)
        lease=acquire_database_lifetime_ownership(self.db,role=ROLE_OFFLINE_OPERATOR)
        try:
            with offline(later,Transport()),self.scope(grant,lease,later):
                staged.load(target,'appen',**args)
                before=self.db.read_bytes()
                with self.assertRaises(FileExistsError):staged.load(target,'appen',**args)
                self.assertEqual(self.db.read_bytes(),before)
                with closing(sqlite3.connect(self.db)) as db:
                    company=db.execute("SELECT id FROM companies WHERE slug='appen'").fetchone()[0]
                    from wahojobs.db.repository import create_crawl_run
                    create_crawl_run(db,company,later.isoformat());db.commit()
                with self.assertRaisesRegex(ValueError,'attempted_or_superseded'):
                    staged.validate_observation(value,'appen')
        finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=self.db)

    def test_cross_version_sealed_contract_requires_exact_grant(self):
        later,target,grant,value,report=self.grant_fixture(cross_version=True)
        args=dict(run_id='retained',code_commit=self.config['code_commit'],journal_root=self.journal)
        self.assertNotEqual(report['plan']['contract_fingerprint'],maintenance.contract_fingerprint())
        with offline(later,Transport()),self.assertRaisesRegex(ValueError,'completed_bound'):
            staged.load(target,'appen',**args)
        lease=acquire_database_lifetime_ownership(self.db,role=ROLE_OFFLINE_OPERATOR)
        try:
            with offline(later,Transport()),self.scope(grant,lease,later):
                self.assertTrue(recovery.compatible_collection(report))
                self.assertEqual(staged.load(target,'appen',**args)[0],value)
            for change in (dict(contract_fingerprint='wrong-origin-contract'),
                    dict(journal_hash='wrong-sealed-hash'),dict(source='mercor')):
                bad=deepcopy(grant);bad['collections'][value.collection_plan_id].update(change)
                with self.subTest(change=change),offline(later,Transport()),self.scope(bad,lease,later):
                    self.assertFalse(recovery.compatible_collection(report))
                    with self.assertRaisesRegex(ValueError,'completed_bound'):
                        staged.load(target,'appen',**args)
            bad=dict(grant,origin_commit='wrong-origin-commit')
            with self.assertRaises(ValueError):
                with self.scope(bad,lease,later):pass
        finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=self.db)

    def test_real_source_contract_preserves_only_reviewed_catalog_differences(self):
        first=self.root/'origin-source';second=self.root/'current-source'
        for directory in (first,second):
            (directory/'wahojobs/tracking').mkdir(parents=True)
            (directory/'wahojobs/crawler').mkdir()
            for name in ('tracking/service.py','crawler/parser.py','schema.sql'):
                (directory/'wahojobs'/name).write_text('same source contract')
        (first/'wahojobs/public_catalog_reader.py').write_text('old accepted catalog')
        (second/'wahojobs/public_catalog_reader.py').write_text('new accepted catalog')
        self.assertEqual(recovery.source_contract(first),recovery.source_contract(second))
        for name in ('tracking/service.py','crawler/parser.py','schema.sql'):
            with self.subTest(name=name):
                (second/'wahojobs'/name).write_text('changed protected contract')
                self.assertNotEqual(recovery.source_contract(first),recovery.source_contract(second))
                (second/'wahojobs'/name).write_text('same source contract')
