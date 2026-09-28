"""Versioned negative-only Mercor pages; retained v1 positives stay unchanged."""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wahojobs import mercor_availability as a, daily_inventory as daily, evidence_maintenance as maintenance
from wahojobs.crawler import local_inventory, staged_observation, pipeline
from wahojobs.crawler.providers import mercor
from wahojobs.daily_source_policy import daily_source
from tests import test_mercor_availability as prior
from tests.test_mercor_availability import Response, transport, START, AT, END, OLD, page


def v2_page(identity='list_old', *, archived=False, mutate=None, visible=None):
    url=a.public_job_url(identity)+'/test-role'
    role=dict(listingId=identity,title='Exact Original Role',status='archived' if archived else 'active',
        deletedAt=None,isPrivate=True,disableApplications=True)
    pool=dict(listingId='list_pool',title='Exact Talent Network',status='active',deletedAt=None,
        isPrivate=False,disableApplications=False,listingType='evergreen')
    props=dict(role=role,candidateStatus=None,nextSeoProps={'canonical':url},poolListing=None,closedListing=None)
    if archived:
        props.update(poolListing=dict(role=pool,candidateStatus=None),closedListing=dict(listingId=identity,
            title=role['title'],poolName=pool['title'],matchSource='domain',reason='archived'))
    data=dict(page=a.PAGE_ROUTE,query=dict(listingId=identity,slug=['test-role']),props={'pageProps':props})
    if mutate:mutate(data)
    body=('<div data-testid="closed-role-notice"><h3>Exact Original Role is no longer accepting applications</h3>'
        '<p>This talent network covers the same expertise and is open, so you can apply here instead.</p></div>'
        '<h1 data-test="listing-description-title">Exact Talent Network</h1>'
        '<div class="max-md:hidden"><div class="hidden w-[420px] md:flex">'
        '<button data-test="start-application-button" disabled>Start application</button></div></div>'
        '<button disabled>Apply now</button>' if archived else
        '<h1 data-test="listing-description-title">Exact Original Role</h1>'
        '<div class="max-md:hidden"><div class="hidden w-[420px] md:flex">'
        '<span>This listing is not accepting applications currently</span></div></div>')
    return ('<!doctype html><link rel="canonical" href="'+url+'"><script id="__NEXT_DATA__">'+json.dumps(data)+
        '</script>'+(body if visible is None else visible)).encode()


