#!/usr/bin/env python3
"""Diagnose, repair, and verify semantic-packet provisioning bundle identities.

This is an offline artifact tool. It never performs semantic extraction, calls a
provider, imports the matcher/runtime/UI, or opens the application database.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.opportunity_semantic_authority import (  # noqa: E402
    canonical_sha256,
    semantic_matching_packet_sha256,
    validate_semantic_matching_packet,
)


CORRECTED_MANIFEST_VERSION = (
    "semantic_matching_benchmark_packet_provisioning_identity_corrected_v2"
)
PACKET_IDENTITY_SCHEMA_VERSION = "semantic_packet_artifact_identity_v1"
CORRECTION_VERSION = "semantic_packet_bundle_identity_repair_v1"
EXPECTED_PACKET_IDS = (1064, 1184, 1185, 1188, 1189, 1229, 1230, 1231, 1232, 1233, 1534)
EXPECTED_MISSING_CONTROL_IDS = (2557, 2683, 3213, 3219, 3237, 2017)


class BundleIdentityError(ValueError):
    """A bounded bundle-identity failure."""


def canonical_json_bytes(value: object) -> bytes:
    """Independent implementation of the committed canonical JSON encoding."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def independent_canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def independent_authenticated_packet_sha256(packet: dict) -> str:
    material = copy.deepcopy(packet)
    material.pop("packet_sha256", None)
    return independent_canonical_sha256(material)


def classify_legacy_packet_digest(
    packet: dict,
    artifact_bytes: bytes,
    legacy_digest: str,
) -> str:
    authenticated = independent_authenticated_packet_sha256(packet)
    envelope = independent_canonical_sha256(packet)
    artifact_file = sha256_bytes(artifact_bytes)
    matches = [
        name
        for name, digest in (
            ("authenticated_packet", authenticated),
            ("canonical_packet_envelope_including_authenticator", envelope),
            ("artifact_file_bytes", artifact_file),
        )
        if digest == legacy_digest
    ]
    if len(matches) != 1:
        raise BundleIdentityError("legacy_packet_digest_domain_unclassified")
    return matches[0]


def build_explicit_packet_identity(
    *,
    packet: dict,
    artifact_bytes: bytes,
    source_packet_sha256: str,
    legacy_manifest_packet_sha256: str,
) -> dict:
    authenticated = independent_authenticated_packet_sha256(packet)
    envelope = independent_canonical_sha256(packet)
    artifact_file = sha256_bytes(artifact_bytes)
    classification = classify_legacy_packet_digest(
        packet,
        artifact_bytes,
        legacy_manifest_packet_sha256,
    )
    return {
        "identity_schema_version": PACKET_IDENTITY_SCHEMA_VERSION,
        "authenticated_packet_sha256": authenticated,
        "canonical_packet_envelope_sha256": envelope,
        "artifact_file_sha256": artifact_file,
        "artifact_file_size": len(artifact_bytes),
        "source_packet_sha256": source_packet_sha256,
        "legacy_manifest_packet_sha256": legacy_manifest_packet_sha256,
        "legacy_digest_classification": classification,
        "digest_rules": {
            "authenticated_packet_sha256": (
                "sha256(canonical_json(packet_without_packet_sha256))"
            ),
            "canonical_packet_envelope_sha256": (
                "sha256(canonical_json(packet_including_packet_sha256))"
            ),
            "artifact_file_sha256": "sha256(exact_artifact_file_bytes)",
            "source_packet_sha256": "sha256(canonical_json(source_packet))",
        },
    }


