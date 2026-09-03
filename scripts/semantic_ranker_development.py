#!/usr/bin/env python3
"""Development-only frozen benchmark adapter, six-call journal, and replay CLI.

Never opens a database. prepare/execute/replay never load human labels. Evaluation
is a separate command/module. Existing frozen artifacts are checksum-pinned.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import semantic_development_core as method

BASE = ROOT / "exports/prospective_semantic_ranking_benchmark_v1"
PACKETS = ROOT / "exports/prospective_semantic_ranking_benchmark_v1_packet_viability"
OUT = ROOT / "exports/prospective_semantic_ranking_benchmark_v1_human_review/semantic_ranker_development_20260903/candidate_01"
LABELS = ROOT / "exports/prospective_semantic_ranking_benchmark_v1_human_review/analysis_20260903/final_frozen_human_labels.json"
LABEL_SHA = "f283abe2b3431d06f870e0877f6abc0ac9209cdb045b465198ad86ac5a27fdb6"
EXPECTED = {
    BASE / "benchmark_manifest.json": "7183c60f894b3808a54748f73cefb310794c8d07bdbdc718a8b32cb027f124ce",
    BASE / "frozen_legacy_baseline.json": "b6b30e49568169c291849d144d7fa41de5ee958150f3bc90f92cdbc61a14e774",
    PACKETS / "packet_bundle.json": "9e3c1759937a67c3bcd7f1b2bb8c4d86f59888ca5c79a985e7706ff3cbbb860a",
}
METHOD_FILES = (
    "scripts/semantic_development_core.py",
    "wahojobs/matching/semantic_shadow_grounding.py",
    "wahojobs/opportunity_semantic_authority.py",
    "wahojobs/opportunity_semantic_contract.py",
    "wahojobs/opportunity_fact_authority.py",
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_new(path, value):
    """Exclusive creation, never overwrite prior experiments or raw responses."""
    path = Path(path).resolve()
    if not path.is_relative_to(OUT.resolve()):
        raise ValueError("output_outside_development_directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)


def verify_frozen():
    for path, expected in EXPECTED.items():
        if sha(path) != expected:
            raise ValueError("protected_input_hash_mismatch:" + path.name)
    for folder in (BASE, PACKETS):
        for line in (folder / "SHA256SUMS.txt").read_text().splitlines():
            expected, name = line.split("  ", 1)
            if sha(folder / name) != expected:
                raise ValueError("protected_bundle_checksum_mismatch:" + name)


def load_universe():
    verify_frozen()
    manifest, baseline = read(BASE / "benchmark_manifest.json"), read(BASE / "frozen_legacy_baseline.json")
    entries = read(PACKETS / "packet_bundle.json")["packets"]
    packets = {x["opportunity_ref"]: x["packet"] for x in entries}
    if len(packets) != 52 or len(entries) != 52:
        raise ValueError("packet_population_changed")
    profiles = []
    for profile in manifest["profiles"]:
        ref = profile["profile_ref"]
        items = next(x["ordered_selected_items"] for x in baseline["profiles"] if x["profile_ref"] == ref)
        judgments = [x for x in manifest["judgments"] if x["profile_ref"] == ref]
        if len(items) != 10 or {x["judgment_ref"] for x in items} != {x["judgment_ref"] for x in judgments}:
            raise ValueError("judgment_population_changed")
        if method.fingerprint(profile["profile_facts"]) != profile["profile_facts_sha256"]:
            raise ValueError("profile_facts_hash_mismatch")
        candidates, legacy, mapping = [], [], {}
        for item in sorted(items, key=lambda x: x["legacy_rank_selected"]):
            if item["deterministic_eligibility"]["status"] not in {"eligible", "unknown"}:
                raise ValueError("non_survivor_in_frozen_population")
            ordinary_ref = "canonical_opportunity:" + str(item["canonical_opportunity_id"])
            packet = packets[item["opportunity_ref"]]
            if packet["opportunity_scope"]["canonical_ref"] != ordinary_ref:
                raise ValueError("packet_opportunity_identity_mismatch")
            frozen = next(x for x in manifest["opportunities"] if x["opportunity_ref"] == item["opportunity_ref"])
            for key in ("semantic_input_sha256", "source_packet_sha256"):
                if packet["identities"][key] != frozen[key]:
                    raise ValueError("packet_frozen_evidence_identity_mismatch")
            candidates.append({"opportunity_ref": ordinary_ref, "packet": packet,
                               "title": item["display_title"]})
            legacy.append({"opportunity_ref": ordinary_ref, "legacy_score": item["legacy_score"]})
            mapping[ordinary_ref] = {"judgment_ref": item["judgment_ref"],
                "benchmark_opportunity_ref": item["opportunity_ref"],
                "title": item["display_title"], "legacy_rank": item["legacy_rank_selected"],
                "legacy_score": item["legacy_score"]}
        request = method.prepare_request(profile["profile_facts"], candidates)
        profiles.append({"profile_ref": ref, "profile_name": profile["profile_facts"]["display_name"],
                         "request": request, "legacy": legacy, "local_judgment_map": mapping})
    if len(profiles) != 6 or len({m["judgment_ref"] for p in profiles for m in p["local_judgment_map"].values()}) != 60:
        raise ValueError("full_population_changed")
    return profiles


def prepare():
    profiles = load_universe()
    files = []
    for p in profiles:
        code = p["profile_ref"].split(":")[-1]
        path = OUT / "execution_inputs" / (code + ".json")
        save_new(path, p)
        body = method.provider_body(p["request"])
        save_new(OUT / "provider_requests" / (code + ".json"), body)
        files.append({"profile": code, "execution_input_sha256": sha(path),
            "provider_body_sha256": method.fingerprint(body),
            "request_input_sha256": p["request"]["input_sha256"],
            "schema_sha256": method.fingerprint(method.output_schema(p["request"])),
            "provider_opportunities": len(p["request"]["id_map"]),
            "fallback_opportunities": p["request"]["fallback"]})
    experiment = {"phase": "human_labeled_development_only", "created_at": datetime.now(timezone.utc).isoformat(),
        "provider_config_candidates": [{"id": "terra_low", "configuration": method.CONFIG}],
        "provider_call_limit": 6, "automatic_retries": 0,
        "temperature": "omitted; provider default, no sampling-control claim",
        "model_choice": "New development selection, based on successful project structured-extraction convention and bounded cost; not claimed pre-specified ranking configuration.",
        "integration_candidates": ["full", "ties_only"],
        "integration_selection_policy": "Prefer full only if it improves pairwise count, does not increase aggregate top-3/top-5 false positives, does not reduce aggregate Strong/Plausible top-3/top-5, and NDCG@3 does not regress. Otherwise prefer ties_only if it meets the same non-regression checks. If neither qualifies, recommend the simpler ties_only method as technically specified but not a demonstrated quality gain; do not claim production readiness.",
        "tunable_weights": [], "open_ended_model_search": False,
        "method_version": method.VERSION, "prompt_version": method.PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(method.PROMPT.encode()).hexdigest(),
        "configuration_sha256": method.fingerprint(method.CONFIG),
        "method_files": {p: sha(ROOT / p) for p in METHOD_FILES},
        "runner_sha256": sha(Path(__file__)), "requests": files,
        "pricing_estimate_per_million": method.PRICING_ESTIMATE_PER_MILLION,
        "pricing_basis": "Existing repository convention, not independently verified current billing",
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()}
    save_new(OUT / "experiment_manifest.json", experiment)
    print("PREPARED profiles=6 judgments=60 provider_opportunities=57 empty_fallback=3", flush=True)


def verify_method(experiment):
    verify_frozen()
    for path, expected in experiment["method_files"].items():
        if sha(ROOT / path) != expected:
            raise ValueError("method_changed_since_experiment_prepare:" + path)
    if sha(Path(__file__)) != experiment["runner_sha256"]:
        raise ValueError("runner_changed_since_prepare")


def execute():
    experiment = read(OUT / "experiment_manifest.json")
    verify_method(experiment)
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY unavailable")
    for row in experiment["requests"]:
        code = row["profile"]
        path = OUT / "execution_inputs" / (code + ".json")
        if sha(path) != row["execution_input_sha256"]:
            raise ValueError("execution_input_hash_mismatch")
        p = read(path)
        raw_path = OUT / "raw_outputs" / (code + ".json")
        if raw_path.exists():
            old = read(raw_path)
            if old["input_sha256"] != row["request_input_sha256"]:
                raise ValueError("existing_raw_input_mismatch")
            print(code + " already journaled; no call", flush=True)
            continue
        marker = OUT / "attempts" / (code + ".json")
        if marker.exists():
            raise ValueError("uncertain_prior_attempt_no_automatic_retry:" + code)
        if method.fingerprint(method.provider_body(p["request"])) != row["provider_body_sha256"]:
            raise ValueError("provider_body_hash_mismatch")
        save_new(marker, {"started_at": datetime.now(timezone.utc).isoformat(),
                          "input_sha256": row["request_input_sha256"]})
        result = method.call_provider(p["request"], key)
        result.update({"input_sha256": row["request_input_sha256"],
                       "completed_at": datetime.now(timezone.utc).isoformat()})
        save_new(raw_path, result)
        print(code + " success=" + str(result["success"]) + " failure=" + str(result["failure"]), flush=True)


def replay():
    experiment = read(OUT / "experiment_manifest.json")
    verify_method(experiment)
    results = {mode: [] for mode in experiment["integration_candidates"]}
    for row in experiment["requests"]:
        code = row["profile"]
        path = OUT / "execution_inputs" / (code + ".json")
        if sha(path) != row["execution_input_sha256"]:
            raise ValueError("execution_input_hash_mismatch")
        p = read(path)
        raw_path = OUT / "raw_outputs" / (code + ".json")
        raw = read(raw_path)
        if raw["input_sha256"] != row["request_input_sha256"]:
            raise ValueError("raw_response_input_mismatch")
        for mode in results:
            items = method.integrate(p["legacy"], p["request"], raw["output"],
                                     mode=mode, failure=raw["failure"])
            for item in items:
                item.update(p["local_judgment_map"][item["opportunity_ref"]])
            results[mode].append({"profile_ref": p["profile_ref"], "profile_name": p["profile_name"],
                                  "items": items, "raw_output_sha256": sha(raw_path)})
    for mode, profiles in results.items():
        save_new(OUT / "integrated_rankings" / (mode + ".json"),
                 {"phase": "development_not_holdout", "method": mode, "profiles": profiles})
    print("REPLAYED two deterministic integration candidates, each 60/60 judgments", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "replay"])
    args = parser.parse_args()
    {"prepare": prepare, "execute": execute, "replay": replay}[args.action]()
