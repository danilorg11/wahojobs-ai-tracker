"""Project accepted Mercor record evidence into the existing location gate."""

import hashlib
import json
import re

from wahojobs.profiles.countries import COUNTRY_BY_CODE, normalize_country
from wahojobs.source_capture import normalize_source_body


MERCOR_GEOGRAPHY_FIELDS = {
    "eligibleLocation": ("location", "allow"),
    "eligibleResidenceLocation": ("residence", "allow"),
    "ineligibleLocation": ("location", "exclude"),
    "ineligibleResidenceLocation": ("residence", "exclude"),
}
DESCRIPTION_GEOGRAPHY_KEY = "wahojobs_applicant_description_geography_v1"
_ELIGIBILITY_SECTIONS = {"role details", "eligibility", "applicant eligibility",
                         "who can apply", "applicant location", "location requirements"}
_APPLICANT = r"(?:applicants|candidates|contributors|workers)"
_PLACE = r"(?P<place>be based in|be located in|reside in|live in|be residents of)"
_MANDATORY = re.compile(rf"{_APPLICANT} (?:must|are required to) (?P<not>not )?{_PLACE} (?P<countries>.+)", re.I)
_ELIGIBLE = re.compile(rf"(?P<only>only )?{_APPLICANT} (?P<place>based in|located in|residing in|living in) "
                       r"(?P<countries>.+?) (?P<result>are eligible|may apply|can apply|are not eligible|cannot apply|are ineligible)", re.I)
_PREFERRED = re.compile(rf"{_APPLICANT} (?P<place>based in|located in|residing in|living in) (?P<countries>.+?) (?:are )?preferred", re.I)
_NOT_REQUIRED = re.compile(rf"{_APPLICANT} (?:are not required to|need not) {_PLACE} (?P<countries>.+)", re.I)
_UNRELATED = re.compile(r"\b(headquarters|customers?|clients?|markets?|timezones?|time zones?|nationality|citizenship|citizens|nationals|authorization|authorised|authorized|visa|passport)\b", re.I)


def _description_countries(text):
    # Exact country vocabulary only. Unknown alternatives cannot support a veto.
    text = re.sub(r"\bU\.S(?:\.A)?\.?(?=\s|$)", "USA", text, flags=re.I).strip(" .")
    text = re.sub(r"^the\s+", "", text, flags=re.I)
    try:
        return [normalize_country(text)], False
    except ValueError:
        pass
    tokens = re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", text, flags=re.I)
    countries, unresolved = set(), False
    for token in tokens:
        try:
            countries.add(normalize_country(re.sub(r"^the\s+", "", token, flags=re.I)))
        except ValueError:
            unresolved = True
    return sorted(countries), unresolved


def prepare_applicant_residence_clause(quote, modality, reference):
    """Accepted qualification clauses only; compound speaker/residence wording.

    Reuse the existing country-list contract. A residence requirement never
    borrows employer location, nationality or work permission.
    """
    text = re.sub(r'[*#]', '', quote).strip().rstrip('.')
    if modality != 'required' or _UNRELATED.search(text):
        return None
    start = (r'(?:(?:Applicants|Candidates|Contributors|Workers) (?:must|are required to) |Must )?'
             r'(?:be )?(?:currently )?')
    compound = (r'(?:Native(?:-level)?(?: or near-native)?|Near-native|Fluent) '
                r'[A-Za-z -]+? speakers? ')
    match = re.fullmatch(rf'(?:{start}|{compound})(?P<place>based|located|residing|living|reside|live) in (?P<countries>.+)', text, re.I)
    if not match:
        return None
    value = match['countries']
    if re.search(r'\b(?:not|except|unless|if|preferred|encouraged)\b', value, re.I):
        return None  # preserve unsupported negation/condition as unassessed
    countries, unresolved = _description_countries(value)
    if not countries:
        return None
    return dict(dimension='residence' if re.search(r'resid|liv', match['place']) else 'location',
                mode='allow', countries=countries, unresolved=unresolved,
                source_field=reference, source_quote=quote)