class NegativePageTests(unittest.TestCase):
    def inspect(self,raw):
        return a.inspect_closed_page_v2(raw,listing_id='list_old',final_url=a.public_job_url('list_old')+'/test-role').state

    def test_exact_disabled_and_archive_pool_are_v2_only(self):
        for archived in (False,True):
            raw=v2_page(archived=archived)
            self.assertEqual(self.inspect(raw),'closed')
            self.assertEqual(a.inspect_public_page(raw,listing_id='list_old',final_url=a.public_job_url('list_old')+'/test-role').state,'indeterminate')
        subdomain=v2_page(archived=True,mutate=lambda d:d['props']['pageProps']['closedListing'].update(matchSource='subdomain'))
        self.assertEqual(self.inspect(subdomain),'closed')
        self.assertEqual(self.inspect(v2_page(archived=True).replace(b'<button disabled>Apply now',
            b'<button class="md:hidden" disabled>Apply now')),'closed')
        self.assertEqual(self.inspect(page()),'indeterminate')
        self.assertEqual(self.inspect(page(state='closed')),'indeterminate')

    def test_private_disabled_requires_explicit_visible_notice_and_matching_title(self):
        raw=v2_page()
        for value in (raw.replace(b'<span>',b'<span hidden>'),raw.replace(b'<span>',b'<span aria-hidden="true">'),
                raw.replace(b'<span>',b'<span style="display: none">'),raw.replace(b'<span>',b'<span class="hidden">'),
                raw.replace(b'<span>',b'<span class="!hidden">'),
                raw.replace(b'"hidden w-[420px] md:flex',b'"hidden w-[420px] md:flex md:hidden'),
                raw.replace(b'<span>',b'<a href="/explore"><span>').replace(b'</span>',b'</span></a>'),
                raw.replace(b'<span>',b'<template><span>').replace(b'</span>',b'</span></template>'),
                raw.replace(b'currently',b'soon'),raw.replace(b'<h1 data-test="listing-description-title">Exact Original Role',b'<h1 data-test="listing-description-title">Other Role'),
                raw+b'<button>Apply now</button>',raw+b'<button>Start application</button>',
                raw+b'<a href="/apply/list_old">Apply</a>',
                raw.replace(b'"disableApplications": true',b'"disableApplications": false'),
                raw.replace(b'"disableApplications": true',b'"disableApplications": "true"'),
                raw.replace(b'"isPrivate": true',b'"isPrivate": false')):
            with self.subTest(value=value[-90:]):self.assertEqual(self.inspect(value),'indeterminate')
        # A true hidden control cannot contradict visible negative evidence.
        self.assertEqual(self.inspect(raw+b'<button hidden>Apply now</button>'),'closed')

    def test_archive_identity_pool_and_named_notice_must_all_agree(self):
        mutations=(lambda p:p['closedListing'].update(listingId='list_other'),
            lambda p:p['closedListing'].update(title='Other Role'),lambda p:p['closedListing'].update(reason='unknown'),
            lambda p:p['closedListing'].update(matchSource='unknown'),lambda p:p['closedListing'].update(poolName='Other Pool'),
            lambda p:p['poolListing']['role'].update(listingId='list_old'),
            lambda p:p['poolListing']['role'].update(status='archived'),
            lambda p:p['poolListing']['role'].update(isPrivate=True),
            lambda p:p['poolListing']['role'].update(disableApplications=True),
            lambda p:p['poolListing']['role'].update(listingType='project'),
            lambda p:p['poolListing']['role'].update(title='Other Pool'),
            lambda p:p['poolListing'].update(candidateStatus='applied'),lambda p:p.update(candidateStatus='applied'),
            lambda p:p['role'].update(deletedAt='2026-09-01'),lambda p:p['role'].update(isPrivate=1))
        for mutation in mutations:
            raw=v2_page(archived=True,mutate=lambda d:mutation(d['props']['pageProps']))
            self.assertEqual(self.inspect(raw),'indeterminate')
        raw=v2_page(archived=True)
        for value in (raw.replace(b'data-testid="closed-role-notice"',b'data-testid="other"'),
                raw.replace(b'data-testid="closed-role-notice"',b'data-testid="closed-role-notice" hidden'),
                raw.replace(b'<h3>',b'<h3 aria-hidden="true">'),
                raw.replace(b'<h3>',b'<script><h3>').replace(b'</h3>',b'</h3></script>'),
                raw.replace(b'<h3>',b'<template><h3>').replace(b'</h3>',b'</h3></template>'),
                raw.replace(b'Exact Original Role is no longer',b'Other Role is no longer'),
                raw.replace(b'This talent network covers',b'This project covers'),
                raw.replace(b'data-test="start-application-button"',b'data-test="start-application-button" data-listing-id="list_old"'),
                raw.replace(b'data-test="start-application-button"',b'data-test="start-application-button" hidden'),
                raw+b'<button>Apply now</button>',raw+b'<a href="/jobs/list_old/apply">Apply</a>',
                raw+b'<h1 data-test="listing-description-title">Exact Original Role</h1>'):
            with self.subTest(value=value[-90:]):self.assertEqual(self.inspect(value),'indeterminate')

    def test_page_identity_duplicates_query_canonical_and_scripts_fail_closed(self):
        raw=v2_page()
        for value in (raw.replace(b'"listingId": "list_old"',b'"listingId": "list_wrong"',1),
                raw.replace(b'"slug": ["test-role"]',b'"slug": ["other"]'),
                raw.replace(b'"disableApplications": true',b'"disableApplications": true, "disableApplications": false'),
                raw+b'<script id="__NEXT_DATA__">{}</script>',
                raw.replace(b'rel="canonical"',b'rel="other"'),
                raw.replace(b'<span>This listing is not accepting applications currently</span>',
                    b'<script>This listing is not accepting applications currently</script>')):
            self.assertEqual(self.inspect(value),'indeterminate')


