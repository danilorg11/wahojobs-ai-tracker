"""Run the one approved read-only DA/DF observation using staged_observation.

This does not publish to a database, enable a source, or contact other hosts.
The destination must be a new directory; an interrupted batch is never resumed.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wahojobs import daily_source_policy as policy
from wahojobs import evidence_maintenance as maintenance
from wahojobs.crawler import staged_observation as staged
from wahojobs.crawler.local_inventory import request_deadline

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SHA256 = "0f7ba16c8e508a89c49ff03e58f30883a09d30880a9d5b899f0a4a2c53579225"
BATCH_SECONDS = 720
BATCH_HTTP_MAX = 32
SOURCES = (
    ("dataannotation", "https://www.dataannotation.tech", 12, 360),
    ("dataforce", "https://dataforcecommunity.transperfect.com/projects", 20, 360),
)
PARSER_FILES = (
    "wahojobs/crawler/providers/dataannotation.py",
    "wahojobs/crawler/providers/dataforce.py",
    "wahojobs/crawler/companies/dataannotation.py",
    "wahojobs/crawler/companies/dataforce.py",
    "wahojobs/crawler/local_inventory.py",
    "wahojobs/crawler/staged_observation.py",
    "wahojobs/daily_source_policy.py",
)


def utc_stamp():
    return datetime.now(timezone.utc).isoformat()


def write_once(path, document):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()


def preflight():
    manifest = ROOT / "docs/remaining_source_coverage_v1_validation.md"
    if sha256(manifest.read_bytes()).hexdigest() != MANIFEST_SHA256:
        raise ValueError("controlled_validation_manifest_changed")
    if sum(row[2] for row in SOURCES) != BATCH_HTTP_MAX:
        raise ValueError("controlled_validation_aggregate_mismatch")
    if sum(row[3] for row in SOURCES) != BATCH_SECONDS:
        raise ValueError("controlled_validation_time_mismatch")
    if set(policy.READY_SOURCES).intersection(row[0] for row in SOURCES):
        raise ValueError("controlled_validation_source_already_active")
    for name, _, cap, seconds in SOURCES:
        historical_cap = 11 if name == "dataannotation" else 20
        if policy.POLICY[name]["readiness"] != "blocked" or policy.POLICY[name]["http_max"] != historical_cap or seconds != policy.POLICY[name]["seconds_max"]:
            raise ValueError("controlled_validation_source_policy_changed")
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    if status.strip():
        raise ValueError("controlled_validation_requires_clean_commit")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    commit = preflight()
    root = args.output.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=False)
    journal = root / "journal"
    captures = root / "captures"
    journal.mkdir()
    captures.mkdir()
    started = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan = {
        "kind": "remaining_source_coverage_v1_controlled_observation",
        "run_id": run_id,
        "started_at": utc_stamp(),
        "code_commit": commit,
        "manifest_sha256": MANIFEST_SHA256,
        "http_max": BATCH_HTTP_MAX,
        "seconds_max": BATCH_SECONDS,
        "sources": [
            {"name": name, "careers_url": url, "http_max": cap, "seconds_max": seconds}
            for name, url, cap, seconds in SOURCES
        ],
        "parser_sha256": {
            name: sha256((ROOT / name).read_bytes()).hexdigest()
            for name in PARSER_FILES
        },
        "execution_host": "local_codex_workspace",
        "publication": False,
    }
    write_once(root / "batch-plan.json", plan)
    results = []
    used = 0
    for name, url, cap, seconds in SOURCES:
        remaining = BATCH_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            results.append({"source": name, "status": "not_started_batch_deadline"})
            break
        outcome = {"source": name, "started_at": utc_stamp()}
        try:
            with request_deadline(time.monotonic() + min(seconds, remaining)):
                staged.collect(name, url, captures, run_id=run_id, code_commit=commit,
                               http_max=cap, journal_root=journal, controlled_validation=True)
            outcome["status"] = "collected_unpublished"
        except Exception as exc:
            outcome["status"] = "collection_failed"
            outcome["error_type"] = type(exc).__name__
            outcome["error_message"] = str(exc)[:300]
        ref = captures / (name + "-collection.json")
        if ref.exists():
            plan_id = json.loads(ref.read_text())["plan_id"]
            outcome["plan_id"] = plan_id
            report = maintenance.report(journal, plan_id)
            finished = [event for event in report["events"] if event["event"] == "finished"]
            if finished:
                usage = finished[-1]["data"]["request_usage"]
                outcome["request_usage"] = usage
                used += usage["http_transactions"]
        outcome["finished_at"] = utc_stamp()
        results.append(outcome)
        if used > BATCH_HTTP_MAX:
            raise RuntimeError("controlled_validation_aggregate_exceeded")
    receipt = {
        "run_id": run_id,
        "finished_at": utc_stamp(),
        "elapsed_capture_seconds": round(time.monotonic() - started, 3),
        "http_attempts": used,
        "sources": results,
        "no_publication": True,
    }
    write_once(root / "batch-receipt.json", receipt)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
