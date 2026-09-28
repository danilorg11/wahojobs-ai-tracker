"""Synthetic exact-page controls based on retained anonymous public Mercor evidence."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.message import Message
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from wahojobs import mercor_availability as availability
from wahojobs.crawler import local_inventory, staged_observation
from wahojobs.crawler.providers import mercor
from wahojobs.daily_source_policy import daily_source, observed_mercor_public_jobs, validate_request, default_sources, POLICY
from wahojobs.db.repository import install_base_schema, create_crawl_run, finish_crawl_run, verify_job_source_acceptance_integrity
from wahojobs.tracking.service import track_crawl_result
from scripts.profile_match_digest import get_active_rows

OLD = "2026-09-01T12:00:00+00:00"
START = "2026-09-27T12:00:00+00:00"
AT = "2026-09-27T12:00:01+00:00"
END = "2026-09-27T12:00:02+00:00"


def page(identity="list_old", state="open", *, role_changes=None, visible=None, url=None):
    url = url or availability.public_job_url(identity, f"https://work.mercor.com/jobs/{identity}/test-role")
    role = dict(listingId=identity, status="active", deletedAt=None,
                isPrivate=state == "closed", disableApplications=state == "closed")
    role.update(role_changes or {})
    props = dict(role=role, closedListing=None, candidateStatus=None, nextSeoProps={"canonical": url})
    data = dict(page=availability.PAGE_ROUTE, query=dict(listingId=identity, slug=url.split("/")[5:]),
                props={"pageProps": props})
    control = ('<a href="/explore">' + availability.CLOSED_MESSAGE + " Please visit Explore.</a>"
               if state == "closed" else '<button disabled="">Apply now</button>')
    return ('<!doctype html><link rel="canonical" href="'+url+'"><script id="__NEXT_DATA__" type="application/json">'
            +json.dumps(data)+"</script>"+(control if visible is None else visible)).encode()


class Response:
    def __init__(self, url, body, status=200, location=None, content_type="text/html"):
        self.url, self.body, self.status = url, body, status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if location is not None: self.headers["Location"] = location
    def geturl(self): return self.url
    def read(self, maximum): return self.body[:maximum]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None): return datetime.fromisoformat(AT)


@contextmanager
def transport(responses):
    opener = Mock()
    opener.open.side_effect = responses
    with patch.object(availability, "build_opener", return_value=opener), \
         patch.object(availability, "_timestamp", return_value=START), \
         patch.object(local_inventory, "datetime", FixedDatetime):
        yield opener


class PageTests(unittest.TestCase):
    def inspect(self, raw, identity="list_old"):
        return availability.inspect_public_page(raw, listing_id=identity,
            final_url=f"https://work.mercor.com/jobs/{identity}/test-role").state

    def test_open_and_explicit_closed_ignore_hydration_disabled_button(self):
        self.assertEqual(self.inspect(page()), "open")
        self.assertEqual(self.inspect(page(state="closed")), "closed")

    def test_no_closure_from_privacy_missing_controls_or_untyped_flag(self):
        for raw in (page(role_changes={"isPrivate": True}),
                    page(state="closed", visible=""),
                    page(state="closed", role_changes={"disableApplications": "true"}),
                    page(state="closed", role_changes={"disableApplications": False}),
                    page(state="closed", visible='<div hidden><a href="/explore">'+availability.CLOSED_MESSAGE+'</a></div>'),
                    page(state="closed", visible='<button>Apply now</button>')):
            self.assertEqual(self.inspect(raw), "indeterminate")

    def test_identity_canonical_duplicates_and_challenge_cannot_qualify(self):
        raw = page()
        for value in (raw.replace(b'"listingId": "list_old"', b'"listingId": "list_other"', 1),
                      raw+b'<script id="__NEXT_DATA__">{}</script>',
                      raw.replace(b'rel="canonical"', b'rel="other"'),
                      b"<html>Challenge</html>",
                      raw.replace(b'"disableApplications": false', b'"disableApplications": false, "disableApplications": true')):
            self.assertEqual(self.inspect(value), "indeterminate")

    def test_exact_redirect_is_bounded_and_retained(self):
        start = availability.public_job_url("list_old")
        final = start+"/test-role"
        events = []
        with transport([Response(start,b"",308,"/jobs/list_old/test-role"), Response(final,page())]) as opener:
            result = availability.observe_public_job("list_old", audit_sink=events.append)
        self.assertEqual(result.decision.state,"open")
        self.assertEqual(opener.open.call_count,2)
        self.assertEqual([e["event"] for e in events], ["request","response","request","response","availability_decision"])

    def test_no_cross_identity_redirect_or_third_request_or_retry(self):
        start = availability.public_job_url("list_old")
        for responses in (
            [Response(start,b"",308,"/jobs/list_other/test-role")],
            [Response(start,b"",308,start+"/first"),Response(start+"/first",b"",308,start+"/second")],
            [Response(start,b"denied",403)],
            [Response(start,b"gone",404)],
            [URLError("offline")],
        ):
            with transport(responses) as opener:
                result = availability.observe_public_job("list_old", audit_sink=lambda e:None)
            self.assertEqual(result.decision.state,"indeterminate")
            self.assertEqual(opener.open.call_count,len(responses))

    def test_policy_needs_scoped_known_identity_and_defaults_do_not_widen(self):
        self.assertEqual(default_sources()["mercor"]["http_max"],1)
        self.assertEqual(POLICY["mercor"]["http_max"],201)
        with daily_source("mercor"):
            with self.assertRaises(ValueError): validate_request(Request(availability.public_job_url("list_old")))
            with observed_mercor_public_jobs(["list_old"]):
                validate_request(Request(availability.public_job_url("list_old")))
                with self.assertRaises(ValueError): validate_request(Request(availability.public_job_url("list_unknown")))
                with self.assertRaises(ValueError): validate_request(Request("https://work.mercor.com/jobs/list_old?auth=x"))

    def test_retention_failure_stops_before_second_dispatch(self):
        start = availability.public_job_url("list_old")
        def audit(event):
            if event["event"] == "response": raise OSError("retention failed")
        with transport([Response(start,b"",308,start+"/test-role")]) as opener:
            with self.assertRaisesRegex(OSError,"retention"):
                availability.observe_public_job("list_old",audit_sink=audit)
        self.assertEqual(opener.open.call_count,1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:");self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close);install_base_schema(self.db)
        self.company=self.db.execute("""INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy)
            VALUES('Mercor','mercor',?,'core','live_feed','count_live')""",(mercor.MERCOR_ENDPOINT,)).lastrowid
        fixture=json.loads((Path(__file__).parent/"fixtures/mercor_observation_contract.json").read_text())["listing"]
        self.listing=dict(fixture, listingId="list_old", payRate="$50", payRateFrequency="hourly")
        self.publish(mercor.parse_mercor_observations({"listings":[self.listing,dict(self.listing,listingId="list_other")]}),OLD,OLD)
        self.known=availability.load_known_jobs(self.db,self.company)
        self.before={row["external_id"]:dict(row) for row in self.db.execute("SELECT * FROM jobs")}
        self.contents=[dict(row) for row in self.db.execute("SELECT * FROM job_source_contents")]
        self.acceptances=[dict(row) for row in self.db.execute("SELECT * FROM job_source_content_acceptances")]

    def publish(self,result,start=START,end=END):
        run=create_crawl_run(self.db,self.company,start)
        summary=track_crawl_result(self.db,self.company,run,result,end,model_enrichment=False)
        finish_crawl_run(self.db,run,summary,end,status="partial",error_message="partial public catalog")
        self.db.commit()
        return summary,run

    def collect(self,state="open",*,limit=2):
        start=availability.public_job_url("list_old");final=start+"/test-role"
        responses=[Response(start,b"",308,final),Response(final,page(state=state))]
        events=[]
        with daily_source("mercor"),availability.known_public_jobs(self.known[:1]), \
             local_inventory.refresh_request_budget(http_limit=limit,detail_limit=0,audit_sink=events.append) as budget, \
             transport(responses) as opener:
            result=availability.augment_result(mercor.parse_mercor_observations({"listings":[]}))
        return result,events,budget,opener

    def test_positive_renews_original_clock_and_preserves_content_and_pay(self):
        result,events,budget,_=self.collect()
        self.assertEqual(budget.summary()["http_transactions"],2)
        self.assertEqual(sum("raw_response" in e for e in events),2)
        self.assertFalse(result.snapshot_complete)
        summary,run=self.publish(result)
        self.assertEqual(summary.jobs_removed,0)
        self.assertEqual([dict(row) for row in self.db.execute("SELECT * FROM job_source_contents")],self.contents)
        self.assertEqual([dict(row) for row in self.db.execute("SELECT * FROM job_source_content_acceptances")],self.acceptances)
        rows={row["external_id"]:dict(row) for row in self.db.execute("SELECT * FROM jobs")}
        self.assertEqual(rows["list_old"]["last_seen_at"],AT)
        self.assertEqual(rows["list_other"],self.before["list_other"])
        capture=dict(self.db.execute("SELECT * FROM job_source_content_captures ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(capture["record_promotion_contract_id"],availability.POSITIVE_CONTRACT_ID)
        self.assertEqual(capture["observed_at"],AT)
        self.assertEqual(capture["promotion_decision"],"held_degraded")
        verify_job_source_acceptance_integrity(self.db,rows["list_old"]["id"])
        active=next(dict(row) for row in get_active_rows(self.db) if row["job_id"]==rows["list_old"]["id"])
        self.assertEqual(active["latest_successful_source_run_at"],AT)
        self.assertEqual(active["source_run_id"],run)

    def test_negative_closes_only_exact_known_job_without_renewing_last_seen(self):
        result,_,_,_=self.collect("closed")
        summary,run=self.publish(result)
        self.assertEqual(summary.jobs_removed,1)
        self.assertFalse(summary.removals_authorized)
        rows={row["external_id"]:dict(row) for row in self.db.execute("SELECT * FROM jobs")}
        self.assertEqual(rows["list_old"]["is_active"],0)
        self.assertEqual(rows["list_old"]["last_seen_at"],OLD)
        self.assertEqual(rows["list_old"]["removed_at"],AT)
        self.assertEqual(rows["list_other"],self.before["list_other"])
        self.assertEqual([dict(row) for row in self.db.execute("SELECT * FROM job_source_contents")],self.contents)
        self.assertEqual(self.db.execute("SELECT created_at FROM job_events WHERE event_type='removed'").fetchone()[0],AT)

    def test_budget_exhaustion_is_unconfirmed_not_false_close(self):
        result,_,budget,opener=self.collect("closed",limit=1)
        summary,_=self.publish(result)
        self.assertEqual(budget.summary()["http_transactions"],1)
        self.assertEqual(opener.open.call_count,1)
        self.assertFalse(result.source_records)
        self.assertEqual(summary.jobs_removed,0)
        self.assertIn("1 remain unconfirmed"," ".join(result.warnings))
        self.assertEqual({row["external_id"]:dict(row) for row in self.db.execute("SELECT * FROM jobs")},self.before)

    def test_codec_roundtrip_and_forged_negative_fail_before_catalog_writes(self):
        result,_,_,_=self.collect("closed")
        self.assertEqual(asdict(staged_observation.decode_result(json.loads(json.dumps(asdict(result))))),asdict(result))
        record=result.source_records[0]
        for changes in (dict(known_job_id=self.before["list_other"]["id"]),
                        dict(listing_id="list_invented"),dict(body_sha256="0"*64),
                        dict(observed_at="2027-01-01T00:00:00+00:00"),
                        dict(raw_html=record.raw_html.replace(availability.CLOSED_MESSAGE,"Unavailable"))):
            forged=replace(result,source_records=(replace(record,**changes),))
            run=create_crawl_run(self.db,self.company,START)
            self.db.commit()
            with self.assertRaises(ValueError):
                with self.db:
                    track_crawl_result(self.db,self.company,run,forged,END,model_enrichment=False)
            self.assertEqual({row["external_id"]:dict(row) for row in self.db.execute("SELECT * FROM jobs")},self.before)

    def test_forged_unbound_positive_cannot_renew_or_insert(self):
        result,_,_,_=self.collect()
        with self.assertRaisesRegex(ValueError,"unbound_positive"):
            self.publish(replace(result,source_records=()))
        self.db.rollback()
        self.assertEqual({row["external_id"]:dict(row) for row in self.db.execute("SELECT * FROM jobs")},self.before)

    def test_changed_known_state_since_collection_aborts_publication(self):
        result,_,_,_=self.collect("closed")
        self.db.execute("UPDATE jobs SET last_seen_at=? WHERE external_id='list_old'",(END,))
        self.db.commit()
        with self.assertRaisesRegex(ValueError,"known_record_changed"): self.publish(result)

    def test_staged_collection_seals_raw_pages_and_rejects_tampering(self):
        from wahojobs import evidence_maintenance as maintenance
        from wahojobs.crawler import pipeline
        start=availability.public_job_url("list_old");final=start+"/test-role"
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);journal=root/"journal";staged=root/"staged"
            responses=[Response(start,b"",308,final),Response(final,page(state="closed"))]
            with transport(responses),patch.object(staged_observation,"timestamp",side_effect=[START,END]), \
                 patch.dict(pipeline.CRAWLERS,mercor=lambda _:availability.augment_result(mercor.parse_mercor_observations({"listings":[]}))):
                plan_id=staged_observation.collect("mercor",mercor.MERCOR_ENDPOINT,staged,
                    run_id="exact",code_commit="a"*40,http_max=2,journal_root=journal,
                    known_jobs=self.known[:1])
            with patch.object(staged_observation,"timestamp",return_value=END):
                value,report=staged_observation.load(staged,"mercor",run_id="exact",code_commit="a"*40,journal_root=journal)
                self.assertEqual(value.result.source_records[0].state,"closed")
                self.assertTrue(report["plan"]["known_job_scope_sha256"])
                with self.assertRaisesRegex(ValueError,"journal_response_unbound"):
                    availability.validate_journal_records(value.result,{"events":[]})
                (next((journal/plan_id).glob("*.raw"))).write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError,"integrity"):
                    staged_observation.load(staged,"mercor",run_id="exact",code_commit="a"*40,journal_root=journal)

    def test_pipeline_rolls_back_all_inventory_if_any_candidate_is_forged(self):
        from wahojobs.crawler import pipeline
        result,_,_,_=self.collect("closed")
        catalog=mercor.parse_mercor_observations({"listings":[dict(self.listing,listingId="list_new")]})
        candidate=catalog.jobs[0]
        attestation=replace(candidate.record_promotion_attestation,
            authority_evidence=dict(candidate.record_promotion_attestation.authority_evidence,is_private=True))
        result=replace(result,jobs=[replace(candidate,record_promotion_attestation=attestation)],
            raw_record_count=result.raw_record_count+1,normalized_record_count=1)
        with patch.object(pipeline,"get_connection",return_value=self.db), \
             patch.dict(pipeline.CRAWLERS,mercor=lambda _:result), \
             patch.object(pipeline,"utc_now",side_effect=[START,END,END]):
            with self.assertRaises(ValueError): pipeline.run_crawl("mercor")
        self.assertEqual({row["external_id"]:dict(row) for row in self.db.execute("SELECT * FROM jobs")},self.before)
        self.assertEqual(self.db.execute("SELECT count(*) FROM job_events WHERE event_type='removed'").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()[0],"failed")

    def test_more_than_100_missing_ids_and_failed_pages_are_all_retained_pending(self):
        known=[]
        for index in range(102):
            identity=f"list_missing_{index}"
            known.append(dict(self.known[0],id=index+1,external_id=identity,
                source_hash=sha256(identity.encode()).hexdigest(),url=availability.public_job_url(identity)))
        def denied(request, timeout): return Response(request.full_url,b"blocked",403)
        events=[]
        with daily_source("mercor"),availability.known_public_jobs(known), \
             local_inventory.refresh_request_budget(http_limit=201,detail_limit=0,audit_sink=events.append),transport(denied) as opener:
            result=availability.augment_result(mercor.parse_mercor_observations({"listings":[]}))
        self.assertEqual(opener.open.call_count,100)
        self.assertFalse(result.source_records)
        pending=next(event for event in events if event["event"]=="pending_qualification")
        self.assertEqual(len(pending["identities"]),102)
        self.assertIn("102 remain unconfirmed"," ".join(result.warnings))
        first={call.args[0].full_url for call in opener.open.call_args_list}
        with daily_source("mercor"),availability.known_public_jobs(known), \
             local_inventory.refresh_request_budget(http_limit=201,detail_limit=0,audit_sink=lambda _:None), \
             transport(denied) as second,patch.object(availability,"_timestamp",return_value="2026-09-28T12:00:00+00:00"):
            availability.augment_result(mercor.parse_mercor_observations({"listings":[]}))
        following={call.args[0].full_url for call in second.open.call_args_list}
        self.assertEqual(len(first),100);self.assertEqual(len(following),100)
        self.assertEqual(len(first|following),102)

    def test_exact_closed_only_summary_clears_expired_cohorts(self):
        from wahojobs import daily_inventory as daily, evidence_maintenance as maintenance
        before=maintenance.inspect_source(self.db,"mercor",datetime.fromisoformat(END),catalog_only=True)
        responses=[]
        for row in self.known:
            start=row["url"];final=start+"/test-role"
            responses.extend([Response(start,b"",308,final),
                Response(final,page(identity=row["external_id"],state="closed",url=final))])
        events=[]
        with daily_source("mercor"),availability.known_public_jobs(self.known), \
             local_inventory.refresh_request_budget(http_limit=4,detail_limit=0,audit_sink=events.append),transport(responses):
            result=availability.augment_result(mercor.parse_mercor_observations({"listings":[]}))
        summary,_=self.publish(result)
        after=maintenance.inspect_source(self.db,"mercor",datetime.fromisoformat(END),catalog_only=True)
        plan=dict(plan_id="closed-only",config=dict(providers=["mercor"],http_limit=4),sources=[before])
        report=dict(events=[*[dict(event="source_transport",data=event) for event in events],
            dict(event="collected_result",data=dict(result=asdict(result))),
            dict(event="operation_result",data=dict(operation="catalog:mercor",
                result=dict(summary=asdict(summary),after=after)))])
        row=daily.summarize_source(plan,report,datetime.fromisoformat(START),datetime.fromisoformat(END))
        self.assertTrue(row["qualifying_observation"])
        self.assertEqual(row["confirmed_closed"],2)
        self.assertEqual(row["uncertain"],0)
        self.assertEqual(row["last_qualifying_verification"],AT)
        self.assertEqual(row["cohorts"],[])
        self.assertEqual(row["stale"],0)
        self.assertIsNone(row["next_verification_deadline"])
        previous=dict(observed=2,cohorts=[dict(verified_at=OLD,records=2)],
            last_qualifying_verification=OLD,next_verification_deadline="2026-09-04T12:00:00+00:00")
        merged=daily.merge_source_history(row,previous)
        self.assertEqual(merged["cohorts"],[])
        self.assertEqual(merged["stale"],0)
        self.assertIsNone(merged["next_verification_deadline"])
        # Unbound closure counts alone cannot establish a qualifying check.
        report["events"]=[event for event in report["events"] if event["event"]!="collected_result"]
        self.assertFalse(daily.summarize_source(plan,report,datetime.fromisoformat(START),
            datetime.fromisoformat(END))["qualifying_observation"])

    def test_disabled_catalog_record_is_not_a_positive_or_absence_closure(self):
        result=mercor.parse_mercor_observations({"listings":[dict(self.listing,disableApplications=True)]})
        self.assertEqual(result.jobs,[])
        summary,_=self.publish(result)
        self.assertEqual(summary.jobs_removed,0)


if __name__=="__main__":unittest.main()

