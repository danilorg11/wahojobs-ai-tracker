"""Beta-host read-only DA/DF capture with one non-resettable task ledger.

Run one source at a time, outside application maintenance. Every invocation
uses the same private ledger directory. A missing receipt blocks further runs
until its attempted requests have been independently reconciled.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wahojobs import evidence_maintenance as maintenance
from wahojobs.crawler import staged_observation as staged
from wahojobs.crawler.local_inventory import request_deadline


ROOT = Path(__file__).resolve().parents[1]
SOURCE_LIMITS = {
    "dataannotation": ("https://www.dataannotation.tech", 32),
    "dataforce": ("https://dataforcecommunity.transperfect.com/projects", 100),
}
AGGREGATE_HTTP_MAX = 132
AGGREGATE_SECONDS_MAX = 900
VERSION = "remaining_source_delivery_ledger_v1"
EXPECTED_HOST = "wahojobs-private-beta-rehearsal-20260917"
LEDGER_ROOT = Path("/var/lib/wahojobs-beta/remaining-source-coverage-v1/task-ledger")
PARSER_FILES = (
    "wahojobs/crawler/providers/dataannotation.py",
    "wahojobs/crawler/providers/dataforce.py",
    "wahojobs/crawler/companies/dataannotation.py",
    "wahojobs/crawler/companies/dataforce.py",
    "wahojobs/daily_source_policy.py",
    "wahojobs/crawler/staged_observation.py",
)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_once(path, document):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()


def usage(directory):
    used = {source: 0 for source in SOURCE_LIMITS}
    seconds = 0.0
    runs = directory / "runs"
    if runs.is_symlink():
        raise ValueError("capture_ledger_runs_symlink_forbidden")
    for run in sorted(runs.iterdir()):
        if run.is_symlink():
            raise ValueError("capture_ledger_run_symlink_forbidden")
        if not run.is_dir():
            continue
        receipt = run / "receipt.json"
        if receipt.is_symlink():
            raise ValueError("capture_ledger_receipt_symlink_forbidden")
        if not receipt.is_file():
            raise ValueError(f"unfinished_capture_requires_request_audit:{run.name}")
        row = json.loads(receipt.read_text(encoding="utf-8"))
        if (row.get("version") != VERSION or row.get("source") not in SOURCE_LIMITS
                or type(row.get("http_attempts")) is not int
                or not 0 <= row["http_attempts"] <= SOURCE_LIMITS[row["source"]][1]
                or type(row.get("elapsed_seconds")) not in (int, float)
                or not math.isfinite(row["elapsed_seconds"])
                or row["elapsed_seconds"] < 0):
            raise ValueError("capture_ledger_receipt_invalid")
        used[row["source"]] += row["http_attempts"]
        seconds += row["elapsed_seconds"]
    if (any(used[source] > SOURCE_LIMITS[source][1] for source in SOURCE_LIMITS)
            or sum(used.values()) > AGGREGATE_HTTP_MAX
            or seconds > AGGREGATE_SECONDS_MAX):
        raise ValueError("capture_ledger_budget_exceeded")
    return used, seconds


@contextmanager
def task_lock(root):
    """Serialize every budget read, reservation, collection and receipt."""
    import fcntl

    lock_path = root / ".task-lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("capture_ledger_lock_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("capture_ledger_already_running") from exc
        yield
    finally:
        os.close(fd)


def verify_release_files(commit):
    """Require the prepared root-owned archive; SHA lineage is operator-verified."""
    if (os.name != 'posix' or os.geteuid() != 0
            or re.fullmatch(r'[a-f0-9]{40}', commit) is None
            or ROOT.resolve(strict=True) != Path('/opt/wahojobs-beta/releases', commit)):
        raise ValueError('capture_requires_pinned_beta_release_archive')
    for path in (ROOT, Path(__file__), *(ROOT / name for name in PARSER_FILES)):
        if path.is_symlink():
            raise ValueError('capture_release_symlink_forbidden')
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('capture_release_must_be_root_owned_read_only')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=tuple(SOURCE_LIMITS), required=True)
    parser.add_argument("--phase", choices=("validation", "commissioning"), required=True)
    parser.add_argument("--code-commit", required=True)
    args = parser.parse_args()
    if socket.gethostname() != EXPECTED_HOST:
        raise ValueError("beta_execution_host_mismatch")
    commit = args.code_commit
    verify_release_files(commit)
    root = LEDGER_ROOT
    if root.is_symlink():
        raise ValueError("capture_ledger_symlink_forbidden")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.resolve(strict=True) != LEDGER_ROOT:
        raise ValueError("capture_ledger_path_changed")
    with task_lock(root):
        return run_locked(root, args, commit)


def run_locked(root, args, commit):
    runs = root / "runs"
    if runs.is_symlink():
        raise ValueError("capture_ledger_runs_symlink_forbidden")
    runs.mkdir(exist_ok=True, mode=0o700)
    anchor = root / "task-ledger.json"
    if anchor.is_symlink():
        raise ValueError("capture_ledger_anchor_symlink_forbidden")
    if anchor.exists():
        prior = json.loads(anchor.read_text(encoding="utf-8"))
        if (prior.get("version") != VERSION or prior.get("limits") != {
                "dataannotation": 32, "dataforce": 100, "aggregate": 132,
                "collection_seconds": 900} or prior.get("execution_host") != EXPECTED_HOST):
            raise ValueError("capture_ledger_contract_changed")
    else:
        write_once(anchor, {"version": VERSION, "created_at": stamp(),
                            "execution_host": EXPECTED_HOST,
                            "limits": {"dataannotation": 32, "dataforce": 100,
                                       "aggregate": 132, "collection_seconds": 900}})
    used, seconds_used = usage(root)
    source_url, source_max = SOURCE_LIMITS[args.source]
    http_remaining = min(source_max - used[args.source], AGGREGATE_HTTP_MAX - sum(used.values()))
    seconds_remaining = AGGREGATE_SECONDS_MAX - seconds_used
    if http_remaining <= 0 or seconds_remaining <= 0:
        raise ValueError("capture_ledger_no_remaining_budget")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = root / "runs" / (run_id + "-" + args.source + "-" + args.phase)
    run.mkdir()
    write_once(run / "plan.json", {"version": VERSION, "source": args.source,
        "phase": args.phase, "started_at": stamp(), "execution_host": socket.gethostname(),
        "code_commit": commit, "http_limit": http_remaining,
        "seconds_limit": seconds_remaining, "aggregate_prior_http": sum(used.values()),
        "aggregate_prior_seconds": seconds_used,
        "parser_sha256": {name: sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in PARSER_FILES}})
    started = time.monotonic()
    status, error = "collected_unpublished", None
    try:
        with request_deadline(started + seconds_remaining):
            staged.collect(args.source, source_url, run / "captures", run_id=run_id,
                           code_commit=commit, http_max=http_remaining,
                           journal_root=run / "journal", controlled_validation=True)
    except Exception as exc:
        status, error = "collection_failed", type(exc).__name__
    reference = json.loads((run / "captures" / (args.source + "-collection.json")).read_text())
    report = maintenance.report(run / "journal", reference["plan_id"])
    finished = report["events"][-1]
    if finished["event"] != "finished":
        raise ValueError("capture_journal_did_not_finish")
    attempted = finished["data"]["request_usage"]["http_transactions"]
    elapsed = time.monotonic() - started
    if attempted > http_remaining or elapsed > seconds_remaining + 1:
        raise ValueError("capture_budget_or_deadline_exceeded")
    receipt = {"version": VERSION, "run_id": run_id, "source": args.source,
               "phase": args.phase, "status": status, "error_type": error,
               "plan_id": reference["plan_id"], "journal_hash": finished["hash"],
               "http_attempts": attempted, "elapsed_seconds": elapsed,
               "finished_at": stamp(), "no_publication": True}
    write_once(run / "receipt.json", receipt)
    print(json.dumps(receipt, indent=2))
    return 0 if status == "collected_unpublished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