def validate_explicit_packet_identity(
    identity: dict,
    *,
    packet: dict,
    artifact_bytes: bytes,
    source_packet_sha256: str,
) -> None:
    required = {
        "identity_schema_version",
        "authenticated_packet_sha256",
        "canonical_packet_envelope_sha256",
        "artifact_file_sha256",
        "artifact_file_size",
        "source_packet_sha256",
        "legacy_manifest_packet_sha256",
        "legacy_digest_classification",
        "digest_rules",
    }
    if set(identity) != required:
        raise BundleIdentityError("packet_identity_fields_invalid")
    if identity["identity_schema_version"] != PACKET_IDENTITY_SCHEMA_VERSION:
        raise BundleIdentityError("packet_identity_schema_invalid")
    authenticated = independent_authenticated_packet_sha256(packet)
    if identity["authenticated_packet_sha256"] != authenticated:
        raise BundleIdentityError("authenticated_packet_digest_mismatch")
    if packet.get("packet_sha256") != authenticated:
        raise BundleIdentityError("packet_internal_authenticator_mismatch")
    if semantic_matching_packet_sha256(packet) != authenticated:
        raise BundleIdentityError("committed_packet_authenticator_mismatch")
    if identity["canonical_packet_envelope_sha256"] != independent_canonical_sha256(packet):
        raise BundleIdentityError("canonical_packet_envelope_digest_mismatch")
    if identity["artifact_file_sha256"] != sha256_bytes(artifact_bytes):
        raise BundleIdentityError("artifact_file_digest_mismatch")
    if identity["artifact_file_size"] != len(artifact_bytes):
        raise BundleIdentityError("artifact_file_size_mismatch")
    if identity["source_packet_sha256"] != source_packet_sha256:
        raise BundleIdentityError("source_packet_digest_mismatch")
    if identity["legacy_digest_classification"] != classify_legacy_packet_digest(
        packet,
        artifact_bytes,
        identity["legacy_manifest_packet_sha256"],
    ):
        raise BundleIdentityError("legacy_packet_digest_classification_mismatch")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def diagnose_original_bundle(source_dir: Path) -> dict:
    manifest_path = source_dir / "run_manifest.json"
    index_path = source_dir / "artifact_index.json"
    manifest = _read_json(manifest_path)
    index = _read_json(index_path)
    index_by_name = {item["name"]: item for item in index}
    packet_ids = tuple(manifest["authorized_canonical_ids"])
    if packet_ids != EXPECTED_PACKET_IDS:
        raise BundleIdentityError("authorized_packet_membership_changed")
    if tuple(manifest["missing_packet_controls"]) != EXPECTED_MISSING_CONTROL_IDS:
        raise BundleIdentityError("missing_control_membership_changed")

    diagnostics = []
    for canonical_id in packet_ids:
        name = f"canonical_{canonical_id}.json"
        path = source_dir / name
        artifact_bytes = path.read_bytes()
        artifact = json.loads(artifact_bytes)
        packet = artifact["shadow_packet_result"]["packet"]
        validate_semantic_matching_packet(copy.deepcopy(packet))
        internal = packet["packet_sha256"]
        committed_authenticated = semantic_matching_packet_sha256(packet)
        independent_authenticated = independent_authenticated_packet_sha256(packet)
        committed_envelope = canonical_sha256(packet)
        independent_envelope = independent_canonical_sha256(packet)
        source_packet_sha256 = independent_canonical_sha256(artifact["source_packet"])
        legacy_digest = manifest["results"][str(canonical_id)]["packet_sha256"]
        index_entry = index_by_name[name]
        diagnostics.append(
            {
                "canonical_opportunity_id": canonical_id,
                "legacy_manifest_packet_sha256": legacy_digest,
                "legacy_digest_classification": classify_legacy_packet_digest(
                    packet,
                    artifact_bytes,
                    legacy_digest,
                ),
                "authenticated_packet_sha256": internal,
                "canonical_packet_envelope_sha256": committed_envelope,
                "artifact_file_sha256": sha256_bytes(artifact_bytes),
                "source_packet_sha256": source_packet_sha256,
                "packet_validation_passed": True,
                "internal_matches_committed_authenticated": (
                    internal == committed_authenticated
                ),
                "internal_matches_independent_authenticated": (
                    internal == independent_authenticated
                ),
                "committed_matches_independent_envelope": (
                    committed_envelope == independent_envelope
                ),
                "source_packet_identity_links_match": len(
                    {
                        source_packet_sha256,
                        artifact["source_packet_sha256"],
                        manifest["preflight"][str(canonical_id)][
                            "source_packet_sha256"
                        ],
                        manifest["authorized_source_packet_sha256"][
                            str(canonical_id)
                        ],
                    }
                )
                == 1,
                "request_body_identity_link_matches": (
                    artifact["request_body_sha256"]
                    == manifest["preflight"][str(canonical_id)][
                        "request_body_sha256"
                    ]
                ),
                "semantic_input_identity_link_matches": (
                    artifact["semantic_input_sha256"]
                    == manifest["preflight"][str(canonical_id)][
                        "semantic_input_sha256"
                    ]
                ),
                "artifact_index_identity_link_matches": (
                    index_entry["sha256"] == sha256_bytes(artifact_bytes)
                    and index_entry["size"] == len(artifact_bytes)
                ),
            }
        )

    expected_truth = all(
        item["legacy_digest_classification"]
        == "canonical_packet_envelope_including_authenticator"
        and item["packet_validation_passed"]
        and item["internal_matches_committed_authenticated"]
        and item["internal_matches_independent_authenticated"]
        and item["committed_matches_independent_envelope"]
        and item["source_packet_identity_links_match"]
        and item["request_body_identity_link_matches"]
        and item["semantic_input_identity_link_matches"]
        and item["artifact_index_identity_link_matches"]
        for item in diagnostics
    )
    return {
        "diagnostic_version": CORRECTION_VERSION,
        "source_bundle": str(source_dir),
        "source_run_manifest_sha256": sha256_file(manifest_path),
        "source_artifact_index_sha256": sha256_file(index_path),
        "packet_count": len(diagnostics),
        "packets_semantically_intact": expected_truth,
        "root_cause": (
            "manifest_packet_sha256_was_canonical_packet_envelope_digest_"
            "including_internal_authenticator"
        ),
        "diagnostics": diagnostics,
    }


