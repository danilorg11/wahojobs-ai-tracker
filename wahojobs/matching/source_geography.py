"""Project accepted Mercor record evidence into the existing location gate."""

import json

from wahojobs.profiles.countries import COUNTRY_BY_CODE, normalize_country


MERCOR_GEOGRAPHY_FIELDS = {
    "eligibleLocation": ("location", "allow"),
    "eligibleResidenceLocation": ("residence", "allow"),
    "ineligibleLocation": ("location", "exclude"),
    "ineligibleResidenceLocation": ("residence", "exclude"),
}
# Mercor supplies alpha-3 tokens. Decode these standard country identifiers;
# an unsupported token remains unresolved, never a guessed country or exclusion.
# XKX is the provider's Kosovo token (the existing country vocabulary uses XK).
MERCOR_COUNTRY_CODES = dict(pair.split(":") for pair in """
USA:US GBR:GB CAN:CA AUS:AU IRL:IE DEU:DE BEL:BE NLD:NL NZL:NZ MLT:MT
SWE:SE DNK:DK NOR:NO FIN:FI AUT:AT IND:IN FRA:FR ESP:ES EST:EE ISL:IS
LVA:LV LTU:LT LIE:LI LUX:LU CHE:CH HRV:HR GRC:GR ITA:IT PRT:PT SVN:SI
BGR:BG CZE:CZ HUN:HU POL:PL ROU:RO SVK:SK JPN:JP MCO:MC ALB:AL BIH:BA
XKX:XK MKD:MK SMR:SM SRB:RS MDA:MD MEX:MX PER:PE GTM:GT SLV:SV HND:HN
CRI:CR PAN:PA DOM:DO COL:CO ECU:EC BOL:BO CHL:CL ARG:AR URY:UY PRY:PY
KOR:KR CYP:CY BRA:BR CUB:CU NIC:NI VEN:VE HKG:HK GUY:GY
""".split())


def _country_requirement(field, value):
    countries = set()
    unresolved = type(value) is not list
    for token in value if type(value) is list else []:
        try:
            if type(token) is not str:
                raise ValueError("country token must be text")
            code = MERCOR_COUNTRY_CODES.get(token.strip().upper())
            countries.add(COUNTRY_BY_CODE[code] if code else normalize_country(token))
        except (ValueError, KeyError):
            unresolved = True
    dimension, mode = MERCOR_GEOGRAPHY_FIELDS[field]
    return {"dimension": dimension, "mode": mode, "countries": sorted(countries),
            "unresolved": unresolved, "source_field": field}


def apply_mercor_applicant_geography(connection, rows):
    """Attach only each variant's accepted source capture; never canonical union.

    No body inference, preferred/employer locations, timezone, nationality or
    work-authorization inference. Null/empty fields do not grant worldwide access.
    The caller holds the same read transaction as its inventory query.
    """
    wanted = {r["job_id"] for r in rows if r.get("source_slug") == "mercor"}
    if not wanted:
        return rows
    cursor = connection.execute("""
        SELECT sc.job_id, sc.id AS capture_id, sc.metadata_json,
               sc.source_url, sc.observed_at, sc.material_content_sha256
        FROM job_source_content_acceptances a
        JOIN job_source_content_captures sc
          ON sc.id = a.accepted_capture_id AND sc.job_id = a.job_id
        JOIN jobs j ON j.id = sc.job_id
        JOIN companies c ON c.id = j.company_id
        JOIN crawl_runs cr ON cr.id = sc.crawl_run_id AND cr.company_id = c.id
        WHERE c.slug = 'mercor' AND sc.provider = 'mercor'
          AND sc.source_type = 'mercor-marketplace'
          AND sc.record_promotion_contract_id = 'mercor_public_active_record_v1'
          AND sc.promotion_policy_version = 'mercor_record_promotion_v1'
          AND sc.promotion_decision IN ('promoted', 'confirmed')
          AND sc.provider_outcome IN ('success', 'partial')
          AND sc.used_sample_data = 0 AND cr.used_sample_data = 0
          AND cr.status IN ('success', 'partial')
    """)
    columns = [column[0] for column in cursor.description]
    evidence = {}
    for raw in cursor:
        capture = dict(zip(columns, raw))
        if capture["job_id"] not in wanted:
            continue
        metadata = json.loads(capture.pop("metadata_json"))
        if type(metadata) is not dict:
            raise ValueError("accepted Mercor metadata is not an object")
        fields = {f: metadata[f] for f in MERCOR_GEOGRAPHY_FIELDS if f in metadata}
        requirements = [_country_requirement(f, v) for f, v in fields.items()
                        if v is not None and v != []]
        if requirements:
            evidence[capture["job_id"]] = (requirements, {**capture, "fields": fields})
    return [dict(row, applicant_country_requirements=evidence[row["job_id"]][0],
                 applicant_geography_evidence=evidence[row["job_id"]][1])
            if row["job_id"] in evidence else row for row in rows]
