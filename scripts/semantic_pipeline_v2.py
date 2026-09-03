"""Complete, isolated semantic pipeline; not imported by production matching.

Stages are explicit: capture evidence, prepare/provision unique opportunities,
prepare/execute per-profile assessments, then integrate the separate legacy base.
No provider operation is implicit in preparation, cache lookup, replay or imports.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import frozen_semantic_ranking_v1 as downstream
import semantic_development_core as core
import semantic_pipeline_evidence as evidence
from wahojobs import opportunity_semantic_extraction as extraction

VERSION = "wahojobs_semantic_pipeline_method_v2"
CACHE_VERSION = "semantic_pipeline_extraction_cache_v1"
ENDPOINT = "https://api.openai.com/v1/responses"
EXTRACTION_CONFIG = {"model": "gpt-5.6-terra", "reasoning": {"effort": "low"},
                     "store": False, "max_output_tokens": 12000}
ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_RECIPE_FILES = (
    "scripts/semantic_pipeline_v2.py", "scripts/semantic_pipeline_evidence.py",
    "wahojobs/opportunity_enrichment.py", "wahojobs/opportunity_semantic_extraction.py",
    "wahojobs/opportunity_semantic_staging.py", "wahojobs/opportunity_semantic_authority.py",
    "wahojobs/opportunity_semantic_contract.py", "wahojobs/opportunity_fact_authority.py",
)
canonical = core.canonical
fingerprint = core.fingerprint


class PipelineError(ValueError):
    pass


def _exact(value, fields, reason):
    if type(value) is not dict or set(value) != set(fields):
        raise PipelineError(reason)


def _utc():
    return datetime.now(timezone.utc).isoformat()


class _BodyCaptured(Exception):
    def __init__(self, body):
        self.body = deepcopy(body)


class _CaptureSession:
    def post(self, url, **kwargs):
        if url != ENDPOINT:
            raise PipelineError("unexpected_provider_endpoint")
        raise _BodyCaptured(kwargs["json"])


def extraction_body(record):
    """Reuse the historical client's exact builder, intercepting before HTTP.

    This session has no socket or network implementation. No fake provider output
    is created. Generation settings, prompt and strict schema are unchanged.
    """
    source = evidence.provider_evidence(record)
    try:
        extraction.OpenAISemanticExtractionClient(
            "local-request-construction-not-a-credential", model=EXTRACTION_CONFIG["model"],
            session=_CaptureSession()).extract(source)
    except _BodyCaptured as captured:
        body = captured.body
        if {k: body[k] for k in EXTRACTION_CONFIG} != EXTRACTION_CONFIG or "temperature" in body:
            raise PipelineError("extraction_configuration_changed")
        return body
    raise PipelineError("no_accepted_extraction_input")


def compatibility(record, body):
    """Strict input/config identity; a matching title or canonical ID is insufficient."""
    evidence.validate_frozen_evidence(record)
    return {
        "version": CACHE_VERSION, "pipeline_version": VERSION,
        "opportunity_ref": record["opportunity_ref"],
        "frozen_evidence_sha256": record["evidence_sha256"],
        "semantic_input_version": record["authority"]["semantic_input_version"],
        "semantic_input_sha256": record["authority"]["semantic_input_sha256"],
        "source_packet_sha256": record["authority"]["source_packet_sha256"],
        "accepted_bindings_sha256": fingerprint(evidence.accepted_bindings(record)),
        "endpoint": ENDPOINT, "configuration": deepcopy(EXTRACTION_CONFIG),
        "prompt_version": extraction.PROMPT_VERSION, "prompt_sha256": extraction.prompt_sha256(),
        "schema_version": extraction.SCHEMA_VERSION,
        "schema_sha256": fingerprint(body["text"]["format"]["schema"]),
        "request_body_sha256": fingerprint(body), "packet_recipe": evidence.PACKET_RECIPE_VERSION,
        "recipe_file_hashes_lf": {name: hashlib.sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                                 for name in UPSTREAM_RECIPE_FILES},
    }


def request_identity(stage, scope, body):
    if stage not in {"extraction", "assessment"} or not isinstance(scope, str) or not scope:
        raise PipelineError("invalid_request_scope")
    return fingerprint([VERSION, stage, scope, fingerprint(body)])


def _materialize(record, payload, *, origin, receipt=None):
    try:
        bindings = evidence.validate_frozen_evidence(record)
        packet, validation = evidence.build_packet(record, payload, bindings)
        evidence.validate_packet_provenance(packet, record, bindings)
        return {"opportunity_ref": record["opportunity_ref"], "packet": packet,
                "failure": None, "origin": origin, "raw_extraction": deepcopy(payload),
                "provider_receipt": deepcopy(receipt),
                "status": "nonempty" if packet["propositions"] else "empty",
                "validation_version": validation["validator_version"]}
    except (ValueError, KeyError, TypeError) as exc:
        return {"opportunity_ref": record["opportunity_ref"], "packet": None,
                "failure": "extraction_or_packet_validation_failed:" + type(exc).__name__,
                "origin": origin, "raw_extraction": deepcopy(payload),
                "provider_receipt": deepcopy(receipt), "status": "unavailable"}


def _cache_path(directory, key):
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise PipelineError("invalid_cache_key")
    return Path(directory).resolve() / (key + ".json")


def _valid_receipt(receipt, identity):
    return (type(receipt) is dict and receipt.get("provider_called") is True
            and receipt.get("response_status") == "completed"
            and isinstance(receipt.get("response_id"), str) and bool(receipt["response_id"])
            and receipt.get("model_requested") == EXTRACTION_CONFIG["model"]
            and receipt.get("request_body_sha256") == identity["request_body_sha256"]
            and receipt.get("endpoint") == ENDPOINT
            and receipt.get("configuration") == EXTRACTION_CONFIG
            and isinstance(receipt.get("response_model"), str)
            and isinstance(receipt.get("completed_at"), str)
            and type(receipt.get("http_status")) is int and 200 <= receipt["http_status"] < 300)


def _load_cache(directory, key, identity, record):
    if directory is None:
        return None, "miss"
    path = _cache_path(directory, key)
    if not path.exists():
        return None, "miss"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        _exact(entry, {"cache_version", "key", "identity", "raw_extraction", "raw_extraction_sha256",
                       "provider_receipt", "integrity_sha256"}, "invalid_cache_entry")
        if (entry["cache_version"] != CACHE_VERSION or entry["key"] != key or entry["identity"] != identity
                or fingerprint(entry["raw_extraction"]) != entry["raw_extraction_sha256"]
                or fingerprint({k: v for k, v in entry.items() if k != "integrity_sha256"}) != entry["integrity_sha256"]
                or not _valid_receipt(entry["provider_receipt"], identity)):
            return None, "incompatible"
        evidence.reject_metadata(entry)
        result = _materialize(record, entry["raw_extraction"], origin="compatible_cache",
                              receipt=entry["provider_receipt"])
        if result["packet"] is None:
            return None, "invalid"
        result["cache_entry_sha256"] = fingerprint(entry)
        return result, "hit"
    except (OSError, ValueError, KeyError, TypeError):
        return None, "invalid"


def prepare_provisioning(sources, *, cache_directory=None):
    """Deduplicate shared canonical evidence and prepare one request per cache miss."""
    by_ref = {}
    for source in sources:
        _exact(source, {"opportunity_ref", "evidence", "failure"}, "invalid_source_envelope")
        ref = source["opportunity_ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"canonical_opportunity:[1-9][0-9]*", ref):
            raise PipelineError("invalid_source_identity")
        if ref in by_ref and by_ref[ref] != source:
            raise PipelineError("conflicting_shared_opportunity_evidence")
        by_ref[ref] = deepcopy(source)
    items = []
    for ref, source in sorted(by_ref.items()):
        item = {"opportunity_ref": ref, "evidence": source["evidence"], "failure": source["failure"],
                "cache_state": "not_applicable", "cached_result": None, "body": None,
                "compatibility": None, "cache_key": None, "request_id": None}
        if source["evidence"] is not None:
            try:
                if source["evidence"]["opportunity_ref"] != ref:
                    raise PipelineError("source_identity_mismatch")
                body = extraction_body(source["evidence"])
                identity = compatibility(source["evidence"], body)
                key = fingerprint(identity)
                hit, state = _load_cache(cache_directory, key, identity, source["evidence"])
                item.update(compatibility=identity, cache_key=key, cache_state=state, cached_result=hit,
                            body=None if hit else body, failure=None,
                            request_id=None if hit else request_identity("extraction", ref, body))
            except (ValueError, KeyError, TypeError) as exc:
                item["failure"] = "source_preparation_failed:" + type(exc).__name__
        items.append(item)
    return {"version": VERSION, "stage": "provisioning_prepared", "items": items,
            "request_count": sum(item["body"] is not None for item in items)}


def _save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        import os
        os.fsync(handle.fileno())


def finish_provisioning(plan, results, *, cache_directory=None):
    """Validate saved/fresh outputs. Missing results are fallback, never dropped IDs.

    Explicit replay fixtures can be materialized without a receipt. They cannot
    become cache hits: cache admission requires full exact-request provenance.
    """
    if plan["version"] != VERSION or plan["stage"] != "provisioning_prepared":
        raise PipelineError("invalid_provisioning_plan")
    expected = {item["opportunity_ref"] for item in plan["items"]}
    if set(results) - expected:
        raise PipelineError("unknown_extraction_result_identity")
    output = {}
    for item in plan["items"]:
        ref = item["opportunity_ref"]
        if item["cached_result"] is not None:
            output[ref] = deepcopy(item["cached_result"])
            continue
        supplied = results.get(ref, {})
        failure = item["failure"] or supplied.get("failure")
        if item["body"] is None or failure or supplied.get("raw_extraction") is None:
            output[ref] = {"opportunity_ref": ref, "packet": None, "status": "unavailable",
                           "failure": failure or "extraction_not_supplied", "origin": "fallback"}
            continue
        result = _materialize(item["evidence"], supplied["raw_extraction"],
                              origin="provider_or_explicit_replay", receipt=supplied.get("provider_receipt"))
        if (cache_directory is not None and result["packet"] is not None
                and _valid_receipt(result["provider_receipt"], item["compatibility"])):
            entry = {"cache_version": CACHE_VERSION, "key": item["cache_key"],
                     "identity": item["compatibility"], "raw_extraction": result["raw_extraction"],
                     "raw_extraction_sha256": fingerprint(result["raw_extraction"]),
                     "provider_receipt": result["provider_receipt"]}
            entry["integrity_sha256"] = fingerprint(entry)
            try:
                _save_new(_cache_path(cache_directory, item["cache_key"]), entry)
                result["cache_write"] = "created"
            except FileExistsError:
                result["cache_write"] = "existing_entry_preserved"
            except OSError:
                result["cache_write"] = "persistence_failed_packet_retained"
        output[ref] = result
    if set(output) != expected:
        raise PipelineError("provisioning_dropped_opportunity")
    return output


def prepare_assessments(profile_batches, provisioned):
    """No legacy fields accepted here. The unchanged downstream adapter owns fit inputs."""
    prepared = {}
    for batch in profile_batches:
        _exact(batch, {"profile_key", "profile_facts", "candidates"}, "invalid_profile_batch")
        key = batch["profile_key"]
        if not isinstance(key, str) or not key or key in prepared:
            raise PipelineError("invalid_or_duplicate_profile_key")
        if type(batch["profile_facts"]) is not dict or set(batch["profile_facts"]) - set(core.PROFILE_FIELDS):
            raise PipelineError("non_runtime_profile_fields")
        evidence.reject_metadata(batch["profile_facts"])
        candidates = []
        for candidate in batch["candidates"]:
            _exact(candidate, {"opportunity_ref", "title"}, "invalid_assessment_candidate")
            ref = candidate["opportunity_ref"]
            if ref not in provisioned or provisioned[ref]["opportunity_ref"] != ref:
                raise PipelineError("missing_provisioning_identity")
            packet = provisioned[ref]["packet"]
            if packet is not None and packet["opportunity_scope"]["canonical_ref"] != ref:
                raise PipelineError("packet_identity_mismatch")
            candidates.append({**candidate, "packet": packet})
        request = downstream.prepare_request(batch["profile_facts"], candidates)
        body = downstream.provider_body(request) if request["provider_input"] is not None else None
        prepared[key] = {"request": request, "body": body,
                         "request_id": request_identity("assessment", key, body) if body else None}
    return prepared


def integrate_rankings(prepared_assessments, assessment_results, legacy_by_profile):
    """The only stage consuming legacy order/scores; exact frozen ties_only delegation."""
    if set(prepared_assessments) != set(legacy_by_profile) or set(assessment_results) - set(prepared_assessments):
        raise PipelineError("profile_integration_population_mismatch")
    output = {}
    for key, prepared in prepared_assessments.items():
        for item in legacy_by_profile[key]:
            _exact(item, {"opportunity_ref", "legacy_score"}, "invalid_legacy_integration_input")
        result = assessment_results.get(key, {})
        output[key] = downstream.integrate(legacy_by_profile[key], prepared["request"],
                                           result.get("output"), failure=result.get("failure"))
    return output


class _RecordingSession:
    def __init__(self, session, expected_body):
        self.session, self.expected = session, expected_body
        self.attempts, self.raw_response = 0, None

    def post(self, url, **kwargs):
        if url != ENDPOINT or fingerprint(kwargs.get("json")) != fingerprint(self.expected) or self.attempts:
            raise PipelineError("unexpected_or_duplicate_provider_request")
        self.attempts += 1
        # A redirect would be another transmission outside the exact authorization.
        kwargs["allow_redirects"] = False
        response = self.session.post(url, **kwargs)
        try:
            self.raw_response = deepcopy(response.json())
        except ValueError:
            self.raw_response = {"non_json_response": True, "http_status": response.status_code}
        return response


def execute_prepared(stage, scope, prepared, *, authorization, api_key, journal_directory, session=None):
    """One exact authorized request, with a durable pre-dispatch claim. Never retries.

    Authorization is a caller-supplied destination and exact request-ID/body-hash
    allowlist. This function does not create or imply user authorization.
    """
    body = prepared.get("body")
    if body is None:
        return {"failure": "no_provider_request", "attempted": False}
    identity = request_identity(stage, scope, body)
    body_sha = fingerprint(body)
    if (type(authorization) is not dict or authorization.get("endpoint") != ENDPOINT
            or authorization.get("requests", {}).get(identity) != body_sha):
        return {"failure": "authorization_required", "attempted": False}
    if prepared.get("request_id") != identity:
        raise PipelineError("prepared_request_identity_mismatch")
    expected = extraction_body(prepared["evidence"]) if stage == "extraction" else downstream.provider_body(prepared["request"])
    if expected != body:
        raise PipelineError("prepared_body_changed")
    directory = Path(journal_directory).resolve()
    result_path, marker_path = directory / (identity + ".result.json"), directory / (identity + ".attempt.json")
    if result_path.exists():
        try:
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            if (saved["request_id"] == identity and saved["request_body_sha256"] == body_sha
                    and saved["integrity_sha256"] == fingerprint(
                        {k: v for k, v in saved.items() if k != "integrity_sha256"})):
                return saved
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {"failure": "uncertain_prior_result", "attempted": False}
    if marker_path.exists():
        return {"failure": "uncertain_prior_attempt", "attempted": False}
    if not api_key:
        return {"failure": "provider_credentials_unavailable", "attempted": False}
    marker = {"pipeline_version": VERSION, "stage": stage, "scope": scope, "request_id": identity,
              "request_body_sha256": body_sha, "endpoint": ENDPOINT, "started_at": _utc()}
    try:
        _save_new(marker_path, marker)
    except FileExistsError:
        return {"failure": "uncertain_prior_attempt", "attempted": False}
    except OSError:
        return {"failure": "durable_journal_unavailable", "attempted": False}
    import requests
    owned = session is None
    http = session if session is not None else requests.Session()
    recorder = _RecordingSession(http, body)
    result = {**marker, "attempted": True, "failure": None, "raw_extraction": None, "output": None,
              "provider_receipt": None, "raw_response": None}
    try:
        if stage == "extraction":
            response = extraction.OpenAISemanticExtractionClient(
                api_key, model=EXTRACTION_CONFIG["model"], session=recorder).extract(evidence.provider_evidence(prepared["evidence"]))
            if not response.provider_called or response.response_status != "completed":
                raise extraction.SemanticExtractionError("extraction_response_not_completed")
            result["raw_extraction"] = response.payload
            result["usage"] = {k: v for k, v in asdict(response).items() if k != "payload"}
            result["provider_receipt"] = {
                "endpoint": ENDPOINT, "configuration": deepcopy(EXTRACTION_CONFIG),
                "request_body_sha256": body_sha, "model_requested": EXTRACTION_CONFIG["model"],
                "provider_called": response.provider_called, "response_id": response.response_id,
                "response_status": response.response_status, "response_model": response.response_model,
                "http_status": response.http_status, "completed_at": _utc(),
            }
        else:
            observed = downstream.call_provider(prepared["request"], api_key, session=recorder)
            result.update(observed)
    except (ValueError, KeyError, TypeError, requests.RequestException, extraction.SemanticExtractionError) as exc:
        result["failure"] = "provider_failure:" + type(exc).__name__
    finally:
        if owned:
            http.close()
    result["raw_response"] = recorder.raw_response
    result["request_id"], result["request_body_sha256"] = identity, body_sha
    result["completed_at"] = _utc()
    result["integrity_sha256"] = fingerprint(result)
    try:
        _save_new(result_path, result)
    except OSError:
        # The pre-dispatch claim remains: a later run cannot silently resend.
        result["failure"] = "provider_result_persistence_failed"
        result["integrity_sha256"] = fingerprint({k: v for k, v in result.items() if k != "integrity_sha256"})
    return result