def repair_bundle(source_dir: Path, output_dir: Path) -> dict:
    diagnosis = diagnose_original_bundle(source_dir)
    if not diagnosis["packets_semantically_intact"]:
        raise BundleIdentityError("source_bundle_packets_not_intact")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BundleIdentityError("corrected_bundle_output_not_empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    original_manifest_bytes = (source_dir / "run_manifest.json").read_bytes()
    original_index_bytes = (source_dir / "artifact_index.json").read_bytes()
    (output_dir / "original_run_manifest.json").write_bytes(original_manifest_bytes)
    (output_dir / "original_artifact_index.json").write_bytes(original_index_bytes)
    manifest = json.loads(original_manifest_bytes)
    original_index = json.loads(original_index_bytes)
    original_index_by_name = {item["name"]: item for item in original_index}

    corrected_results = copy.deepcopy(manifest["results"])
    for canonical_id in EXPECTED_PACKET_IDS:
        name = f"canonical_{canonical_id}.json"
        source_artifact = source_dir / name
        target_artifact = output_dir / name
        shutil.copyfile(source_artifact, target_artifact)
        artifact_bytes = target_artifact.read_bytes()
        artifact = json.loads(artifact_bytes)
        packet = artifact["shadow_packet_result"]["packet"]
        old_result = corrected_results[str(canonical_id)]
        legacy_digest = old_result.pop("packet_sha256")
        old_result["packet_identity"] = build_explicit_packet_identity(
            packet=packet,
            artifact_bytes=artifact_bytes,
            source_packet_sha256=artifact["source_packet_sha256"],
            legacy_manifest_packet_sha256=legacy_digest,
        )
        old_result["artifact_file_unchanged_from_original"] = (
            original_index_by_name[name]["sha256"] == sha256_bytes(artifact_bytes)
            and original_index_by_name[name]["size"] == len(artifact_bytes)
        )

    corrected_manifest = copy.deepcopy(manifest)
    corrected_manifest["run_version"] = CORRECTED_MANIFEST_VERSION
    corrected_manifest["artifact_directory"] = str(output_dir)
    corrected_manifest["results"] = corrected_results
    corrected_manifest["identity_correction"] = {
        "correction_version": CORRECTION_VERSION,
        "corrected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_bundle": str(source_dir),
        "source_run_version": manifest["run_version"],
        "source_run_manifest_sha256": sha256_bytes(original_manifest_bytes),
        "source_artifact_index_sha256": sha256_bytes(original_index_bytes),
        "packet_semantic_content_changed": False,
        "packet_artifact_bytes_changed": False,
        "semantic_extraction_regenerated": False,
        "provider_called_for_repair": False,
        "root_cause": diagnosis["root_cause"],
    }
    corrected_manifest["digest_domains"] = {
        "authenticated_packet_sha256": (
            "sha256(canonical_json(packet_without_packet_sha256)); "
            "must equal packet.packet_sha256 and committed validator recomputation"
        ),
        "canonical_packet_envelope_sha256": (
            "sha256(canonical_json(packet_including_packet_sha256)); preserves "
            "the old manifest digest under an explicit non-authenticator name"
        ),
        "artifact_file_sha256": "sha256(exact canonical artifact file bytes)",
        "source_packet_sha256": "sha256(canonical_json(source_packet))",
    }
    corrected_manifest_path = output_dir / "corrected_run_manifest.json"
    _write_json(corrected_manifest_path, corrected_manifest)

    index_entries = []
    for path in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if path.name == "artifact_index.json":
            continue
        index_entries.append(
            {
                "name": path.name,
                "path": str(path),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "digest_domain": "artifact_file_bytes_sha256",
            }
        )
    corrected_index = {
        "index_version": "semantic_packet_corrected_artifact_index_v1",
        "source_bundle": str(source_dir),
        "artifacts": index_entries,
    }
    _write_json(output_dir / "artifact_index.json", corrected_index)
    return verify_corrected_bundle(output_dir)


def verify_corrected_bundle(bundle_dir: Path) -> dict:
    manifest_path = bundle_dir / "corrected_run_manifest.json"
    index_path = bundle_dir / "artifact_index.json"
    manifest = _read_json(manifest_path)
    index_document = _read_json(index_path)
    if manifest["run_version"] != CORRECTED_MANIFEST_VERSION:
        raise BundleIdentityError("corrected_manifest_version_invalid")
    if tuple(manifest["authorized_canonical_ids"]) != EXPECTED_PACKET_IDS:
        raise BundleIdentityError("corrected_packet_membership_changed")
    if tuple(manifest["missing_packet_controls"]) != EXPECTED_MISSING_CONTROL_IDS:
        raise BundleIdentityError("corrected_missing_controls_changed")
    if set(manifest["results"]) != {str(item) for item in EXPECTED_PACKET_IDS}:
        raise BundleIdentityError("corrected_result_membership_changed")
    if any("packet_sha256" in item for item in manifest["results"].values()):
        raise BundleIdentityError("ambiguous_packet_sha256_retained")

    index_entries = index_document["artifacts"]
    index_by_name = {item["name"]: item for item in index_entries}
    for entry in index_entries:
        path = bundle_dir / entry["name"]
        if entry["sha256"] != sha256_file(path) or entry["size"] != path.stat().st_size:
            raise BundleIdentityError("corrected_artifact_index_mismatch")

    packet_results = []
    original_manifest = _read_json(bundle_dir / "original_run_manifest.json")
    original_index = _read_json(bundle_dir / "original_artifact_index.json")
    original_index_by_name = {item["name"]: item for item in original_index}
    for canonical_id in EXPECTED_PACKET_IDS:
        name = f"canonical_{canonical_id}.json"
        path = bundle_dir / name
        artifact_bytes = path.read_bytes()
        artifact = json.loads(artifact_bytes)
        packet = artifact["shadow_packet_result"]["packet"]
        validate_semantic_matching_packet(copy.deepcopy(packet))
        identity = manifest["results"][str(canonical_id)]["packet_identity"]
        source_packet_sha256 = independent_canonical_sha256(artifact["source_packet"])
        validate_explicit_packet_identity(
            identity,
            packet=packet,
            artifact_bytes=artifact_bytes,
            source_packet_sha256=source_packet_sha256,
        )
        if identity["source_packet_sha256"] != artifact["source_packet_sha256"]:
            raise BundleIdentityError("artifact_source_packet_identity_mismatch")
        if identity["source_packet_sha256"] != manifest[
            "authorized_source_packet_sha256"
        ][str(canonical_id)]:
            raise BundleIdentityError("manifest_source_packet_identity_mismatch")
        if identity["legacy_manifest_packet_sha256"] != original_manifest[
            "results"
        ][str(canonical_id)]["packet_sha256"]:
            raise BundleIdentityError("legacy_packet_digest_not_preserved")
        if identity["artifact_file_sha256"] != original_index_by_name[name]["sha256"]:
            raise BundleIdentityError("packet_artifact_bytes_changed")
        if identity["artifact_file_sha256"] != index_by_name[name]["sha256"]:
            raise BundleIdentityError("corrected_index_packet_link_mismatch")
        packet_results.append(
            {
                "canonical_opportunity_id": canonical_id,
                "authenticated_packet_sha256": identity[
                    "authenticated_packet_sha256"
                ],
                "canonical_packet_envelope_sha256": identity[
                    "canonical_packet_envelope_sha256"
                ],
                "artifact_file_sha256": identity["artifact_file_sha256"],
                "source_packet_sha256": identity["source_packet_sha256"],
                "packet_validation_passed": True,
                "artifact_bytes_unchanged": True,
            }
        )

    return {
        "verification_version": CORRECTION_VERSION,
        "bundle": str(bundle_dir),
        "corrected_run_manifest_sha256": sha256_file(manifest_path),
        "artifact_index_sha256": sha256_file(index_path),
        "packet_count": len(packet_results),
        "missing_control_count": len(EXPECTED_MISSING_CONTROL_IDS),
        "all_checks_passed": True,
        "packet_results": packet_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    diagnose = subparsers.add_parser("diagnose")
    diagnose.add_argument("--source", type=Path, required=True)
    repair = subparsers.add_parser("repair")
    repair.add_argument("--source", type=Path, required=True)
    repair.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "diagnose":
        result = diagnose_original_bundle(args.source)
    elif args.command == "repair":
        result = repair_bundle(args.source, args.output)
    else:
        result = verify_corrected_bundle(args.bundle)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
