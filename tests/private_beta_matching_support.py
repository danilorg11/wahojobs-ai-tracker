"""Bounded release cohort: preserved wording, synthetic acceptance and owners.

This observer never collects sources, prepares model evidence, changes ranking,
or treats detail-only comparison as an executed Matches admission decision.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

from tests.matching_delivery_support import DeliveryFixture
from tests import matching_delivery_support
from wahojobs import authenticated_profile_matches as browser

ROOT = Path(matching_delivery_support.__file__).parent / 'fixtures'
CLOCK = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
PERSONAS = ('beginner_bilingual_generalist', 'monolingual_entry_level',
    'multilingual_language_specialist', 'customer_support_worker', 'software_engineer',
    'biology_researcher', 'restrictive_location_non_us', 'non_phone_preference')


def visible_text(body):
    """Text including closed native disclosures, excluding HTML syntax."""
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts=[]
        def handle_data(self, value):
            self.parts.append(value)
    parsed=Text();parsed.feed(body.decode('utf-8'))
    return ' '.join(' '.join(parsed.parts).split())


def persona(name):
    from scripts.profile_matching_coverage import canonical_profile_for_persona
    from wahojobs.profiles.canonical import complete_trusted_fixture_provenance
    from wahojobs.profiles.canonical_v2 import convert_v1_to_v2
    from wahojobs.profiles.preference_model import legacy_preferences_to_preference_draft_v2
    from tests.test_profile_preference_model import with_preference_model
    original = next(p for p in json.loads((ROOT/'product_readiness_personas_v1.json').read_text())['personas']
                    if p['persona_id'] == name)
    v1 = complete_trusted_fixture_provenance(canonical_profile_for_persona(original))
    result = convert_v1_to_v2(json.loads(json.dumps(v1)),
        persistent_profile_id='prf_0123456789abcdef0123456789abcdef', source_ordinal_resolver=lambda *_: [1])
    # Existing deterministic mapping only; ambiguities remain unselected. This is
    # labelled synthetic confirmation, not a claim that historical people agreed.
    draft = legacy_preferences_to_preference_draft_v2(result['preferences'])
    return with_preference_model(result, draft['preference_model'])


def sources():
    from tests.test_provider_detail_recovery import CASES, candidate, response
    from wahojobs.crawler.provider_details import recover_detail
    from tests.test_accepted_title_uncertainty import BODY
    from tests.candidate_decision_support import TOOL_BODY
    result = []
    titles = {1039: 'Biologist Expert Network', 11242: 'Scientific Computing', 11271: 'Biology Assessment Expert',
              384: 'Portuguese Language Specialist (Brazil)', 646: 'Portuguese Language Specialist (Brazil) – AI Trainer'}
    for file in ('card_source_wording.json', 'language_task_source_examples.json'):
        for row in json.loads((ROOT/file).read_text(encoding='utf-8-sig')):
            result.append(dict(provider=row['source_slug'], title=titles[row['job_id']], body=row['body'],
                body_format=row['body_format'], metadata=json.loads(row['metadata_json']),
                external_id=row['external_id'], url=row['url'], location=row.get('location') or 'Unknown',
                commitment=row.get('commitment') or '', authority='preserved public source wording',
                observed_at=row['last_captured_at'], fixture=file, original_job_id=row['job_id']))
    for case in CASES:
        recovered = recover_detail(case['provider'], candidate(case), response(case))
        result.append(dict(provider=case['provider'], title=case['title'], body=recovered.source_body,
            body_format=recovered.source_body_format, metadata=recovered.source_metadata,
            external_id=case['external_id'], url=case['url'], location=recovered.location,
            commitment=recovered.commitment or '', authority='preserved public detail wording',
            observed_at=case['observed_at'], fixture='source_detail_recovery/'+case['file']))
    result.append(dict(provider='alignerr', title='Generalist',
        body=(ROOT/'source_requirement_fidelity/accepted-generalist.txt').read_text(encoding='utf-8'),
        body_format='text/markdown', metadata={}, external_id='583b0f74-43d6-4382-a132-0e9fd41daf8c',
        url='https://www.alignerr.com/jobs/583b0f74-43d6-4382-a132-0e9fd41daf8c', location='Remote', commitment='',
        authority='preserved public accepted wording', observed_at='2026-09-14T13:00:44.369032+00:00',
        fixture='source_requirement_fidelity/accepted-generalist.txt', original_job_id=2037))
    for name, title, text in (
        ('support', 'Customer success / support operations Evaluator', BODY),
        ('python', 'AI Content Evaluation with Python', TOOL_BODY),
        ('french', 'French AI Content Evaluator', '## Responsibilities\nReview and evaluate AI-generated content.\n\n## Requirements\nNo prior AI experience required\n\nNative French required')):
        result.append(dict(provider='synthetic-beta', title=title, body=text, body_format='text/markdown', metadata={},
            external_id='beta-control-'+name, url='https://jobs.example.test/beta-control-'+name,
            location='Remote', commitment='', authority='synthetic mechanism control', observed_at=None,
            fixture='existing test bodies; French control follows source_requirement_fidelity contract'))
    for index, row in enumerate(result):
        row.update(job_id=960000+index, canonical_id=970000+index)
        row['body_sha256'] = sha256(row['body'].encode()).hexdigest()
        network=row.get('original_job_id')==1039
        row['opportunity_kind']='evergreen_application' if network else 'live_posting'
        row['availability_basis']='evergreen_page' if network else 'api_feed'
        row['include_live']=0 if network else 1
    return result


def seed_inventory(connection, now=CLOCK):
    """Add the same intact cohort to a new synthetic database; no owner writes."""
    from wahojobs.crawler.types import JobCandidate
    from wahojobs.source_capture import SourceCaptureContext
    from wahojobs.db.repository import upsert_job_source_content
    rows = sources(); stamp = now.isoformat()
    providers = {name: 980000+i for i, name in enumerate(sorted({s['provider'] for s in rows}))}
    for slug, proposed_id in list(providers.items()):
        existing=connection.execute('SELECT id FROM companies WHERE slug=?',(slug,)).fetchone()
        cid=existing[0] if existing else proposed_id
        providers[slug]=cid
        if existing is None:
            connection.execute('INSERT INTO companies(id,name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES (?,?,?,?,?,?,?)',
                (cid, slug.title(), slug, 'https://jobs.example.test/'+slug, 'core', 'live_feed', 'count_live'))
        connection.execute('INSERT INTO crawl_runs(id,company_id,status,started_at,finished_at,jobs_found_count,used_sample_data) VALUES (?,?,\'success\',?,?,?,0)',
            (proposed_id, cid, stamp, stamp, sum(s['provider']==slug for s in rows)))
    for row in rows:
        cid=providers[row['provider']]
        connection.execute('INSERT INTO canonical_opportunities(id,company_id,canonical_key,canonical_title,normalized_title,source_category,first_seen_at,last_seen_at,is_active,variant_count) VALUES (?,?,?,?,?,\'\',?,?,1,1)',
            (row['canonical_id'], cid, row['external_id'], row['title'], row['title'].lower(), stamp, stamp))
        connection.execute('INSERT INTO jobs(id,company_id,canonical_opportunity_id,external_id,title,location,department,expertise,commitment,url,source_hash,first_seen_at,last_seen_at,is_active,opportunity_kind,availability_basis,include_in_live_market_estimate) VALUES (?,?,?,?,?,?,\'\',\'\',?,?,?,?,?,1,?,?,?)',
            (row['job_id'], cid, row['canonical_id'], row['external_id'], row['title'], row['location'], row['commitment'], row['url'], row['body_sha256'], stamp, stamp,row['opportunity_kind'],row['availability_basis'],row['include_live']))
        candidate=JobCandidate(external_id=row['external_id'],title=row['title'],location=row['location'],url=row['url'],
            source_hash=row['body_sha256'],source_body=row['body'],source_body_format=row['body_format'],source_metadata=row['metadata'],
            opportunity_kind=row['opportunity_kind'],availability_basis=row['availability_basis'],include_in_live_market_estimate=bool(row['include_live']))
        upsert_job_source_content(connection,row['job_id'],row['provider'],'synthetic-private-beta-replay',candidate,stamp,
            capture_context=SourceCaptureContext(None,'success',False,True,True,False,1,1,1,0,
                'synthetic acceptance of preserved public wording; not current verification','private-beta-v1'))
    return rows


def cohort_case(name, *, strict_pay=False):
    """Observe the supported authenticated composition, with no HTTP listener."""
    from wahojobs.authenticated_variant_details import variant_detail_url
    from wahojobs import authenticated_source_detail
    profile = persona(name)
    if strict_pay:
        from tests.test_profile_preference_model import with_preference_model
        model=deepcopy(profile['preferences']['preference_model'])
        model['compensation_expectations']=[dict(minimum_kind='strict',amount='100',currency='USD',period='hour')]
        profile=with_preference_model(profile,model)
    fixture = DeliveryFixture(profile, 'Intentionally empty control', title='Inventory control', now=CLOCK)
    try:
        with closing(sqlite3.connect(fixture.path)) as c, c:
            c.row_factory=sqlite3.Row
            # Fixture bootstrap remains intact but inactive, never a competitor.
            c.execute('UPDATE jobs SET is_active=0 WHERE company_id=900001')
            c.execute('UPDATE canonical_opportunities SET is_active=0 WHERE company_id=900001')
            inventory=seed_inventory(c)
        evaluated=[]; projected=[]
        original=browser.profile_preview.build_preview_context_from_canonical_rows
        def observer(canonical, **kwargs):
            projected.append(deepcopy(canonical))
            prior=kwargs.get('evaluated_match_sink')
            def sink(match):
                evaluated.append(deepcopy(match))
                if prior is not None: prior(match)
            return original(canonical, **dict(kwargs,evaluated_match_sink=sink))
        with patch.object(browser.profile_preview,'build_preview_context_from_canonical_rows',side_effect=observer):
            page,run,context,_=fixture.current()
        main=browser._primary_presentation_matches(context)
        conditional=browser._conditional_presentation_matches(context)
        recommendations=browser._recommendation_presentation_matches(context)
        selected={m['job_id']:m for group in context['matches'].values() for m in group}
        outcomes=[]
        for source in inventory:
            packets=[]; original_detail=authenticated_source_detail.prepare_detail_display
            def inspect_detail(job,p):
                result=original_detail(job,p);packets.append(deepcopy(result));return result
            route=variant_detail_url(dict(job_id=source['job_id'],canonical_opportunity_id=source['canonical_id']),run_id=run.match_run_id)
            with patch.object(authenticated_source_detail,'prepare_detail_display',side_effect=inspect_detail):
                detail=fixture.get(route)
            match=selected.get(source['job_id'])
            rendered=context.get('_card_evidence',{}).get(source['job_id'])
            from wahojobs.candidate_source_display import plain
            exact_rows=packets[0].get('comparisons',[]) if packets else []
            exact_text=visible_text(detail.body)
            outcomes.append(dict(job_id=source['job_id'],title=source['title'],provider=source['provider'],
                scored=[m for m in evaluated if m.get('job_id')==source['job_id']],
                matches_qualification_admission_comparison_reached=bool(match and 'source_qualification_comparisons' in match),
                matches_language_admission_comparison_reached=bool(match and match.get('source_language_checks')),
                rendered_comparison=rendered.get('comparisons',[]) if rendered else None,
                rendered_quotes_present=all(unescape(r['source']['quote']) in unescape(page.body.decode())
                    for r in rendered.get('comparisons',[])) if rendered else None,
                selected_match=match, detail_status=detail.status,
                exact_comparison=exact_rows,
                exact_quotes_present=all(' '.join(plain(r['source']['quote']).split()) in exact_text
                                         for r in exact_rows),
                comparison_authority='authenticated exact detail; does not prove Matches admission ran',
                main_rank=next((i for i,m in enumerate(main,1) if m['job_id']==source['job_id']),None),
                conditional_rank=next((i for i,m in enumerate(conditional,1) if m['job_id']==source['job_id']),None)))
            outcomes[-1]['recommendation_rank']=next((i for i,m in enumerate(recommendations,1)
                                                     if m['job_id']==source['job_id']),None)
        return dict(persona=name, scenario='strict USD 100/hour minimum' if strict_pay else 'preselected profile', clock=CLOCK.isoformat(), status=page.status,
            inventory_count=context.get('_authenticated_inventory_count'), projected=projected,
            main=[m['job_id'] for m in main],conditional=[m['job_id'] for m in conditional],
            recommendations=[m['job_id'] for m in recommendations],
            preferences=context.get('_typed_preference_enforcement'), outcomes=outcomes,
            anonymous_status=fixture.get(owner=None).status, other_owner_status=fixture.get(owner=1).status)
    finally:
        fixture.close()


def emit_receipt(name, result):
    root=os.environ.get('WAHOJOBS_BETA_COHORT_EVIDENCE')
    if root:
        target=Path(root);target.mkdir(parents=True,exist_ok=True)
        # No sessions, owner IDs, CSRF tokens, or raw response bodies are emitted.
        (target/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