class NegativeLifecycleTests(unittest.TestCase):
    setUp=prior.LifecycleTests.setUp
    publish=prior.LifecycleTests.publish

    def collect(self,*,both=False,archived=False):
        selected=self.known if both else self.known[:1];responses=[];events=[]
        for i,row in enumerate(selected):
            start=row['url'];final=start+'/test-role'
            responses.extend([Response(start,b'',308,final),Response(final,v2_page(row['external_id'],archived=archived or bool(i)))])
        with daily_source('mercor'),a.known_public_jobs(selected),\
                local_inventory.refresh_request_budget(http_limit=len(responses),detail_limit=0,audit_sink=events.append),transport(responses):
            result=a.augment_result(mercor.parse_mercor_observations({'listings':[]}))
        return result,events

    def test_both_negative_shapes_close_only_named_jobs_without_clock_renewal_and_summary_qualifies(self):
        before=maintenance.inspect_source(self.db,'mercor',datetime.fromisoformat(END),catalog_only=True)
        result,events=self.collect(both=True)
        self.assertEqual(len(result.source_records),2)
        self.assertEqual({r.contract_id for r in result.source_records},{a.NEGATIVE_CONTRACT_ID})
        sealed=staged_observation.decode_result(json.loads(json.dumps(asdict(result))))
        self.assertEqual(asdict(sealed),asdict(result))
        summary,_=self.publish(sealed)
        self.assertEqual(summary.jobs_removed,2);self.assertFalse(summary.removals_authorized)
        self.assertEqual({r['last_seen_at'] for r in self.db.execute('SELECT last_seen_at FROM jobs')},{OLD})
        self.assertEqual({r['removed_at'] for r in self.db.execute('SELECT removed_at FROM jobs')},{AT})
        self.assertEqual([dict(row) for row in self.db.execute('SELECT * FROM job_source_contents')],self.contents)
        after=maintenance.inspect_source(self.db,'mercor',datetime.fromisoformat(END),catalog_only=True)
        plan=dict(plan_id='v2-closed',config=dict(providers=['mercor'],http_limit=4),sources=[before])
        report=dict(events=[*[dict(event='source_transport',data=e) for e in events],
            dict(event='collected_result',data=dict(result=asdict(result))),
            dict(event='operation_result',data=dict(operation='catalog:mercor',result=dict(summary=asdict(summary),after=after)))])
        row=daily.summarize_source(plan,report,datetime.fromisoformat(START),datetime.fromisoformat(END))
        self.assertTrue(row['qualifying_observation']);self.assertEqual(row['confirmed_closed'],2)
        self.assertEqual(row['uncertain'],0);self.assertEqual(row['cohorts'],[]);self.assertEqual(row['stale'],0)

    def test_archived_pool_does_not_insert_replacement_or_touch_unobserved_sibling(self):
        result,_=self.collect(archived=True)
        summary,_=self.publish(result)
        rows={r['external_id']:dict(r) for r in self.db.execute('SELECT * FROM jobs')}
        self.assertEqual(set(rows),{'list_old','list_other'})
        self.assertEqual(rows['list_other'],self.before['list_other'])
        self.assertEqual(rows['list_old']['last_seen_at'],OLD)
        self.assertEqual(rows['list_old']['is_active'],0)
        self.assertEqual(summary.jobs_removed,1)
        self.assertEqual([dict(r) for r in self.db.execute('SELECT * FROM job_source_contents')],self.contents)

    def test_contract_downgrade_positive_forgery_and_missing_notice_cannot_publish(self):
        result,_=self.collect();record=result.source_records[0]
        for changes in (dict(contract_id=a.CONTRACT_ID),dict(state='open'),dict(contract_id='invented'),
                dict(raw_html=record.raw_html.replace('currently','soon')),
                dict(known_job_id=self.before['list_other']['id'])):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.publish(replace(result,source_records=(replace(record,**changes),)))
            self.db.rollback()
            self.assertEqual({r['external_id']:dict(r) for r in self.db.execute('SELECT * FROM jobs')},self.before)
        positive,_,_,_=prior.LifecycleTests.collect(self)
        with self.assertRaises(ValueError):a.validate_record(replace(positive.source_records[0],contract_id=a.NEGATIVE_CONTRACT_ID))

    def test_v2_sealed_raw_journal_roundtrip_and_tamper_rejection(self):
        start=a.public_job_url('list_old');final=start+'/test-role'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);journal=root/'journal';staged=root/'staged'
            with transport([Response(start,b'',308,final),Response(final,v2_page(archived=True))]),\
                    patch.object(staged_observation,'timestamp',side_effect=[START,END]),\
                    patch.dict(pipeline.CRAWLERS,mercor=lambda _:a.augment_result(mercor.parse_mercor_observations({'listings':[]}))):
                plan_id=staged_observation.collect('mercor',mercor.MERCOR_ENDPOINT,staged,run_id='negative-v2',
                    code_commit='a'*40,http_max=2,journal_root=journal,known_jobs=self.known[:1])
            with patch.object(staged_observation,'timestamp',return_value=END):
                value,report=staged_observation.load(staged,'mercor',run_id='negative-v2',code_commit='a'*40,journal_root=journal)
                self.assertEqual(value.result.source_records[0].contract_id,a.NEGATIVE_CONTRACT_ID)
                a.validate_journal_records(value.result,report)
                with self.assertRaises(ValueError):a.validate_journal_records(value.result,{'events':[]})
                raw=next(p for p in (journal/plan_id).glob('*.raw') if p.stat().st_size>0)
                raw.write_bytes(b'changed')
                with self.assertRaises(ValueError):staged_observation.load(staged,'mercor',run_id='negative-v2',code_commit='a'*40,journal_root=journal)


if __name__=='__main__':unittest.main()
