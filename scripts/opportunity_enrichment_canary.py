#!/usr/bin/env python3
"""Read-only Opportunity Enrichment vNext canary over accepted source bodies."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_foundation_report as foundation  # noqa: E402
from wahojobs.config import DB_PATH  # noqa: E402
from wahojobs.opportunity_enrichment import (  # noqa: E402
    DERIVATION_RECIPE_VERSION,
    EXTRACTOR_VERSION,
    FIELD_DEFAULTS,
    SCHEMA_VERSION,
    SEMANTIC_INPUT_VERSION,
    TAXONOMY_VERSION,
    canonical_json,
    extract_deterministic_document,
    has_sufficient_llm_source_content,
    load_semantic_input,
    semantic_input_sha256,
    source_body_text,
    validate_enrichment_document,
)


CANARY_SCHEMA_VERSION = "opportunity_enrichment_canary_v1"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Measure deterministic vNext coverage on a stable, read-only sample of "
            "canonicals with accepted source bodies."
        )
    )
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--company-slug", default="meridial")
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def open_read_only(path: Path):
    resolved = Path(path).resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def select_canary_ids(connection, company_slug: str, limit: int) -> list[int]:
    if limit <= 0 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    rows = connection.execute(
        """
        SELECT co.id
        FROM companies c
        JOIN canonical_opportunities co ON co.company_id = c.id
        JOIN jobs j ON j.canonical_opportunity_id = co.id
        JOIN job_source_contents sc ON sc.job_id = j.id
        JOIN job_source_content_acceptances a ON a.job_id = j.id
        WHERE c.slug = ?
          AND co.is_active = 1
          AND j.semantic_authority_state = 'versioned_accepted'
          AND sc.body IS NOT NULL
          AND trim(sc.body) != ''
        GROUP BY co.id
        ORDER BY co.id
        LIMIT ?
        """,
        (company_slug, limit),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _body_grounded_paths(document: dict) -> set[str]:
    return {
        fact["field_path"]
        for fact in document["variant_facts"]
        if any(
            authority_ref.startswith("source_content:")
            for item in fact["evidence"]
            for authority_ref in item["authority_refs"]
        )
    }


def _canonical_body_grounded_paths(document: dict) -> set[str]:
    return {
        item["field_path"]
        for item in document["field_evidence"]
        if any(
            authority_ref.startswith("source_content:")
            for authority_ref in item["authority_refs"]
        )
    }


def build_canary(connection, company_slug: str, limit: int) -> dict:
    canonical_ids = select_canary_ids(connection, company_slug, limit)
    if len(canonical_ids) != limit:
        raise ValueError(
            f"requested {limit} canonicals but found {len(canonical_ids)} accepted-body canonicals"
        )

    items = []
    field_counts = {
        field_path: {
            "canonical_known": 0,
            "canonical_body_grounded": 0,
            "variant_body_supported": 0,
        }
        for field_path, _priority, _purpose in foundation.READINESS_FIELD_SPECS
    }
    for canonical_id in canonical_ids:
        semantic_input = load_semantic_input(connection, canonical_id)
        document = extract_deterministic_document(semantic_input)
        validate_enrichment_document(document)
        unknown_fields = set(document["unknown_fields"])
        known_fields = sorted(set(FIELD_DEFAULTS) - unknown_fields)
        body_grounded = _body_grounded_paths(document)
        canonical_body_grounded = _canonical_body_grounded_paths(document)
        observation = {
            "canonical_opportunity_id": canonical_id,
            "freshness": "current",
            "source_fact_flags": foundation.semantic_source_flags(semantic_input),
            "known_fields": known_fields,
            "semantic_quality_status": "unreviewed",
        }
        for field_path in field_counts:
            field_counts[field_path]["canonical_known"] += field_path in known_fields
            field_counts[field_path]["canonical_body_grounded"] += (
                field_path in canonical_body_grounded
            )
            field_counts[field_path]["variant_body_supported"] += (
                field_path in body_grounded
            )
        items.append(
            {
                "canonical_opportunity_id": canonical_id,
                "canonical_title": semantic_input["canonical"]["canonical_title"],
                "semantic_input_sha256": semantic_input_sha256(semantic_input),
                "variant_count": len(semantic_input["variants"]),
                "accepted_body_variants": sum(
                    bool(
                        source_body_text(
                            source.get("body"),
                            source.get("body_format"),
                        )
                    )
                    for source in semantic_input["rich_content"]
                ),
                "llm_eligible": has_sufficient_llm_source_content(
                    semantic_input
                ),
                "known_fields": known_fields,
                "canonical_body_grounded_fields": sorted(canonical_body_grounded),
                "variant_body_supported_fields": sorted(body_grounded),
                "minimum_semantic_packet_ready": foundation.semantic_packet_ready(
                    observation
                ),
                "mechanically_complete_semantic_packet": (
                    foundation.mechanically_complete_semantic_packet(observation)
                ),
                "structured_requirement_signal": (
                    foundation.structured_requirement_signal(observation)
                ),
            }
        )

    identity = {
        "company_slug": company_slug,
        "canonical_ids": canonical_ids,
        "semantic_inputs": [item["semantic_input_sha256"] for item in items],
    }
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "canary_fingerprint": sha256(
            canonical_json(identity).encode("utf-8")
        ).hexdigest(),
        "mode": "deterministic_dry_run",
        "llm_calls": 0,
        "company_slug": company_slug,
        "sample_size": len(items),
        "canonical_ids": canonical_ids,
        "contracts": {
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "semantic_input_version": SEMANTIC_INPUT_VERSION,
            "derivation_recipe_version": DERIVATION_RECIPE_VERSION,
            "matching_readiness_report": foundation.REPORT_SCHEMA_VERSION,
        },
        "accepted_body_variants": sum(
            item["accepted_body_variants"] for item in items
        ),
        "llm_eligible": sum(item["llm_eligible"] for item in items),
        "minimum_semantic_packet_ready": sum(
            item["minimum_semantic_packet_ready"] for item in items
        ),
        "mechanically_complete_semantic_packet": sum(
            item["mechanically_complete_semantic_packet"] for item in items
        ),
        "structured_requirement_signal": sum(
            item["structured_requirement_signal"] for item in items
        ),
        "field_coverage": [
            {"field_path": field_path, **field_counts[field_path]}
            for field_path in field_counts
        ],
        "items": items,
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        with open_read_only(args.database) as connection:
            report = build_canary(connection, args.company_slug, args.limit)
    except (sqlite3.Error, ValueError) as exc:
        print(f"Opportunity enrichment canary failed: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
