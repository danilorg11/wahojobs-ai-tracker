#!/usr/bin/env python3
"""Construct an OE semantic matching packet through the offline shadow seam.

The database is opened read-only and immutable.  This script accepts frozen
semantic extraction/grouping artifacts only; it performs no provider call,
packet persistence, matching, ranking, lifecycle/trust, or candidate UI work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.opportunity_semantic_shadow import (  # noqa: E402
    construct_oe_semantic_matching_packet_v1_shadow,
)


def _read_artifact(path: Path, case_id: str | None) -> tuple[dict, dict | None, int | None]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if type(document) is not dict:
        raise ValueError("artifact must be a JSON object")
    if "cases" in document:
        cases = document["cases"]
        if type(cases) is not dict or not cases:
            raise ValueError("artifact.cases must be a non-empty object")
        if case_id is None:
            if len(cases) != 1:
                raise ValueError("--case-id is required for a multi-case artifact")
            case_id = next(iter(cases))
        case = cases.get(str(case_id))
        if type(case) is not dict:
            raise ValueError(f"artifact has no case {case_id!r}")
        return (
            case.get("raw_extraction"),
            case.get("raw_grouping"),
            case.get("canonical_opportunity_id"),
        )
    return (
        document.get("extraction_payload"),
        document.get("grouping_payload"),
        document.get("canonical_opportunity_id"),
    )


def _read_only_connection(path: Path):
    resolved = path.resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--canonical-id", type=int, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--case-id")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    extraction_payload, grouping_payload, artifact_canonical_id = _read_artifact(
        args.artifact, args.case_id
    )
    if extraction_payload is None:
        raise ValueError("artifact does not contain a semantic extraction payload")
    if (
        artifact_canonical_id is not None
        and int(artifact_canonical_id) != args.canonical_id
    ):
        raise ValueError("artifact canonical identity does not match --canonical-id")
    connection = _read_only_connection(args.database)
    try:
        result = construct_oe_semantic_matching_packet_v1_shadow(
            connection,
            args.canonical_id,
            extraction_payload=extraction_payload,
            grouping_payload=grouping_payload,
        )
    finally:
        connection.close()
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
