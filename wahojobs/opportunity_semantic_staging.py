"""Isolated OE Semantic staging and verification v0 offline proof.

This module is deliberately disconnected from enrichment runtime, persistence,
matching, and candidate presentation.  It preserves every proposal and source
branch through deterministic authentication, normalization, bounded verification,
relation completeness, final-contract admission, and compatibility projection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict

from wahojobs.opportunity_semantic_contract import (
    ATOM_KINDS,
    CONTRACT_VERSION,
    MAX_ALTERNATIVES_PER_GROUP,
    MAX_ATOMS,
    MAX_ATOMS_PER_CONJUNCTION,
    MAX_CONSTRAINT_GROUPS,
    MODALITIES,
    SemanticContractValidationError,
    _evidence_supports_atom,
    _evidence_supports_temporal,
    contract_atom_sha256,
    flatten_patch_paths,
    project_verified_legacy_compatibility,
)
from wahojobs.opportunity_semantic_extraction import (
    EXTRACTION_CONTRACT_VERSION,
    SemanticExtractionValidationError,
    _materialize_atom,
    validate_accepted_evidence_bindings,
)


STAGING_VERSION = "oe_semantic_staging_v0"
STRUCTURAL_VALIDATOR_VERSION = "oe_semantic_staging_v0_structural_validator_v1"
NORMALIZER_VERSION = "oe_semantic_staging_v0_source_normalizer_v1"
SEMANTIC_VERIFICATION_VERSION = "oe_semantic_verification_v0"
RELATION_BUILDER_VERSION = "oe_semantic_staging_v0_relation_builder_v1"
FINALIZER_VERSION = "oe_semantic_staging_v0_finalizer_v1"

PROVISIONAL_STATUSES = frozenset({"provisional", "structurally_invalid"})
NORMALIZATION_STATUSES = frozenset({"resolved", "ambiguous", "unmapped"})
SEMANTIC_DECISIONS = frozenset(
    {"entails", "contradicts", "not_established"}
)
LEXICAL_FAST_PATH_DECISIONS = frozenset({"entails", "abstain"})
RELATION_STATES = frozenset(
    {
        "complete_verified",
        "grounded_incomplete",
        "invalid_proposal",
        "unrepresentable_relation",
    }
)
ASSURANCE_STATES = frozenset(
    {
        "retained_unresolved",
        "semantic_verified",
        "hard_projection_authorized",
        "rejected",
    }
)

# ``hard_projection_authorized`` is a frozen verifier-experiment token.  OE
# Semantic Authority Boundary v1 gives it no eligibility effect and preserves it
# only as an experimental observation in semantic matching packets.

_ATOM_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SemanticStagingValidationError(ValueError):
    """Raised when the offline staging proof receives malformed frozen input."""


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value) -> str:
    if type(value) is not str:
        value = _canonical_json(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fail(path: str, message: str) -> None:
    raise SemanticStagingValidationError(f"{path}: {message}")


def _expect_exact_keys(value, keys, path: str) -> None:
    if type(value) is not dict:
        _fail(path, "must be an object")
    missing = set(keys) - set(value)
    extra = set(value) - set(keys)
    if missing or extra:
        _fail(
            path,
            f"has invalid keys; missing={sorted(missing)} extra={sorted(extra)}",
        )


# Each matcher is server-owned and bounded.  The matched raw value is copied from
# an authenticated quote; a model cannot supply an alternative free-text value.
_SOURCE_VALUE_PATTERNS = {
    ("capability", "capability"): (
        (r"\bmetacognitive communication\b", "analytical_communication"),
        (r"\bstructured communication\b", "analytical_communication"),
        (r"\banalytical communication\b", "analytical_communication"),
        (r"\bLaTeX specialists?\b", "latex_typesetting"),
        (r"\bLaTeX\b", "latex_typesetting"),
        (r"\bfact[ -]?check(?:ing)?\b", "fact_checking"),
        (r"\bfactual accuracy\b", "fact_checking"),
        (r"\bquality assurance\b", "quality_assurance"),
        (r"\bsoftware test(?:ing)?\b", "software_testing"),
    ),
    ("domain_expertise", "domain"): (
        (r"\bsoftware architecture\b", "software"),
        (r"\bsoftware\b", "software"),
        (r"\bmathematics\b", "mathematics"),
    ),
    ("education", "level"): (
        (r"\bbachelor(?:['’]s)?\b", "bachelor"),
        (r"\bmaster(?:['’]s)?\b", "master"),
        (r"\bPh\.?D\.?\b", "doctorate"),
        (r"\bdoctorate\b", "doctorate"),
        (r"\badvanced degree\b", "advanced_degree"),
    ),
    ("education", "field"): (
        (r"\bcomputer science\b", "computer_science"),
        (r"\bmathematics\b", "mathematics"),
        (r"\bclosely related technical field\b", "related_field"),
        (r"\bclosely related engineering field\b", "related_field"),
        (r"\bclosely related field\b", "related_field"),
        (r"\brelated field\b", "related_field"),
        (r"\bsoftware engineering\b", None),
        (r"\belectrical engineering\b", None),
        (r"\bPhysics\b", None),
        (r"\bChemistry\b", None),
        (r"\bSTEM discipline\b", None),
    ),
    ("asset_access", "asset"): (
        (r"\bsecure computer\b", "secure_computer"),
        (r"\bhigh[‑ -]speed internet\b", "high_speed_internet"),
        (r"\bdigital storefront\b", "digital_storefront"),
        (r"\bGoogle Business Profile\b", "google_business_profile"),
    ),
    ("asset_access", "relation"): (
        (r"\bsupply\b", "supply"),
        (r"\bpossess and manage\b", "possess_and_manage"),
        (r"\bpossess\b", "possess"),
        (r"\baccess\b", "access"),
    ),
    ("language_proficiency", "language"): (
        (r"\bEnglish\b", "english"),
        (r"\bItalian\b", None),
        (r"\bIndonesian\b", None),
        (r"\bChinese\b", "chinese"),
        (r"\bFrench\b", "french"),
        (r"\bGalician\b", "galician"),
        (r"\bJapanese\b", "japanese"),
        (r"\bKorean\b", "korean"),
        (r"\bSpanish\b", "spanish"),
    ),
    ("language_proficiency", "proficiency"): (
        (r"\bFluency\b", "fluent"),
        (r"\bfluent\b", "fluent"),
        (r"\bnative\b", "native"),
        (r"\bprofessional(?: working)? proficiency\b", "professional"),
    ),
    ("interest_involvement", "domain"): (
        (r"\bmathematics\b", "mathematics"),
    ),
    ("interest_involvement", "relation"): (
        (r"\blive and breathe\b", "strong_interest"),
        (r"\bstrong interest\b", "strong_interest"),
        (r"\bactive involvement\b", "active_involvement"),
    ),
    ("professional_status", "status"): (
        (r"\bFreelance\b", "freelancer"),
    ),
    ("professional_status", "scope"): (
        (r"\bIndependent Contractor\b", "professional_practice"),
    ),
    ("professional_standing", "standing"): (
        (r"\bpeer[‑ -]reviewed publications\b", "published_author"),
        (r"\bpublished author\b", "published_author"),
    ),
    ("role_activity", "activity"): (
        (r"\bverify factual accuracy\b", "fact_checking"),
        (r"\bfact[ -]?check(?:ing)?\b", "fact_checking"),
        (
            r"\b(?:evaluate|review|assess|challenge|converse with)\b.{0,80}"
            r"\b(?:AI|models?|outputs?)\b",
            "ai_training_evaluation",
        ),
        (r"\bevaluat(?:e|ing) and improve\b.{0,60}\boutputs?\b", "ai_training_evaluation"),
        (r"\bLaTeX (?:rendering|expressions?)\b", "document_typesetting"),
        (r"\bMath Rendering Correction\b", "document_typesetting"),
        (r"\bquality assurance\b", "quality_assurance"),
        (r"\btest(?:ing)?\b.{0,50}\b(?:software|platform|application|digital tool)\b", "software_testing"),
    ),
    ("role_activity", "artifact"): (
        (r"\bAI[ -]generated (?:code|text|outputs?)\b", "ai_output"),
        (r"\bAI outputs?\b", "ai_output"),
        (r"\bmodel outputs?\b", "ai_output"),
        (r"\blanguage models?\b", "ai_output"),
        (r"\bthe model\b", "ai_output"),
        (r"\bLaTeX (?:rendering|expressions?)\b", "document"),
        (r"\btext\b", "document"),
        (r"\bgenerated tasks?\b", "generated_task"),
        (r"\bscoring criteria\b", "scoring_criteria"),
        (r"\bplatform features?\b", "platform_feature"),
        (r"\bdigital tools?\b", "digital_tool"),
        (r"\bsoftware\b", "software"),
    ),
}

_REGISTRY_FIELDS_BY_KIND = {
    "capability": ("capability",),
    "domain_expertise": ("domain",),
    "education": ("level", "field"),
    "asset_access": ("asset", "relation"),
    "language_proficiency": ("language", "locale", "proficiency"),
    "interest_involvement": ("domain", "relation"),
    "professional_status": ("status", "scope"),
    "professional_standing": ("standing",),
    "role_activity": ("activity", "artifact"),
}

_OPTIONAL_REGISTRY_FIELDS = frozenset(
    {("education", "field"), ("language_proficiency", "locale")}
)


def _source_value_records(atom: dict) -> list[dict]:
    records = []
    seen = set()
    for field in _REGISTRY_FIELDS_BY_KIND.get(atom["kind"], ()):
        patterns = _SOURCE_VALUE_PATTERNS.get((atom["kind"], field), ())
        for evidence in atom["evidence"]:
            quote = evidence["quote"]
            for pattern, normalized in patterns:
                for match in re.finditer(pattern, quote, re.IGNORECASE):
                    identity = (
                        field,
                        evidence["source_id"],
                        match.start(),
                        match.end(),
                        normalized,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    records.append(
                        {
                            "field": field,
                            "raw_value": match.group(0),
                            "normalized_value": normalized,
                            "normalization_status": (
                                "resolved" if normalized is not None else "unmapped"
                            ),
                            "source_id": evidence["source_id"],
                            "start": match.start(),
                            "end": match.end(),
                            "quote_sha256": _sha256(quote),
                        }
                    )
    records.sort(
        key=lambda item: (
            item["field"],
            item["source_id"],
            item["start"],
            item["end"],
            item["raw_value"],
        )
    )
    return records


def _normalization_for_atom(atom: dict) -> dict:
    source_values = _source_value_records(atom)
    fields = {}
    payload = atom["typed_payload"]
    for field in _REGISTRY_FIELDS_BY_KIND.get(atom["kind"], ()):
        observed = [item for item in source_values if item["field"] == field]
        proposed_present = field in payload
        proposed = payload.get(field)
        mapped = sorted(
            {
                item["normalized_value"]
                for item in observed
                if item["normalized_value"] is not None
            }
        )
        bound = [
            item
            for item in observed
            if item["normalized_value"] == proposed and proposed is not None
        ]
        if proposed is None:
            if observed:
                status = "ambiguous" if len(mapped) > 1 else "unmapped"
                alignment = "qualifier_loss"
            elif (atom["kind"], field) in _OPTIONAL_REGISTRY_FIELDS:
                status = "resolved"
                alignment = "exact_absence"
            else:
                status = "unmapped"
                alignment = "not_established"
        elif bound:
            status = "resolved"
            alignment = "exact"
        elif observed:
            status = "ambiguous" if len(mapped) > 1 else "unmapped"
            alignment = "source_value_substitution"
        else:
            status = "unmapped"
            alignment = "not_established"
        fields[field] = {
            "status": status,
            "alignment": alignment,
            "proposed_value": copy.deepcopy(proposed),
            "proposed_field_present": proposed_present,
            "observed_source_values": copy.deepcopy(observed),
            "bound_source_values": copy.deepcopy(bound),
        }
    statuses = {item["status"] for item in fields.values()}
    alignments = {item["alignment"] for item in fields.values()}
    if not fields or statuses <= {"resolved"}:
        overall = "resolved"
    elif "ambiguous" in statuses:
        overall = "ambiguous"
    else:
        overall = "unmapped"
    qualifier_complete = not (
        alignments
        & {"qualifier_loss", "source_value_substitution", "not_established"}
    ) and overall == "resolved"
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "status": overall,
        "qualifier_complete": qualifier_complete,
        "fields": fields,
        "source_values": source_values,
    }


def _lexical_fast_path(atom: dict) -> dict:
    failures = []
    for index, evidence in enumerate(atom["evidence"]):
        try:
            _evidence_supports_atom(atom, evidence["quote"], f"evidence[{index}]")
            _evidence_supports_temporal(
                atom, evidence["quote"], f"evidence[{index}]"
            )
        except (SemanticContractValidationError, KeyError) as exc:
            failures.append(str(exc))
            continue
        return {"decision": "entails", "reason": None}
    return {
        "decision": "abstain",
        "reason": failures[0] if failures else "no_authenticated_evidence",
    }


def stage_provisional_atoms(
    model_payload: dict,
    source_packet: dict,
    evidence_bindings: list[dict],
) -> dict:
    """Authenticate and retain every proposed identity in a provisional ledger."""

    bindings = validate_accepted_evidence_bindings(source_packet, evidence_bindings)
    _expect_exact_keys(
        model_payload,
        {"extraction_version", "atoms", "constraint_groups"},
        "model",
    )
    if model_payload["extraction_version"] != EXTRACTION_CONTRACT_VERSION:
        _fail("model.extraction_version", "is not the frozen extraction contract")
    raw_atoms = model_payload["atoms"]
    if type(raw_atoms) is not list or len(raw_atoms) > MAX_ATOMS:
        _fail("model.atoms", f"must be a list of at most {MAX_ATOMS}")

    ledger = []
    sources = {}
    proposal_indices = defaultdict(list)
    for index, raw_atom in enumerate(raw_atoms):
        proposal_id = (
            raw_atom.get("id")
            if type(raw_atom) is dict and type(raw_atom.get("id")) is str
            else f"atom_index_{index}"
        )
        ledger_id = f"p{index:03d}:{proposal_id}"
        proposal_indices[proposal_id].append(index)
        try:
            atom, atom_sources = _materialize_atom(raw_atom, index, bindings)
        except SemanticExtractionValidationError as exc:
            ledger.append(
                {
                    "ledger_id": ledger_id,
                    "proposal_id": proposal_id,
                    "proposal_index": index,
                    "status": "structurally_invalid",
                    "raw_proposal": copy.deepcopy(raw_atom),
                    "atom": None,
                    "atom_sha256": _sha256(raw_atom),
                    "authentication": {
                        "status": "invalid",
                        "reason": str(exc),
                    },
                    "normalization": {
                        "normalizer_version": NORMALIZER_VERSION,
                        "status": "unmapped",
                        "qualifier_complete": False,
                        "fields": {},
                        "source_values": [],
                    },
                    "lexical_fast_path": {
                        "decision": "abstain",
                        "reason": "structurally_invalid",
                    },
                    "semantic_verification": {"decision": "pending"},
                    "assurance": "retained_unresolved",
                }
            )
            continue
        sources.update(atom_sources)
        ledger.append(
            {
                "ledger_id": ledger_id,
                "proposal_id": proposal_id,
                "proposal_index": index,
                "status": "provisional",
                "raw_proposal": copy.deepcopy(raw_atom),
                "atom": atom,
                "atom_sha256": contract_atom_sha256(atom),
                "authentication": {
                    "status": "authenticated",
                    "source_ids": sorted(atom_sources),
                    "reference_count": len(atom["evidence"]),
                },
                "normalization": _normalization_for_atom(atom),
                "lexical_fast_path": _lexical_fast_path(atom),
                "semantic_verification": {"decision": "pending"},
                "assurance": "retained_unresolved",
            }
        )

    duplicate_ids = {
        proposal_id
        for proposal_id, indices in proposal_indices.items()
        if len(indices) > 1
    }
    for item in ledger:
        if item["proposal_id"] in duplicate_ids:
            item["status"] = "structurally_invalid"
            item["authentication"] = {
                "status": "invalid",
                "reason": "duplicate_proposal_id",
            }

    return {
        "staging_version": STAGING_VERSION,
        "structural_validator_version": STRUCTURAL_VALIDATOR_VERSION,
        "accepted_binding_count": len(bindings),
        "accepted_evidence_catalog": [
            sources[source_id] for source_id in sorted(sources)
        ],
        "provisional_atoms": ledger,
        "atom_accounting": {
            "proposed_count": len(raw_atoms),
            "ledger_count": len(ledger),
            "accounted_proposal_indices": [
                item["proposal_index"] for item in ledger
            ],
            "complete_and_exact": len(raw_atoms) == len(ledger),
        },
        "raw_constraint_groups": copy.deepcopy(model_payload["constraint_groups"]),
    }


def semantic_verification_request(staging: dict) -> dict:
    """Build the bounded verifier input; it contains no groups or authority fields."""

    atoms = []
    for item in staging["provisional_atoms"]:
        if item["status"] != "provisional":
            continue
        atom = item["atom"]
        atoms.append(
            {
                "ledger_id": item["ledger_id"],
                "atom_sha256": item["atom_sha256"],
                "subject": atom["subject"],
                "kind": atom["kind"],
                "typed_payload": copy.deepcopy(atom["typed_payload"]),
                "polarity": atom["polarity"],
                "temporal": atom["temporal"],
                "evidence": copy.deepcopy(atom["evidence"]),
            }
        )
    return {
        "verification_version": SEMANTIC_VERIFICATION_VERSION,
        "atoms": atoms,
    }


def verification_from_reviewed_labels(
    staging: dict,
    reviewed_labels: dict,
) -> dict:
    """Bind frozen human labels to immutable verifier-request identities."""

    _expect_exact_keys(
        reviewed_labels,
        {"entails", "contradicts", "not_established"},
        "reviewed_labels",
    )
    decisions_by_proposal = {}
    for decision in sorted(SEMANTIC_DECISIONS):
        values = reviewed_labels[decision]
        if type(values) is not list or any(type(value) is not str for value in values):
            _fail(f"reviewed_labels.{decision}", "must be a list of atom IDs")
        for proposal_id in values:
            if proposal_id in decisions_by_proposal:
                _fail("reviewed_labels", f"duplicates atom {proposal_id!r}")
            decisions_by_proposal[proposal_id] = decision

    provisional = [
        item for item in staging["provisional_atoms"] if item["status"] == "provisional"
    ]
    proposal_ids = {item["proposal_id"] for item in provisional}
    if set(decisions_by_proposal) != proposal_ids:
        _fail(
            "reviewed_labels",
            "must account for every and only structurally valid proposal",
        )
    decisions = []
    for item in provisional:
        decision = decisions_by_proposal[item["proposal_id"]]
        decisions.append(
            {
                "ledger_id": item["ledger_id"],
                "atom_sha256": item["atom_sha256"],
                "decision": decision,
                "qualifier_decisions": {
                    "payload": decision,
                    "polarity": decision,
                    "temporal": decision,
                },
            }
        )
    return {
        "verification_version": SEMANTIC_VERIFICATION_VERSION,
        "decisions": decisions,
    }


def validate_semantic_verification(staging: dict, verification: dict) -> dict:
    """Validate that decisions classify, but never mutate, provisional atoms."""

    _expect_exact_keys(
        verification,
        {"verification_version", "decisions"},
        "verification",
    )
    if verification["verification_version"] != SEMANTIC_VERIFICATION_VERSION:
        _fail("verification.verification_version", "is not supported")
    raw_decisions = verification["decisions"]
    if type(raw_decisions) is not list:
        _fail("verification.decisions", "must be a list")
    provisional = {
        item["ledger_id"]: item
        for item in staging["provisional_atoms"]
        if item["status"] == "provisional"
    }
    indexed = {}
    for index, raw in enumerate(raw_decisions):
        path = f"verification.decisions[{index}]"
        _expect_exact_keys(
            raw,
            {"ledger_id", "atom_sha256", "decision", "qualifier_decisions"},
            path,
        )
        ledger_id = raw["ledger_id"]
        if ledger_id not in provisional or ledger_id in indexed:
            _fail(f"{path}.ledger_id", "is unknown or duplicated")
        if raw["atom_sha256"] != provisional[ledger_id]["atom_sha256"]:
            _fail(f"{path}.atom_sha256", "does not match the immutable atom")
        decision = raw["decision"]
        if decision not in SEMANTIC_DECISIONS:
            _fail(f"{path}.decision", "is outside the closed decision set")
        qualifiers = raw["qualifier_decisions"]
        _expect_exact_keys(
            qualifiers,
            {"payload", "polarity", "temporal"},
            f"{path}.qualifier_decisions",
        )
        if any(value not in SEMANTIC_DECISIONS for value in qualifiers.values()):
            _fail(f"{path}.qualifier_decisions", "contains an invalid decision")
        indexed[ledger_id] = copy.deepcopy(raw)
    if set(indexed) != set(provisional):
        _fail("verification.decisions", "must account for every provisional atom")
    return {
        "verification_version": SEMANTIC_VERIFICATION_VERSION,
        "decisions": [indexed[key] for key in sorted(indexed)],
    }


def apply_semantic_verification(staging: dict, verification: dict) -> dict:
    validated = validate_semantic_verification(staging, verification)
    decisions = {item["ledger_id"]: item for item in validated["decisions"]}
    result = copy.deepcopy(staging)
    for item in result["provisional_atoms"]:
        decision = decisions.get(item["ledger_id"])
        if decision is None:
            continue
        item["semantic_verification"] = decision
        if decision["decision"] == "entails":
            item["assurance"] = "semantic_verified"
        else:
            item["assurance"] = "rejected"
    result["semantic_verification"] = validated
    return result


def _group_core(raw_group: dict, path: str) -> dict:
    if type(raw_group) is not dict:
        _fail(path, "must be an object")
    core = {
        key: copy.deepcopy(raw_group[key])
        for key in ("modality", "any_of")
        if key in raw_group
    }
    _expect_exact_keys(core, {"modality", "any_of"}, path)
    if core["modality"] not in MODALITIES:
        _fail(f"{path}.modality", "is invalid")
    alternatives = core["any_of"]
    if type(alternatives) is not list or not alternatives:
        _fail(f"{path}.any_of", "must be non-empty")
    if len(alternatives) > MAX_ALTERNATIVES_PER_GROUP:
        _fail(f"{path}.any_of", "has too many alternatives")
    seen = set()
    for alternative_index, alternative in enumerate(alternatives):
        alt_path = f"{path}.any_of[{alternative_index}]"
        _expect_exact_keys(alternative, {"all_of"}, alt_path)
        conjunction = alternative["all_of"]
        if type(conjunction) is not list or not conjunction:
            _fail(f"{alt_path}.all_of", "must be non-empty")
        if len(conjunction) > MAX_ATOMS_PER_CONJUNCTION:
            _fail(f"{alt_path}.all_of", "has too many atoms")
        if any(type(atom_id) is not str for atom_id in conjunction):
            _fail(f"{alt_path}.all_of", "must contain atom IDs")
        identity = tuple(sorted(conjunction))
        if len(identity) != len(set(identity)) or identity in seen:
            _fail(alt_path, "duplicates an atom or alternative")
        seen.add(identity)
    return core


def _dnf_identity(group: dict) -> tuple:
    return tuple(
        sorted(tuple(sorted(alternative["all_of"])) for alternative in group["any_of"])
    )


def _group_atom_ids(group: dict) -> list[str]:
    return [
        atom_id
        for alternative in group["any_of"]
        for atom_id in alternative["all_of"]
    ]


def _derive_authoritative_modality(group: dict, atoms_by_id: dict) -> str | None:
    atoms = [atoms_by_id[atom_id]["atom"] for atom_id in _group_atom_ids(group)]
    if atoms and all(atom["kind"] == "role_activity" for atom in atoms):
        return "descriptive"
    if any(atom["kind"] == "role_activity" for atom in atoms):
        return None
    text = " ".join(
        evidence["quote"] for atom in atoms for evidence in atom["evidence"]
    )
    preferred = re.search(
        r"\b(?:ideal|preferred|signal(?:s)? fit|bonus|nice to have)\b",
        text,
        re.IGNORECASE,
    ) is not None
    required = re.search(
        r"\b(?:required|essential|must|need(?:s|ed)?|we(?:'|’)re looking|"
        r"looking for|you(?:'|’)ll supply|you will supply)\b",
        text,
        re.IGNORECASE,
    ) is not None
    if preferred == required:
        return None
    return "preferred" if preferred else "required"


def role_activity_composition_supported(atom: dict) -> bool:
    """Require evidence of both a closed action and its closed object."""

    if atom["kind"] != "role_activity":
        return True
    payload = atom["typed_payload"]
    quotes = " ".join(item["quote"] for item in atom["evidence"])
    activity = payload["activity"]
    artifact = payload["artifact"]
    action_patterns = {
        "ai_training_evaluation": r"\b(?:evaluate|review|assess|challenge|converse with)\b",
        "document_typesetting": r"\b(?:LaTeX|typesett|rendering correction)\b",
        "fact_checking": r"\b(?:fact[ -]?check|factual accuracy)\b",
        "quality_assurance": r"\bquality assurance\b",
        "software_testing": r"\btest(?:ing)?\b",
    }
    artifact_patterns = {
        "ai_output": r"\b(?:AI[ -]generated|AI outputs?|model outputs?|language models?|the model)\b",
        "document": r"\b(?:document|LaTeX|text)\b",
        "generated_task": r"\bgenerated tasks?\b",
        "scoring_criteria": r"\bscoring criteria\b",
        "digital_tool": r"\bdigital tools?\b",
        "platform_feature": r"\bplatform features?\b",
        "software": r"\b(?:software|applications?)\b",
    }
    if activity not in action_patterns or artifact not in artifact_patterns:
        return False
    if re.search(action_patterns[activity], quotes, re.IGNORECASE) is None:
        return False
    if re.search(artifact_patterns[artifact], quotes, re.IGNORECASE) is None:
        return False
    if activity == "software_testing" and re.search(
        r"\b(?:software|platform features?|applications?|digital tools?)\b",
        quotes,
        re.IGNORECASE,
    ) is None:
        return False
    return True


def _deduplicated_source_values(atoms, field: str) -> list[dict]:
    values = {}
    for item in atoms:
        for source_value in item["normalization"]["source_values"]:
            if source_value["field"] != field:
                continue
            identity = (
                source_value["source_id"],
                source_value["start"],
                source_value["end"],
                source_value["normalized_value"],
            )
            values[identity] = source_value
    return [values[key] for key in sorted(values)]


def _education_source_branches(group: dict, atoms_by_id: dict) -> list[dict] | None:
    atom_items = [atoms_by_id[atom_id] for atom_id in _group_atom_ids(group)]
    if not atom_items or any(item["atom"]["kind"] != "education" for item in atom_items):
        return None
    levels = _deduplicated_source_values(atom_items, "level")
    fields = _deduplicated_source_values(atom_items, "field")
    if not levels:
        return None
    dimensions = [(level, field) for level in levels for field in (fields or [None])]
    branches = []
    for level, field in dimensions:
        matching = []
        for alternative in group["any_of"]:
            if len(alternative["all_of"]) != 1:
                continue
            atom_id = alternative["all_of"][0]
            payload = atoms_by_id[atom_id]["atom"]["typed_payload"]
            if payload.get("level") != level["normalized_value"]:
                continue
            if field is not None and payload.get("field") != field["normalized_value"]:
                continue
            matching.append(atom_id)
        values = [level] + ([] if field is None else [field])
        branches.append(
            {
                "source_branch_id": "sb:" + _sha256(values)[:24],
                "source_values": copy.deepcopy(values),
                "proposed_atom_ids": matching,
                "coverage_state": "matched" if len(matching) == 1 else "unresolved",
            }
        )
    return branches


def _default_source_branches(group: dict) -> list[dict]:
    branches = []
    for alternative in group["any_of"]:
        atom_ids = copy.deepcopy(alternative["all_of"])
        branches.append(
            {
                "source_branch_id": "sb:" + _sha256(atom_ids)[:24],
                "source_values": [],
                "proposed_atom_ids": atom_ids,
                "coverage_state": "matched",
            }
        )
    return branches


def _grouping_observation(group: dict, frozen_grouping: dict | None) -> str:
    if not frozen_grouping:
        return "not_available"
    target_dnf = _dnf_identity(group)
    target_ids = set(_group_atom_ids(group))
    overlap = False
    subset = False
    for index, raw in enumerate(frozen_grouping.get("constraint_groups") or []):
        try:
            candidate = _group_core(raw, f"frozen_grouping.constraint_groups[{index}]")
        except SemanticStagingValidationError:
            continue
        candidate_ids = set(_group_atom_ids(candidate))
        if candidate["modality"] == group["modality"] and _dnf_identity(candidate) == target_dnf:
            return "exact"
        if candidate_ids & target_ids:
            overlap = True
            if candidate_ids < target_ids:
                subset = True
    if subset:
        return "subset_after_legacy_deletion"
    return "overlap_different" if overlap else "absent_after_legacy_deletion"


def construct_relations(
    verified_staging: dict,
    frozen_grouping: dict | None = None,
) -> dict:
    """Construct relation states over the complete provisional atom ledger."""

    raw_groups = verified_staging["raw_constraint_groups"]
    if type(raw_groups) is not list or len(raw_groups) > MAX_CONSTRAINT_GROUPS:
        _fail("raw_constraint_groups", "is not bounded")
    atoms_by_id = {}
    for item in verified_staging["provisional_atoms"]:
        atoms_by_id.setdefault(item["proposal_id"], item)
    memberships = Counter()
    relations = []
    for index, raw_group in enumerate(raw_groups):
        relation_id = f"r{index:03d}"
        try:
            group = _group_core(raw_group, f"raw_constraint_groups[{index}]")
        except SemanticStagingValidationError as exc:
            relations.append(
                {
                    "relation_id": relation_id,
                    "state": "unrepresentable_relation",
                    "reason_codes": [str(exc)],
                    "proposal": copy.deepcopy(raw_group),
                    "source_branches": [],
                    "grouping_observation": "not_compared",
                    "weakening_detected": False,
                }
            )
            continue
        atom_ids = _group_atom_ids(group)
        for atom_id in atom_ids:
            memberships[atom_id] += 1
        reasons = []
        if len(atom_ids) != len(set(atom_ids)):
            reasons.append("duplicate_atom_membership")
        missing = [atom_id for atom_id in atom_ids if atom_id not in atoms_by_id]
        if missing:
            reasons.append("unknown_atom_ids:" + ",".join(sorted(missing)))
        items = [atoms_by_id[atom_id] for atom_id in atom_ids if atom_id in atoms_by_id]
        source_branches = _education_source_branches(group, atoms_by_id)
        if source_branches is None:
            source_branches = _default_source_branches(group)
        unresolved_source_branches = [
            item for item in source_branches if item["coverage_state"] != "matched"
        ]
        mapped_branch_identities = [
            tuple(item["proposed_atom_ids"])
            for item in source_branches
            if item["coverage_state"] == "matched"
        ]
        source_logic_exact = (
            not unresolved_source_branches
            and len(mapped_branch_identities) == len(set(mapped_branch_identities))
            and set(mapped_branch_identities) == set(_dnf_identity(group))
        )
        decisions = {
            item["semantic_verification"].get("decision", "pending")
            for item in items
        }
        if any(item["status"] != "provisional" for item in items):
            reasons.append("structurally_invalid_member")
        if decisions & {"contradicts", "not_established"}:
            reasons.append("reviewed_unsupported_member")
        if "pending" in decisions:
            reasons.append("semantic_verification_pending")
        unresolved_normalization = [
            item["proposal_id"]
            for item in items
            if item["normalization"]["status"] != "resolved"
            or not item["normalization"]["qualifier_complete"]
        ]
        if unresolved_normalization:
            reasons.append(
                "normalization_or_qualifier_unresolved:"
                + ",".join(sorted(unresolved_normalization))
            )
        if not source_logic_exact:
            reasons.append("source_logic_incomplete")
        authoritative_modality = (
            _derive_authoritative_modality(group, atoms_by_id)
            if not missing
            else None
        )
        if authoritative_modality is None:
            reasons.append("authoritative_modality_unresolved")
        elif authoritative_modality != group["modality"]:
            reasons.append("proposed_modality_not_authoritative")
        unsafe_compositions = [
            item["proposal_id"]
            for item in items
            if not role_activity_composition_supported(item["atom"])
        ]
        if unsafe_compositions:
            reasons.append(
                "role_activity_composition_unresolved:"
                + ",".join(sorted(unsafe_compositions))
            )

        if missing or "duplicate_atom_membership" in reasons:
            state = "invalid_proposal"
        elif decisions & {"contradicts", "not_established"}:
            state = "invalid_proposal"
        elif reasons:
            state = "grounded_incomplete"
        else:
            state = "complete_verified"
            for item in items:
                item["assurance"] = "hard_projection_authorized"
        relations.append(
            {
                "relation_id": relation_id,
                "state": state,
                "reason_codes": reasons,
                "proposal": group,
                "proposal_sha256": _sha256(group),
                "authoritative_modality": authoritative_modality,
                "source_branches": source_branches,
                "source_logic_exact": source_logic_exact,
                "grouping_observation": _grouping_observation(
                    group, frozen_grouping
                ),
                "weakening_detected": False,
            }
        )

    multiply_assigned = sorted(
        atom_id for atom_id, count in memberships.items() if count > 1
    )
    unassigned = sorted(
        item["proposal_id"]
        for item in verified_staging["provisional_atoms"]
        if memberships[item["proposal_id"]] == 0
    )
    if multiply_assigned:
        for relation in relations:
            if set(_group_atom_ids(relation.get("proposal") or {"any_of": []})) & set(
                multiply_assigned
            ):
                relation["state"] = "invalid_proposal"
                relation["reason_codes"].append("atom_assigned_to_multiple_relations")
    source_branch_count = sum(len(item["source_branches"]) for item in relations)
    accounted_source_branch_count = sum(
        1
        for relation in relations
        for branch in relation["source_branches"]
        if branch["coverage_state"] in {"matched", "unresolved"}
    )
    return {
        "relation_builder_version": RELATION_BUILDER_VERSION,
        "relations": relations,
        "atom_membership": {
            "multiply_assigned_atom_ids": multiply_assigned,
            "unassigned_atom_ids": unassigned,
        },
        "source_branch_accounting": {
            "source_branch_count": source_branch_count,
            "accounted_source_branch_count": accounted_source_branch_count,
            "complete_and_exact": source_branch_count
            == accounted_source_branch_count,
        },
    }


def finalize_verified_relations(
    verified_staging: dict,
    relation_ledger: dict,
) -> dict:
    """Admit only complete verified groups, then run the v0 projector."""

    atoms_by_id = {
        item["proposal_id"]: item for item in verified_staging["provisional_atoms"]
    }
    complete_relations = [
        item
        for item in relation_ledger["relations"]
        if item["state"] == "complete_verified"
    ]
    accepted_atom_ids = []
    groups = []
    authoritative_modalities = {}
    relation_by_final_group = []
    for relation in complete_relations:
        group = relation["proposal"]
        final_group_index = len(groups)
        groups.append(copy.deepcopy(group))
        relation_by_final_group.append(relation["relation_id"])
        for atom_id in _group_atom_ids(group):
            if atom_id not in accepted_atom_ids:
                accepted_atom_ids.append(atom_id)
            authoritative_modalities[atom_id] = relation["authoritative_modality"]
    atoms = [copy.deepcopy(atoms_by_id[atom_id]["atom"]) for atom_id in accepted_atom_ids]
    catalog_by_id = {
        source["id"]: source
        for source in verified_staging["accepted_evidence_catalog"]
        if source["id"]
        in {
            evidence["source_id"]
            for atom in atoms
            for evidence in atom["evidence"]
        }
    }
    catalog = [catalog_by_id[source_id] for source_id in sorted(catalog_by_id)]
    assurances = {
        atom_id: {
            "atom_sha256": atoms_by_id[atom_id]["atom_sha256"],
            "semantic_decision": atoms_by_id[atom_id]["semantic_verification"][
                "decision"
            ],
            "qualifier_assurance": "complete",
            "projection_assurance": "hard_projection_authorized",
        }
        for atom_id in accepted_atom_ids
    }
    raw_contract = {
        "contract_version": CONTRACT_VERSION,
        "atoms": atoms,
        "constraint_groups": groups,
    }
    projection = project_verified_legacy_compatibility(
        raw_contract,
        catalog,
        assurances,
        authoritative_modalities,
    )
    final_group_logic = sorted(
        (group["modality"], _dnf_identity(group))
        for group in projection["normalized_contract"]["constraint_groups"]
    )
    source_group_logic = sorted(
        (item["proposal"]["modality"], _dnf_identity(item["proposal"]))
        for item in complete_relations
    )
    weakening_detected = final_group_logic != source_group_logic
    if weakening_detected:
        raise AssertionError("finalization changed complete relation logic")
    incomplete_relation_ids = {
        item["relation_id"]
        for item in relation_ledger["relations"]
        if item["state"] != "complete_verified"
    }
    if incomplete_relation_ids & set(relation_by_final_group):
        raise AssertionError("an incomplete relation entered the final contract")
    return {
        "finalizer_version": FINALIZER_VERSION,
        "final_contract": projection["normalized_contract"],
        "accepted_evidence_catalog": catalog,
        "verified_atom_assurances": assurances,
        "authoritative_modalities": authoritative_modalities,
        "final_relation_ids": relation_by_final_group,
        "excluded_relation_ids": sorted(incomplete_relation_ids),
        "projection": projection,
        "logic_weakening_detected": weakening_detected,
    }


def replay_case(case: dict, reviewed_labels: dict) -> dict:
    staged = stage_provisional_atoms(
        case["raw_extraction"],
        case["source_packet"],
        case["accepted_evidence_bindings"],
    )
    request = semantic_verification_request(staged)
    verification = verification_from_reviewed_labels(staged, reviewed_labels)
    verified = apply_semantic_verification(staged, verification)
    relations = construct_relations(verified, case.get("raw_grouping"))
    finalization = finalize_verified_relations(verified, relations)
    decisions = Counter(
        item["semantic_verification"].get("decision", "pending")
        for item in verified["provisional_atoms"]
    )
    lexical = Counter(
        item["lexical_fast_path"]["decision"]
        for item in verified["provisional_atoms"]
    )
    normalization = Counter(
        item["normalization"]["status"]
        for item in verified["provisional_atoms"]
    )
    relation_states = Counter(item["state"] for item in relations["relations"])
    projected_atom_ids = {
        atom_id
        for outcome in finalization["projection"]["group_outcomes"]
        if outcome["state"] == "projected"
        for atom_id in outcome["atom_ids"]
    }
    unsupported_ids = {
        item["proposal_id"]
        for item in verified["provisional_atoms"]
        if item["semantic_verification"].get("decision")
        in {"contradicts", "not_established"}
    }
    supported_ids = {
        item["proposal_id"]
        for item in verified["provisional_atoms"]
        if item["semantic_verification"].get("decision") == "entails"
    }
    legacy_validated_ids = set(
        case.get("atom_validation", {}).get("validated_atom_ids") or []
    )
    source_values = [
        source_value
        for item in verified["provisional_atoms"]
        for source_value in item["normalization"]["source_values"]
    ]
    finalized_ids = {
        atom["id"] for atom in finalization["final_contract"]["atoms"]
    }
    finalized_integrity_violations = [
        item["proposal_id"]
        for item in verified["provisional_atoms"]
        if item["proposal_id"] in finalized_ids
        and not item["normalization"]["qualifier_complete"]
    ]
    projected_groups = [
        item
        for item in finalization["projection"]["group_outcomes"]
        if item["state"] == "projected"
    ]
    correct_projected_groups = [
        item
        for item in projected_groups
        if not (set(item["atom_ids"]) & unsupported_ids)
    ]
    incomplete_relation_ids = {
        item["relation_id"]
        for item in relations["relations"]
        if item["state"] != "complete_verified"
    }
    projected_from_incomplete = sorted(
        incomplete_relation_ids & set(finalization["final_relation_ids"])
    )
    return {
        "canonical_opportunity_id": case["canonical_opportunity_id"],
        "verification_request": request,
        "staging": verified,
        "relations": relations,
        "finalization": finalization,
        "metrics": {
            "evidence_authentication_exact": all(
                item["authentication"]["status"] == "authenticated"
                for item in verified["provisional_atoms"]
            ),
            "provisional_atom_count": len(verified["provisional_atoms"]),
            "provisional_atom_accounting_complete": verified["atom_accounting"][
                "complete_and_exact"
            ],
            "semantic_decisions": dict(decisions),
            "lexical_fast_path": dict(lexical),
            "legacy_semantic_validator": {
                "reviewed_supported_accepted": len(
                    supported_ids & legacy_validated_ids
                ),
                "reviewed_supported_rejected": len(
                    supported_ids - legacy_validated_ids
                ),
                "reviewed_unsupported_accepted": len(
                    unsupported_ids & legacy_validated_ids
                ),
                "reviewed_unsupported_rejected": len(
                    unsupported_ids - legacy_validated_ids
                ),
            },
            "normalization_statuses": dict(normalization),
            "relation_states": dict(relation_states),
            "source_branch_accounting_complete": relations[
                "source_branch_accounting"
            ]["complete_and_exact"],
            "source_value_count": len(source_values),
            "final_atom_count": len(finalized_ids),
            "final_relation_count": len(finalization["final_relation_ids"]),
            "projected_group_count": len(projected_groups),
            "correct_projected_group_count": len(correct_projected_groups),
            "unsupported_projected_atom_ids": sorted(
                projected_atom_ids & unsupported_ids
            ),
            "finalized_source_value_or_qualifier_violations": sorted(
                finalized_integrity_violations
            ),
            "logic_weakening_detected": finalization[
                "logic_weakening_detected"
            ],
            "projected_from_incomplete_relation_ids": projected_from_incomplete,
            "legacy_patch_paths": flatten_patch_paths(
                finalization["projection"]["legacy_patch"]
            ),
        },
    }


def aggregate_replay(case_results: dict[str, dict]) -> dict:
    cases = list(case_results.values())
    proposed = sum(item["metrics"]["provisional_atom_count"] for item in cases)
    decision_counts = Counter()
    lexical_counts = Counter()
    normalization_counts = Counter()
    relation_counts = Counter()
    grouping_observations = Counter()
    source_branch_count = 0
    accounted_source_branch_count = 0
    projected = 0
    correct_projected = 0
    unsupported_projected = []
    integrity_violations = []
    weakening = []
    final_atoms = 0
    legacy_validator = Counter()
    projected_from_incomplete = []
    notable_values = defaultdict(list)
    fact_checking_ai_output = []
    for case_id, result in case_results.items():
        metrics = result["metrics"]
        decision_counts.update(metrics["semantic_decisions"])
        lexical_counts.update(metrics["lexical_fast_path"])
        legacy_validator.update(metrics["legacy_semantic_validator"])
        normalization_counts.update(metrics["normalization_statuses"])
        relation_counts.update(metrics["relation_states"])
        grouping_observations.update(
            relation["grouping_observation"]
            for relation in result["relations"]["relations"]
        )
        branches = result["relations"]["source_branch_accounting"]
        source_branch_count += branches["source_branch_count"]
        accounted_source_branch_count += branches["accounted_source_branch_count"]
        projected += metrics["projected_group_count"]
        correct_projected += metrics["correct_projected_group_count"]
        final_atoms += metrics["final_atom_count"]
        unsupported_projected.extend(
            f"{case_id}:{atom_id}"
            for atom_id in metrics["unsupported_projected_atom_ids"]
        )
        integrity_violations.extend(
            f"{case_id}:{atom_id}"
            for atom_id in metrics[
                "finalized_source_value_or_qualifier_violations"
            ]
        )
        if metrics["logic_weakening_detected"]:
            weakening.append(case_id)
        projected_from_incomplete.extend(
            f"{case_id}:{relation_id}"
            for relation_id in metrics["projected_from_incomplete_relation_ids"]
        )
        for atom in result["staging"]["provisional_atoms"]:
            for source_value in atom["normalization"]["source_values"]:
                key = source_value["raw_value"].casefold()
                if key in {"italian", "indonesian", "chemistry", "physics"}:
                    notable_values[key].append(
                        {
                            "case_id": case_id,
                            "atom_id": atom["proposal_id"],
                            "raw_value": source_value["raw_value"],
                            "status": source_value["normalization_status"],
                        }
                    )
        for atom in result["finalization"]["final_contract"]["atoms"]:
            if atom["kind"] == "role_activity" and atom["typed_payload"] == {
                "activity": "fact_checking",
                "artifact": "ai_output",
            }:
                fact_checking_ai_output.append(
                    {"case_id": case_id, "atom_id": atom["id"]}
                )

    supported = decision_counts["entails"]
    retained_supported = sum(
        1
        for result in cases
        for atom in result["staging"]["provisional_atoms"]
        if atom["semantic_verification"].get("decision") == "entails"
        and atom["assurance"]
        in {"semantic_verified", "hard_projection_authorized"}
    )
    return {
        "case_count": len(cases),
        "provisional_atoms": {
            "proposed": proposed,
            "accounted": sum(
                item["staging"]["atom_accounting"]["ledger_count"]
                for item in cases
            ),
            "accounting_rate": (
                sum(
                    item["staging"]["atom_accounting"]["ledger_count"]
                    for item in cases
                )
                / proposed
                if proposed
                else 1.0
            ),
        },
        "evidence_authentication": {
            "exact_case_count": sum(
                item["metrics"]["evidence_authentication_exact"] for item in cases
            ),
            "case_count": len(cases),
        },
        "semantic_verification": dict(decision_counts),
        "lexical_fast_path": dict(lexical_counts),
        "legacy_semantic_validator": dict(legacy_validator),
        "supported_proposal_retention": {
            "supported": supported,
            "retained_supported_or_unresolved": retained_supported,
            "rate": retained_supported / supported if supported else 1.0,
        },
        "normalization": {
            "atom_statuses": dict(normalization_counts),
            "notable_source_values": dict(notable_values),
            "finalized_source_value_substitution_or_qualifier_loss": integrity_violations,
        },
        "relations": {
            "states": dict(relation_counts),
            "frozen_grouping_observations": dict(grouping_observations),
            "source_branches": source_branch_count,
            "accounted_source_branches": accounted_source_branch_count,
            "source_branch_accounting_rate": (
                accounted_source_branch_count / source_branch_count
                if source_branch_count
                else 1.0
            ),
            "logic_weakening_cases": weakening,
            "projected_from_incomplete_relation_ids": projected_from_incomplete,
        },
        "final_contract": {
            "retained_atom_count": final_atoms,
            "fact_checking_ai_output": fact_checking_ai_output,
        },
        "projection": {
            "projected_groups": projected,
            "correct_projected_groups": correct_projected,
            "conditional_precision": (
                correct_projected / projected if projected else 1.0
            ),
            "reviewed_unsupported_projected_atom_ids": unsupported_projected,
            "unsafe_hard_gates": unsupported_projected,
        },
        "offline_controls": {
            "provider_calls": 0,
            "network_calls": 0,
            "database_reads": 0,
            "database_writes": 0,
        },
    }


__all__ = [
    "ASSURANCE_STATES",
    "FINALIZER_VERSION",
    "LEXICAL_FAST_PATH_DECISIONS",
    "NORMALIZATION_STATUSES",
    "NORMALIZER_VERSION",
    "PROVISIONAL_STATUSES",
    "RELATION_BUILDER_VERSION",
    "RELATION_STATES",
    "SEMANTIC_DECISIONS",
    "SEMANTIC_VERIFICATION_VERSION",
    "STAGING_VERSION",
    "STRUCTURAL_VALIDATOR_VERSION",
    "SemanticStagingValidationError",
    "aggregate_replay",
    "apply_semantic_verification",
    "construct_relations",
    "finalize_verified_relations",
    "replay_case",
    "role_activity_composition_supported",
    "semantic_verification_request",
    "stage_provisional_atoms",
    "validate_semantic_verification",
    "verification_from_reviewed_labels",
]
