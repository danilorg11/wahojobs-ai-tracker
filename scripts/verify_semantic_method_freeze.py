#!/usr/bin/env python3
"""Read-only freeze verification and optional saved-v1 replay. Never calls APIs.

The default check needs only repository files, not any benchmark/review artifacts.
The optional replay reads saved requests/responses, never human labels or metrics.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import frozen_semantic_ranking_v1 as frozen
import semantic_development_core as core

MANIFEST_PATH = "docs/semantic_ranking_method_v1.freeze.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def text_digest(data):
    # Git's existing Windows autocrlf setting changes text bytes, not behavior.
    return digest(data.replace(b"\r\n", b"\n"))


def source_digest(data, function):
    source = data.decode("utf-8").replace("\r\n", "\n")
    node = next(n for n in ast.parse(source).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == function)
    return digest(ast.get_source_segment(source, node).encode("utf-8"))


def verify_manifest(*, root=ROOT, git_ref=None, check_environment=True):
    root = Path(root).resolve()

    def read_file(name):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("freeze_path_outside_repository")
        if git_ref is not None:
            return subprocess.check_output(
                ["git", "show", f"{git_ref}:{name}"], cwd=root)
        return path.read_bytes()

    manifest_bytes = read_file(MANIFEST_PATH)
    manifest = json.loads(manifest_bytes)
    for name, entry in manifest["files"].items():
        if text_digest(read_file(name)) != entry["sha256_lf"]:
            raise ValueError("frozen_file_changed:" + name)
        if git_ref is not None and text_digest((root / name).read_bytes()) != entry["sha256_lf"]:
            raise ValueError("working_file_differs_from_committed_freeze:" + name)
    for name, entry in manifest["components"].items():
        if source_digest(read_file(entry["file"]), entry["function"]) != entry["sha256"]:
            raise ValueError("frozen_component_changed:" + name)
    if manifest["frozen_method_version"] != frozen.FROZEN_METHOD_VERSION:
        raise ValueError("frozen_identity_changed")
    if manifest["implementation_version"] != core.VERSION or frozen.INTEGRATION != "ties_only":
        raise ValueError("selected_integration_changed")
    if manifest["provider"]["configuration"] != core.CONFIG:
        raise ValueError("provider_configuration_changed")
    if manifest["provider"]["configuration_sha256"] != core.fingerprint(core.CONFIG):
        raise ValueError("configuration_hash_changed")
    if manifest["prompt"]["sha256"] != digest(core.PROMPT.encode("utf-8")):
        raise ValueError("prompt_changed")
    if manifest["schema"]["version"] != core.SCHEMA_VERSION:
        raise ValueError("schema_version_changed")
    if manifest["schema"]["probe_schema_sha256"] != core.fingerprint(
            core.output_schema(manifest["schema"]["probe_request"])):
        raise ValueError("schema_probe_changed")
    if manifest["class_precedence"] != core.FIT_VALUE:
        raise ValueError("class_precedence_changed")
    if manifest["stable_ids"]["order_seed"] != core.ORDER_SEED:
        raise ValueError("ordering_seed_changed")
    if check_environment:
        env = manifest["environment"]
        if platform.python_version() != env["python"]:
            raise ValueError("python_version_changed")
        if unicodedata.unidata_version != env["unicode_database"]:
            raise ValueError("unicode_version_changed")
        for package, version in env["http_dependency_versions"].items():
            if importlib.metadata.version(package) != version:
                raise ValueError("http_dependency_changed:" + package)
    return {"manifest_sha256": digest(manifest_bytes),
            "manifest_sha256_lf": text_digest(manifest_bytes),
            "files_verified": len(manifest["files"]),
            "components_verified": len(manifest["components"]),
            "git_ref": git_ref, "environment_checked": check_environment}


def replay_v1(candidate_directory, *, manifest=None):
    """Recreate the exact old serialization in memory; no output file is written."""
    if manifest is None:
        manifest = json.loads((ROOT / MANIFEST_PATH).read_text(encoding="utf-8"))
    directory = Path(candidate_directory)
    profiles = []
    judgment_ids = []
    for reference in manifest["development_references"]["replay_inputs"]:
        code = reference["profile"]
        input_bytes = (directory / "execution_inputs" / (code + ".json")).read_bytes()
        raw_bytes = (directory / "raw_outputs" / (code + ".json")).read_bytes()
        if digest(input_bytes) != reference["execution_input_sha256"]:
            raise ValueError("v1_input_changed:" + code)
        if digest(raw_bytes) != reference["raw_output_sha256"]:
            raise ValueError("v1_raw_output_changed:" + code)
        item, raw = json.loads(input_bytes), json.loads(raw_bytes)
        if core.fingerprint(frozen.provider_body(item["request"])) != reference["provider_body_sha256"]:
            raise ValueError("v1_request_body_changed:" + code)
        if raw["input_sha256"] != item["request"]["input_sha256"]:
            raise ValueError("v1_raw_input_binding_changed:" + code)
        results = frozen.integrate(item["legacy"], item["request"],
                                   raw["output"], failure=raw["failure"])
        for result in results:
            result.update(item["local_judgment_map"][result["opportunity_ref"]])
            judgment_ids.append(result["judgment_ref"])
        profiles.append({"profile_ref": item["profile_ref"],
                         "profile_name": item["profile_name"], "items": results,
                         "raw_output_sha256": digest(raw_bytes)})
    if len(judgment_ids) != 60 or len(set(judgment_ids)) != 60:
        raise ValueError("v1_replay_population_changed")
    artifact = {"phase": "development_not_holdout", "method": "ties_only",
                "profiles": profiles}
    serialized = (json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False)
                  + "\n").encode("utf-8")
    actual = digest(serialized)
    if actual != manifest["development_references"]["selected_ranking_sha256"]:
        raise ValueError("v1_replay_ranking_changed:" + actual)
    return {"ranking_sha256": actual, "profiles": len(profiles),
            "judgments": len(judgment_ids), "provider_calls": 0,
            "human_review_artifact_reads": 0, "files_written": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-ref", help="Verify file blobs at this local git revision")
    parser.add_argument("--replay-v1", type=Path,
                        help="Existing candidate_01 directory; read-only, no API call")
    args = parser.parse_args()
    report = {"freeze": verify_manifest(git_ref=args.git_ref)}
    if args.replay_v1:
        report["v1_replay"] = replay_v1(args.replay_v1)
    print(json.dumps(report, indent=2))