def prepare_applicant_location_support(body, source_field):
    """Prepare non-exclusive recruitment evidence at detail ingestion.

    This bounded grammar describes where applicants are invited, not where an
    employer operates. A named city followed by nationwide coverage does not
    restrict applicants to that city. Unsupported qualifications stay raw.
    """
    body = normalize_source_body(body)
    if not body:
        return None
    clauses, section = [], ''
    for number, raw in enumerate(body.splitlines(), 1):
        if raw.startswith('#'):
            section = raw.strip('# *:').casefold()
            continue
        if section not in {'about the role', 'about this role', 'the role', 'overview'}:
            continue
        for sentence in re.split(r'(?<=[.!?])\s+', raw.strip()):
            if (_UNRELATED.search(sentence) or re.search(
                    r'\b(?:not|no|only|except|unless|if|preferred|preferably|ideal|ideally|may|might)\b', sentence, re.I)):
                continue
            match = re.fullmatch(
                r"(?:We(?:'re|’re| are) looking for|We are recruiting|We recruit) "
                r"(?P<role>[\w ,()—–-]+?) (?:based|located|residing|living) in "
                r"(?P<places>.+?) to [^\n]+", sentence, re.I)
            if not match:
                continue
            places = match['places']
            # 'City and across Country' expressly broadens the invitation.
            nationwide = re.fullmatch(r'[\w ,–-]+? and (?:across|throughout) (.+)', places, re.I)
            countries, unresolved = _description_countries(nationwide[1] if nationwide else places)
            if not countries or unresolved:
                continue
            clauses.append(dict(dimension='location', mode='allow', modality='invitation',
                                countries=countries, unresolved=False,
                                source_field=f'{source_field}:line {number}', source_quote=sentence))
    if clauses:
        # Use the existing explicit applicant-clause grammar to retain contrary
        # statements alongside invitations, including negated eligibility.
        explicit = prepare_mercor_description_geography(body, source_field, {}) or {}
        for clause in explicit.get('clauses', []):
            if clause['modality'] in {'mandatory', 'unresolved'}:
                clauses.append(dict(clause,
                    ambiguous_statement=clause['modality'] == 'unresolved',
                    source_conflict=clause['dimension'] in explicit['conflicting_dimensions']))
    return (dict(version=1, body_sha256=hashlib.sha256(body.encode()).hexdigest(), clauses=clauses)
            if clauses else None)


