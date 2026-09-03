#!/usr/bin/env python3
"""Read-only complete-freeze checks and explicit offline historical replay.

Default verification needs no benchmark files. --replay-v1 reads only the
allowlisted pre-label evidence and saved machine inputs/outputs, never labels.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import semantic_pipeline_evidence as evidence
import semantic_pipeline_v2 as pipeline
import verify_semantic_method_freeze as historical

MANIFEST_PATH = "docs/semantic_pipeline_method_v2.freeze.json"
RANKING_SHA = "ec012d94659d8abfa74a37aa50b00cffbca998c5fa03a31bca559ef62ec7a1e7"
HISTORICAL_MANIFEST_SHA = "efc38e195e26ad066acc318b6ddd38dab1854d8d3f31f241a3a5806cb41a2b75"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dependency_closure(root, seeds):
    """Conservative local import closure, including function-local imports/init files.

    No imports are executed by this scanner. Pin the complete source of these
    modules so helpers/constants on the acceptance-to-packet path cannot drift.
    """
    root = Path(root)
    found, pending = set(), list(seeds)
    def candidates(module):
        if not module:
            return []
        stem = module.replace(".", "/")
        return [stem + ".py", stem + "/__init__.py", "scripts/" + stem + ".py"]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        path = root / name
        if not path.is_file():
            raise ValueError("missing_dependency:" + name)
        found.add(name)
        for parent in Path(name).parents:
            init = (parent / "__init__.py").as_posix()
            if str(parent) != "." and (root / init).is_file() and init not in found:
                pending.append(init)
        package = name.split("/")[:-1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = ".".join(package[:len(package)-node.level+1] + ([base] if base else []))
                modules = [base] + [base + "." + a.name for a in node.names if a.name != "*"]
            for module in modules:
                for dependency in candidates(module):
                    if (root / dependency).is_file() and dependency not in found:
                        pending.append(dependency)
    return sorted(found)


def verify_manifest(*, root=ROOT, git_ref=None, check_environment=True):
    root = Path(root).resolve()
    def read(name):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("manifest_path_escape")
        return subprocess.check_output(["git", "show", f"{git_ref}:{name}"], cwd=root) if git_ref else path.read_bytes()
    manifest_bytes = read(MANIFEST_PATH)
    m = json.loads(manifest_bytes)
    for name, entry in m["files"].items():
        if historical.text_digest(read(name)) != entry["sha256_lf"]:
            raise ValueError("frozen_file_changed:" + name)
        if git_ref and historical.text_digest((root / name).read_bytes()) != entry["sha256_lf"]:
            raise ValueError("working_file_changed:" + name)
    closure = dependency_closure(root, m["runtime_seeds"])
    if closure != m["runtime_dependency_closure"] or set(closure) - set(m["files"]):
        raise ValueError("runtime_dependency_closure_changed")
    for name, entry in m["components"].items():
        if historical.source_digest(read(entry["file"]), entry["function"]) != entry["sha256"]:
            raise ValueError("component_changed:" + name)
    if digest(read(historical.MANIFEST_PATH)) != HISTORICAL_MANIFEST_SHA:
        raise ValueError("historical_downstream_manifest_changed")
    historical.verify_manifest(root=root, git_ref=git_ref, check_environment=check_environment)
    if m["method_identity"] != pipeline.VERSION or m["selected_integration"] != "ties_only":
        raise ValueError("method_identity_changed")
    upstream, downstream = m["extraction"], m["assessment"]
    if upstream["configuration"] != pipeline.EXTRACTION_CONFIG or downstream["configuration"] != pipeline.core.CONFIG:
        raise ValueError("provider_configuration_changed")
    if upstream["prompt_sha256"] != pipeline.extraction.prompt_sha256():
        raise ValueError("extraction_prompt_changed")
    if upstream["base_schema_sha256"] != pipeline.extraction.schema_sha256():
        raise ValueError("extraction_schema_changed")
    if downstream["prompt_sha256"] != digest(pipeline.core.PROMPT.encode("utf-8")):
        raise ValueError("assessment_prompt_changed")
    if m["class_precedence"] != pipeline.core.FIT_VALUE:
        raise ValueError("class_precedence_changed")
    if m["cache"]["version"] != pipeline.CACHE_VERSION or m["packet"]["recipe_version"] != evidence.PACKET_RECIPE_VERSION:
        raise ValueError("upstream_policy_version_changed")
    if check_environment:
        actual = {"python": platform.python_version(), "unicode_database": unicodedata.unidata_version,
                  "sqlite": sqlite3.sqlite_version}
        if actual != {key: m["environment"][key] for key in actual}:
            raise ValueError("runtime_environment_changed")
        for package, version in m["environment"]["package_versions"].items():
            if importlib.metadata.version(package) != version:
                raise ValueError("runtime_package_changed:" + package)
    return {"manifest_sha256": digest(manifest_bytes), "manifest_sha256_lf": historical.text_digest(manifest_bytes),
            "files_verified": len(m["files"]), "runtime_dependencies_verified": len(closure),
            "components_verified": len(m["components"]), "git_ref": git_ref, "external_calls": 0}


def replay_v1(*, root=ROOT):
    """Complete new orchestration from saved pre-label evidence; no fixture cache admission.

    Benchmark identifiers are used only in this verifier to join immutable replay
    files. Runtime receives canonical evidence/profile facts and separate legacy
    input, exactly as an ordinary caller would supply them.
    """
    root = Path(root)
    freeze = json.loads((root / MANIFEST_PATH).read_text(encoding="utf-8"))
    refs = freeze["offline_replay"]
    def checked(name, sha):
        data = (root / name).read_bytes()
        if digest(data) != sha:
            raise ValueError("replay_fixture_changed:" + name)
        return data
    manifest = json.loads(checked(refs["prelabel_manifest"]["path"], refs["prelabel_manifest"]["sha256"]))
    baseline = json.loads(checked(refs["legacy_baseline"]["path"], refs["legacy_baseline"]["sha256"]))
    provision_dir = root / refs["provision_directory"]
    checksums = checked(refs["provision_checksums"]["path"], refs["provision_checksums"]["sha256"]).decode("utf-8")
    expected = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in checksums.splitlines() if line}
    bundle = json.loads(checked((provision_dir / "packet_bundle.json").relative_to(root).as_posix(),
                               expected["packet_bundle.json"]))
    sources, saved, packets = [], {}, {}
    for opportunity in manifest["opportunities"]:
        evidence_path = Path(refs["prelabel_manifest"]["path"]).parent / opportunity["frozen_evidence_path"]
        frozen = json.loads(checked(evidence_path, opportunity["frozen_evidence_file_sha256"]))
        record = evidence.seal_evidence(frozen["local_authority_identifiers"]["canonical_opportunity_id"],
            frozen["frozen_semantic_input"], frozen["frozen_source_packet"], frozen["authority"]["accepted_capture_bindings"])
        ref = record["opportunity_ref"]
        code = opportunity["opportunity_ref"].rsplit(":", 1)[-1]
        result_path = "results/" + code + ".json"
        result = json.loads(checked((provision_dir / result_path).relative_to(root).as_posix(), expected[result_path]))
        if result["canonical_opportunity_id"] != frozen["local_authority_identifiers"]["canonical_opportunity_id"]:
            raise ValueError("historical_extraction_join_mismatch")
        sources.append({"opportunity_ref": ref, "evidence": record, "failure": None})
        saved[ref] = {"raw_extraction": result["raw_extraction"]}
        packets[ref] = result["packet"]
    plan = pipeline.prepare_provisioning(sources)
    rebuilt = pipeline.finish_provisioning(plan, saved)
    if len(rebuilt) != 52 or len(packets) != 52:
        raise ValueError("packet_population_changed")
    bundle_by_ref = {p["packet"]["opportunity_scope"]["canonical_ref"]: p["packet"] for p in bundle["packets"]}
    for ref, result in rebuilt.items():
        if (result["packet"] != packets[ref] or result["packet"] != bundle_by_ref[ref]
                or pipeline.canonical(result["packet"]) != pipeline.canonical(packets[ref])):
            raise ValueError("packet_replay_changed")
    old = json.loads((root / historical.MANIFEST_PATH).read_text(encoding="utf-8"))
    candidate_dir = root / refs["assessment_directory"]
    batches, legacy, stored_inputs, responses, response_hashes = [], {}, {}, {}, {}
    for reference in old["development_references"]["replay_inputs"]:
        code = reference["profile"]
        item = json.loads(checked((candidate_dir / "execution_inputs" / (code + ".json")).relative_to(root).as_posix(),
                                  reference["execution_input_sha256"]))
        raw_bytes = checked((candidate_dir / "raw_outputs" / (code + ".json")).relative_to(root).as_posix(), reference["raw_output_sha256"])
        raw = json.loads(raw_bytes)
        profile = next(p for p in manifest["profiles"] if p["profile_ref"] == item["profile_ref"])
        if pipeline.fingerprint(profile["profile_facts"]) != profile["profile_facts_sha256"]:
            raise ValueError("profile_facts_changed")
        normal_facts = {k: v for k, v in profile["profile_facts"].items() if k in pipeline.core.PROFILE_FIELDS}
        normal_items = next(p["ordered_selected_items"] for p in baseline["profiles"] if p["profile_ref"] == item["profile_ref"])
        normal_items = sorted(normal_items, key=lambda x: x["legacy_rank_selected"])
        batches.append({"profile_key": code, "profile_facts": normal_facts,
            "candidates": [{"opportunity_ref": "canonical_opportunity:" + str(x["canonical_opportunity_id"]),
                            "title": x["display_title"]} for x in normal_items]})
        legacy[code] = [{"opportunity_ref": "canonical_opportunity:" + str(x["canonical_opportunity_id"]),
                         "legacy_score": x["legacy_score"]} for x in normal_items]
        if legacy[code] != item["legacy"] or raw["input_sha256"] != item["request"]["input_sha256"]:
            raise ValueError("historical_input_binding_changed")
        stored_inputs[code], responses[code], response_hashes[code] = item, raw, digest(raw_bytes)
    prepared = pipeline.prepare_assessments(batches, rebuilt)
    for reference in old["development_references"]["replay_inputs"]:
        code = reference["profile"]
        if prepared[code]["request"] != stored_inputs[code]["request"]:
            raise ValueError("assessment_request_replay_changed")
        if pipeline.fingerprint(prepared[code]["body"]) != reference["provider_body_sha256"]:
            raise ValueError("assessment_body_replay_changed")
        pipeline.downstream.validate_output(responses[code]["output"], prepared[code]["request"])
    rankings = pipeline.integrate_rankings(prepared, responses, legacy)
    profiles, judgments = [], []
    for reference in old["development_references"]["replay_inputs"]:
        code = reference["profile"]
        item, rows = stored_inputs[code], rankings[code]
        for row in rows:
            row.update(item["local_judgment_map"][row["opportunity_ref"]])
            judgments.append(row["judgment_ref"])
        profiles.append({"profile_ref": item["profile_ref"], "profile_name": item["profile_name"],
                         "items": rows, "raw_output_sha256": response_hashes[code]})
    if len(judgments) != 60 or len(set(judgments)) != 60 or any(len(p["items"]) != 10 for p in profiles):
        raise ValueError("ranking_population_changed")
    artifact = {"phase": "development_not_holdout", "method": "ties_only", "profiles": profiles}
    serialized = (json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if digest(serialized) != RANKING_SHA:
        raise ValueError("ranking_replay_changed:" + digest(serialized))
    return {"packets_exact": 52, "nonempty_packets": sum(x["status"] == "nonempty" for x in rebuilt.values()),
            "empty_packets": sum(x["status"] == "empty" for x in rebuilt.values()),
            "assessment_requests_exact": len(prepared), "saved_assessments_validated": len(responses),
            "judgments": len(judgments), "ranking_sha256": digest(serialized),
            "provider_calls": 0, "human_label_reads": 0, "cache_admissions": 0, "files_written": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-ref", help="Verify manifest and file blobs at a local git revision")
    parser.add_argument("--replay-v1", action="store_true", help="Explicitly verify the allowlisted local v1 machine fixtures")
    args = parser.parse_args()
    report = {"freeze": verify_manifest(git_ref=args.git_ref)}
    if args.replay_v1:
        report["offline_v1_replay"] = replay_v1()
    print(json.dumps(report, indent=2))
