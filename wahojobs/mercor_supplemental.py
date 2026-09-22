"""Dated Mercor pay composition over a separately retained raw summary capture.

Only the supplemental pay pair is reusable. All current conditions come from
this summary. Immutable capture references make this projection replayable.
"""
from dataclasses import replace
import json
import re

CONTRACT='mercor_supplemental_composition_v1'
KEY='wahojobs_supplemental_composition_v1'


def composed_metadata(catalog,supplement):
    from wahojobs.crawler.provider_details import DETAIL_KEY
    from wahojobs.catalog_display import pay_source_text
    from wahojobs.candidate_source_display import pay_facts
    from wahojobs.source_capture import normalize_source_body
    current=json.loads(catalog['metadata_json']);old=json.loads(supplement['metadata_json'])
    detail=old.get(DETAIL_KEY,{})
    if (supplement['record_promotion_contract_id']!='provider_detail_content_v1'
            or supplement['promotion_decision'] not in ('promoted','confirmed')
            or catalog['record_promotion_contract_id']!='mercor_public_active_record_v1'
            or catalog['promotion_policy_version']!='mercor_record_promotion_v2'
            or catalog['promotion_decision'] not in ('promoted','confirmed')
            or detail.get('provider')!='mercor' or not detail.get('pay_evidence')
            or not old.get('pay') or catalog['provider']!='mercor'
            or any(catalog[k]!=supplement[k] for k in ('job_id','external_id','source_url','provider','source_type'))
            or json.loads(catalog['semantic_job_fields_json'])['title']!=json.loads(supplement['semantic_job_fields_json'])['title']):return None
    # These explicit current fields supersede the previous pay. Null/empty
    # payRate is undisclosed in the summary contract, not a withdrawal.
    if (current.get('payRate') not in (None,'') or current.get('pay') not in (None,'')
            or current.get('payRateFrequency') not in (None,'',detail['record'].get('payRateFrequency'))):return None
    body=normalize_source_body(catalog['body']) or ''
    old_body=normalize_source_body(supplement['body']) or ''
    if body!=old_body:
        text=pay_source_text(body)
        if (pay_facts({},text)['label'] or re.search(
                r'\b(?:unpaid|no compensation|not paid|pay (?:is|has been) (?:removed|withdrawn)|compensation (?:is|will be) (?:undisclosed|not disclosed))\b',body,re.I)):
            return None
    return dict(current,pay=old['pay'],**{DETAIL_KEY:detail,KEY:dict(
        catalog_capture_id=catalog['id'],supplemental_capture_id=supplement['id'],
        supplemental_observed_at=detail['observed_at'],
        supplemental_material_sha256=supplement['material_content_sha256'])})


def references(connection,catalog_id,supplement_id):
    catalog=connection.execute('SELECT * FROM job_source_content_captures WHERE id=?',(catalog_id,)).fetchone()
    supplement=connection.execute('SELECT * FROM job_source_content_captures WHERE id=?',(supplement_id,)).fetchone()
    if not catalog or not supplement or supplement_id>=catalog_id:raise ValueError('invalid_supplemental_origin')
    predecessor=connection.execute("SELECT * FROM job_source_content_captures WHERE job_id=? AND id<? AND promotion_decision IN ('promoted','confirmed') ORDER BY id DESC LIMIT 1",(catalog['job_id'],catalog_id)).fetchone()
    if predecessor is None:raise ValueError('missing_supplemental_predecessor')
    meta=json.loads(predecessor['metadata_json'])
    expected=meta[KEY]['supplemental_capture_id'] if predecessor['record_promotion_contract_id']==CONTRACT else predecessor['id']
    if expected!=supplement_id:raise ValueError('superseded_supplemental_origin')
    metadata=composed_metadata(catalog,supplement)
    if metadata is None:raise ValueError('incompatible_supplemental_origin')
    return catalog,supplement,metadata


def validate_references(connection,candidate,context):
    from wahojobs.source_capture import canonical_source_metadata_json,normalize_source_body
    meta=candidate.source_metadata;origin=meta[KEY]
    catalog,supplement,expected=references(connection,origin['catalog_capture_id'],origin['supplemental_capture_id'])
    fields=json.loads(catalog['semantic_job_fields_json'])
    if (canonical_source_metadata_json(meta)!=canonical_source_metadata_json(expected)
            or normalize_source_body(candidate.source_body)!=catalog['body']
            or candidate.source_body_format!=catalog['body_format']
            or candidate.source_updated_at!=catalog['source_updated_at']
            or any(getattr(candidate,k)!=v for k,v in fields.items())):
        raise ValueError('composition_differs_from_immutable_sources')
    return catalog


def compose_after_catalog(connection,previous,catalog_id,candidate):
    from wahojobs.crawler.types import RecordPromotionAttestation
    from wahojobs.source_capture import SourceCaptureContext
    from wahojobs.db.repository import upsert_job_source_content
    if previous is None:return None
    previous_meta=json.loads(previous['metadata_json'])
    supplement_id=previous_meta[KEY]['supplemental_capture_id'] if previous['record_promotion_contract_id']==CONTRACT else previous['id']
    supplement=connection.execute('SELECT * FROM job_source_content_captures WHERE id=?',(supplement_id,)).fetchone()
    catalog=connection.execute('SELECT * FROM job_source_content_captures WHERE id=?',(catalog_id,)).fetchone()
    if supplement is None:return None
    metadata=composed_metadata(catalog,supplement)
    if metadata is None:return None
    context=SourceCaptureContext(None,'partial',False,False,False,False,1,1,1,0,CONTRACT,CONTRACT)
    composed=replace(candidate,**json.loads(catalog['semantic_job_fields_json']),source_metadata=metadata,record_promotion_attestation=RecordPromotionAttestation(
        contract_id=CONTRACT,body_observation='present' if catalog['body'] else 'not_observed',
        authority_evidence=dict(metadata[KEY],content_only=True)))
    return upsert_job_source_content(connection,catalog['job_id'],'mercor',catalog['source_type'],
        composed,catalog['observed_at'],capture_context=context)


def validate(attestation,candidate,prepared,context,*,provider,source_type):
    from wahojobs.source_capture import canonical_authority_evidence_json
    metadata=json.loads(prepared.metadata_json);origin=metadata.get(KEY,{})
    if (provider!='mercor' or source_type!='mercor-marketplace' or context.crawl_run_id is not None
            or context.used_sample_data or context.snapshot_complete or context.pagination_complete
            or context.empty_snapshot_validated or context.provider_outcome!='partial'
            or (context.raw_record_count,context.normalized_record_count,context.candidate_count,context.rejected_record_count)!=(1,1,1,0)
            or context.payload_shape!=CONTRACT or context.schema_fingerprint!=CONTRACT
            or set(origin)!={'catalog_capture_id','supplemental_capture_id','supplemental_observed_at','supplemental_material_sha256'}
            or any(type(origin[k]) is not int or origin[k]<1 for k in ('catalog_capture_id','supplemental_capture_id'))
            or attestation.authority_evidence_json!=canonical_authority_evidence_json(dict(origin,content_only=True))):
        raise ValueError('invalid_supplemental_composition')


def decide(prepared,context,accepted_row,**options):
    from wahojobs.source_capture import SourcePromotionDecision
    if options['record_attestation'].contract_id!=CONTRACT:raise ValueError('composition_contract_required')
    # The referenced catalog has already passed current content/timestamp policy;
    # this operation adds older dated pay, never new availability authority.
    return SourcePromotionDecision('promoted',('dated_supplemental_pay_composition',),
        accepted_row['source_updated_at'] if accepted_row else None)