def prepare_mercor_description_geography(body, source_field, structured):
    """Prepare a bounded grammar at ingestion, never while serving matches.

    Match whole statements, retaining exact accepted-body lines and modality.
    A bare country-only bullet requires a known applicant/role-details section.
    Unsupported qualifiers/conditional variants remain opaque, not hard gates.
    """
    body = normalize_source_body(body)
    if not body:
        return None
    clauses, section, code = [], "", False
    for line_number, raw in enumerate(body.splitlines(), 1):
        text = raw.strip()
        if text.startswith("```"):
            code = not code
        if code or not text or text.startswith((">", "```")):
            continue
        if (text.startswith("#") or re.fullmatch(r"\*\*[^*]+\*\*:?", text)
                or re.fullmatch(r"[A-Za-z][A-Za-z /&()-]{1,60}:", text)):
            section = text.strip("# *:").casefold()
            continue
        text = re.sub(r"^(?:[-*•]\s+)", "", text).strip().rstrip(".")
        # Country mentions about a different dimension are never location rules.
        if _UNRELATED.search(text):
            continue
        dimension, mode, modality, countries, unresolved = "location", "allow", "mandatory", [], False
        match = _NOT_REQUIRED.fullmatch(text)
        if match:
            modality = "not_required"
        else:
            match = _PREFERRED.fullmatch(text)
            if match:
                modality = "preferred"
            else:
                match = _MANDATORY.fullmatch(text)
                if match:
                    mode = "exclude" if match['not'] else "allow"
                else:
                    match = _ELIGIBLE.fullmatch(text)
                    if match:
                        if match['result'].lower() in {"are not eligible", "cannot apply", "are ineligible"}:
                            mode = "exclude"
                        elif not match['only']:
                            # A positive invitation need not be an exhaustive list.
                            modality = "invitation"
        if match:
            dimension = "residence" if re.search(r"resid|liv", match['place'], re.I) else "location"
            countries, unresolved = _description_countries(match['countries'])
        elif re.fullmatch(rf"{_APPLICANT} (?:from|in) (?:all countries|any country|anywhere|worldwide) (?:are eligible|may apply|can apply)", text, re.I):
            modality = "unrestricted"
        else:
            only = re.fullmatch(r"(?P<countries>.+?) only", text, re.I)
            if only and not re.search(r"\b(not|except|unless|preferred|if|for)\b", text, re.I):
                countries, unresolved = _description_countries(only['countries'])
                # A recognized country alone is insufficient outside this context.
                if not countries:
                    continue
                if section not in _ELIGIBILITY_SECTIONS:
                    modality = "unresolved"
            elif (re.search(_APPLICANT, text, re.I) and re.search(r"\b(based|located|reside|residents|living)\b", text, re.I)):
                # Includes conditional/variant-specific and compound statements.
                modality, unresolved = "unresolved", True
            else:
                continue
        if modality == "unresolved" and re.search(r"\b(?:a preference, not a requirement|preferred)\b", text, re.I):
            modality = "preferred"
        if section in {"preferred qualifications", "preferred locations", "nice to have", "preferences"}:
            if modality == "mandatory":
                # Mandatory phrasing inside a preference section is ambiguous.
                modality = "unresolved"
            elif modality == "unresolved" and not re.search(r"\b(must|required)\b", text, re.I):
                modality = "preferred"
        clauses.append({"dimension": dimension, "mode": mode, "countries": countries,
                        "unresolved": unresolved, "modality": modality,
                        "source_field": f"{source_field}:line {line_number}",
                        "source_quote": raw, "section": section})
    if not clauses:
        return None
    # Detect contradictions involving newly prepared description evidence. Leave
    # existing structured-only policy untouched; do not silently pick a source.
    structured_requirements = [_country_requirement(f, v) for f, v in structured.items()
                               if f in MERCOR_GEOGRAPHY_FIELDS and v not in (None, [])]
    conflicts = []
    for dimension in ("location", "residence"):
        described = [c for c in clauses if c['dimension'] == dimension]
        mandatory = [c for c in described if c['modality'] == 'mandatory' and not c['unresolved']]
        required = mandatory + [c for c in structured_requirements if c['dimension'] == dimension and not c['unresolved']]
        allowed = [set(c['countries']) for c in required if c['mode'] == 'allow']
        excluded = set().union(*(set(c['countries']) for c in required if c['mode'] == 'exclude'))
        impossible = bool(mandatory and allowed and not (set.intersection(*allowed) - excluded))
        released = any(c['modality'] == 'unrestricted' and required for c in described)
        released |= any(c['modality'] == 'not_required' and any(a <= set(c['countries']) for a in allowed) for c in described)
        invitation_conflict = any(c['modality'] == 'invitation' and not c['unresolved'] and
                                  (bool(set(c['countries']) & excluded) or any(not set(c['countries']) <= a for a in allowed)) for c in described)
        if impossible or released or invitation_conflict:
            conflicts.append(dimension)
    return {"version": 1, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "clauses": clauses, "conflicting_dimensions": conflicts}
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

    Description evidence was prepared during ingestion and shares this accepted
    capture's hash/provenance. No description parsing occurs on the request path.
    Null/empty fields do not grant worldwide access.
    The caller holds the same read transaction as its inventory query.
    """
    wanted = {r["job_id"] for r in rows if r.get("source_slug") == "mercor"}
    if not wanted:
        return rows
    cursor = connection.execute(f"""
        SELECT sc.job_id, sc.id AS capture_id, sc.metadata_json,
               sc.source_url, sc.observed_at, sc.material_content_sha256
        FROM job_source_content_captures sc
        JOIN jobs j ON j.id = sc.job_id
        JOIN companies c ON c.id = j.company_id
        JOIN crawl_runs cr ON cr.id = sc.crawl_run_id AND cr.company_id = c.id
        WHERE sc.job_id IN ({','.join('?' for _ in wanted)})
          AND c.slug = 'mercor' AND sc.provider = 'mercor'
          AND sc.source_type = 'mercor-marketplace'
          AND sc.record_promotion_contract_id = 'mercor_public_active_record_v1'
          AND sc.promotion_policy_version IN ('mercor_record_promotion_v1', 'mercor_record_promotion_v2')
          AND (sc.promotion_decision IN ('promoted', 'confirmed') OR
               (sc.promotion_policy_version = 'mercor_record_promotion_v2'
                AND sc.promotion_decision = 'held_degraded'
                AND sc.decision_reasons_json = '["summary_omits_compatible_supplemental_content"]'))
          AND sc.provider_outcome IN ('success', 'partial')
          AND sc.used_sample_data = 0 AND cr.used_sample_data = 0
          AND cr.status IN ('success', 'partial')
          AND sc.normalized_record_count = sc.candidate_count
        ORDER BY sc.id
    """, sorted(wanted))
    columns = [column[0] for column in cursor.description]
    evidence = {}
    for raw in cursor:
        capture = dict(zip(columns, raw))
        if capture["job_id"] not in wanted:
            continue
        evidence.pop(capture["job_id"], None)
        metadata = json.loads(capture.pop("metadata_json"))
        if type(metadata) is not dict:
            raise ValueError("accepted Mercor metadata is not an object")
        fields = {f: metadata[f] for f in MERCOR_GEOGRAPHY_FIELDS if f in metadata}
        requirements = [_country_requirement(f, v) for f, v in fields.items()
                        if v is not None and v != []]
        description = metadata.get(DESCRIPTION_GEOGRAPHY_KEY)
        if description:
            if description.get("version") != 1:
                raise ValueError("unsupported prepared Mercor description geography")
            for clause in description["clauses"]:
                if clause["modality"] in {"mandatory", "unresolved"}:
                    requirements.append({**clause, "ambiguous_statement": clause["modality"] == "unresolved"})
            for dimension in description["conflicting_dimensions"]:
                affected = [r for r in requirements if r["dimension"] == dimension]
                if not affected:
                    raise ValueError("conflicting geography lacks restriction evidence")
                for requirement in affected:
                    requirement["source_conflict"] = True
        if requirements:
            provenance = {**capture, "fields": fields}
            if description:
                provenance["description"] = description
            evidence[capture["job_id"]] = (requirements, provenance)
    return [dict(row, applicant_country_requirements=evidence[row["job_id"]][0],
                 applicant_geography_evidence=evidence[row["job_id"]][1])
            if row["job_id"] in evidence else row for row in rows]
