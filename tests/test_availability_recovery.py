"""Targeted attempts cannot consume daily history or widen source authority."""
from datetime import datetime,timezone
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch,Mock
from wahojobs import daily_inventory as d,availability_recovery as a
from scripts import daily_inventory as cli

AT=datetime(2026,9,25,15,tzinfo=timezone.utc)
class TargetedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=dict(state_directory=str(self.root),sources=d.default_sources(),code_commit='a'*40,
            database=str(self.root/'product.sqlite3'),first_run_at='2026-09-23T06:00:00+00:00')
        self.cohorts={s:[dict(verified_at='2026-09-23T06:00:17+00:00',records=11)] for s in a.CAPS}
        self.p=patch.object(d,'_baseline_cohorts',return_value=self.cohorts);self.p.start();self.addCleanup(self.p.stop)
    def reserve(self,sources=None):return a.reserve(self.config,AT,sources or ['alignerr','mercor'])
    def test_duplicate_attempt_preserves_daily_failure_and_claims(self):
        daily=self.root/'runs/20260925T060000Z/run.json';d.write_json(daily,{'outcome':'recovery_failed'})
        before=daily.read_bytes();r=self.reserve();target=self.root/'runs'/r['run_id']
        a.audit(target,'mercor',dict(event='request',kind='catalog',observed_at=AT.isoformat()))
        ledger=(target/'availability-http-ledger.json').read_bytes()
        self.assertIsNone(self.reserve());self.assertEqual(daily.read_bytes(),before)
        self.assertEqual((target/'availability-http-ledger.json').read_bytes(),ledger)
    def test_no_need_disabled_or_unapproved_source_makes_no_reservation(self):
        self.cohorts['mercor']=[dict(verified_at=AT.isoformat(),records=377),
            dict(verified_at='2026-09-21T23:00:39+00:00',records=10),
            dict(verified_at='2026-09-18T13:07:32+00:00',records=39)]
        with self.assertRaisesRegex(ValueError,'freshness_need'):self.reserve(['mercor'])
        with self.assertRaisesRegex(ValueError,'scope'):self.reserve(['micro1'])
        self.config['sources']['alignerr']['enabled']=False
        with self.assertRaisesRegex(ValueError,'disabled'):self.reserve(['alignerr'])
        self.assertFalse((self.root/'runs').exists())
    def test_cumulative_budget_binds_sources_release_and_physical_attempts(self):
        r=self.reserve();target=self.root/'runs'/r['run_id'];event=dict(event='request',kind='catalog')
        for _ in range(100):a.audit(target,'alignerr',event)
        with self.assertRaisesRegex(ValueError,'consumed'):a.audit(target,'alignerr',event)
        a.audit(target,'mercor',event)
        with self.assertRaisesRegex(ValueError,'consumed'):a.audit(target,'mercor',event)
        with self.assertRaisesRegex(ValueError,'scope'):a.audit(target,'micro1',event)
        ledger=d.read_json(target/'availability-http-ledger.json');self.assertEqual(len(ledger['attempts']),101)
        ledger['code_commit']='b'*40;d.write_json(target/'availability-http-ledger.json',ledger)
        with self.assertRaisesRegex(ValueError,'scope'):a.audit(target,'mercor',event)
    def test_unpersisted_reservation_prevents_transport(self):
        from wahojobs.crawler.local_inventory import refresh_request_budget
        from urllib.request import Request
        r=self.reserve();target=self.root/'runs'/r['run_id'];transport=Mock()
        with patch.object(d,'write_json',side_effect=OSError('disk full')):
            with refresh_request_budget(http_limit=1,detail_limit=0,audit_sink=lambda e:a.audit(target,'mercor',e)) as budget:
                with self.assertRaises(OSError):
                    budget.reserve(Request('https://aws.api.mercor.com/work/listings-explore-page'));transport()
        transport.assert_not_called()
    def test_preparation_and_finalization_leave_unselected_state_untouched(self):
        r=self.reserve(['mercor']);target=self.root/'runs'/r['run_id'];other=self.root/'appen-state.json'
        d.write_json(other,dict(outcome='complete',sentinel=True));before=other.read_bytes()
        plan={s:dict(state='due',reason=None,next_eligible_at=None) for s in d.SOURCES}
        with patch.object(d,'coverage_plan',return_value=plan):d.collect_phase(self.config,r['run_id'],'prepare')
        self.assertEqual(d.read_json(target/'coverage-plan.json')['appen']['state'],'not_targeted')
        self.assertFalse((target/'appen.json').exists())
        d.finish_run_sources(self.config,r)
        self.assertEqual(set(r['sources']),{'mercor'});self.assertEqual(other.read_bytes(),before)
    def test_targeted_publication_failure_reuses_independent_recovery(self):
        ops=Mock();ops.collect.return_value=True;ops.publish.side_effect=TimeoutError('fixture')
        with patch.object(d,'now',return_value=AT):
            result=cli.supervise(self.config,self.root/'policy','manual',operations=ops,availability_sources=['mercor'])
        ops.restore.assert_called_once_with(d.RECOVERY_SECONDS)
        self.assertEqual(result['outcome'],'failed');self.assertTrue(result['normal_service_resumed'])
        self.assertEqual(set(result['sources']),{'mercor'})

from tests import test_daily_source_coverage as coverage
from tests.test_daily_source_coverage import Transport,offline
from tests.evidence_maintenance_support import T0
from datetime import timedelta
class TargetedIntegrationTests(unittest.TestCase):
    setUp=coverage.CoverageIntegrationTests.setUp
    def test_real_targeted_collection_and_publication_share_one_ledger(self):
        at=T0+timedelta(days=1);cohorts={s:[dict(verified_at=(at-timedelta(hours=60)).isoformat(),records=1)] for s in a.CAPS}
        transport=Transport();before=d.protected_domains(self.db)
        other=Path(self.config['state_directory'])/'appen-state.json'
        d.write_json(other,dict(outcome='complete',sentinel=True));old=other.read_bytes()
        with offline(at,transport),patch.object(d,'_baseline_cohorts',return_value=cohorts):
            receipt=a.reserve(self.config,at,['alignerr','mercor']);run_id=receipt['run_id']
            target=Path(self.config['state_directory'])/'runs'/run_id
            for phase in ('prepare','collect-alignerr','collect-mercor','backup','publish-alignerr','publish-mercor','finish'):
                d.collect_phase(self.config,run_id,phase)
            d.finish_run_sources(self.config,receipt)
        self.assertEqual({s for s,_,_ in transport.calls},set(a.CAPS))
        self.assertEqual(len(d.read_json(target/'availability-http-ledger.json')['attempts']),len(transport.calls))
        self.assertEqual(before,d.protected_domains(self.db));self.assertEqual(other.read_bytes(),old)
        self.assertEqual(set(receipt['sources']),set(a.CAPS))
        mercor=receipt['sources']['mercor'];self.assertEqual(mercor['outcome'],'partial_individual')
        self.assertEqual(mercor['confirmed_closed'],0);self.assertEqual(mercor['publication_requests_used'],0)

if __name__=='__main__':unittest.main()
