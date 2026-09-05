"""Small, read-only validity proofs for process-local recommendation reuse.

SQLite's rollback-journal commit counter is read under a shared SQLite lock.
No inventory rows are read. WAL, memory databases, attachments, or an uncertain
file identity deliberately return no proof rather than reusing an old result.
The durable runtime already requires rollback-journal SQLite databases.
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat

from wahojobs.matching.opportunity_trust import freshness_max_age_hours, parse_utc


def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, None if os.name == "nt" else metadata.st_ctime_ns)


def database_commit_token(connection):
    """Return a constant-size token while the caller owns a read transaction."""
    if not connection.in_transaction:
        return None
    # Establish a shared lock before inspecting the on-disk commit counter.
    connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
    databases = connection.execute("PRAGMA database_list").fetchall()
    if len(databases) != 1 or databases[0][1] != "main" or not databases[0][2]:
        return None
    if connection.execute("PRAGMA journal_mode").fetchone()[0] not in {
        "delete", "truncate", "persist",
    }:
        return None
    path = Path(databases[0][2])
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        return None
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        header = stream.read(100)
        after = os.fstat(stream.fileno())
    if not (_identity(before) == _identity(opened) == _identity(after)
            == _identity(path.lstat())):
        return None
    if (len(header) != 100 or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x01\x01"
            or header[24:28] != header[92:96]):
        return None
    return (str(path), _identity(after), header)


def inventory_deadline(rows, evaluated_at, *, recent_cache_hours):
    """Find the next time boundary once, while the inventory is already loaded.

    Include every row, not just visible representatives: expiration can change
    representative selection, section caps, and the relaxation candidate pool.
    """
    deadlines = []
    for row in rows:
        maximum = freshness_max_age_hours(
            row.get("inventory_model", ""), row.get("market_count_policy", "")
        )
        if maximum is None:
            continue
        observed = parse_utc(row.get("latest_successful_source_run_at"))
        if observed is None:
            # Missing temporal evidence is not a proof of indefinitely valid reuse.
            return evaluated_at
        for hours in (maximum, recent_cache_hours):
            boundary = observed + timedelta(hours=hours)
            if boundary >= evaluated_at:
                # The existing policy permits equality; move past it by one tick.
                deadlines.append(boundary + timedelta(microseconds=1))
    return min(deadlines, default=datetime.max.replace(tzinfo=timezone.utc))
