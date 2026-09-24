"""Lossless, online housekeeping for private beta request diagnostics.

Only the application log parent is in scope. An archived run retains its old
path through a relative link, while the archive stores a hash manifest.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

RUN_NAME = re.compile(r"run-[0-9a-f]{32}\Z")
KEEP_RECENT = 8
WARN_BYTES = 32 * 3 * 262144
WARN_FREE_BYTES = 128 * 1024 * 1024


def _regular_files(directory):
    result = {}
    for entry in directory.iterdir():
        value = entry.lstat()
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise ValueError("unsafe_diagnostic_run_entry")
        result[entry.name] = sha256(entry.read_bytes()).hexdigest()
    return result


def _open_run_names(parent):
    """Protect any run whose files are still held open by a process."""
    if os.name != "posix":
        return set()
    result = set()
    proc = Path("/proc")
    for task in proc.iterdir():
        if not task.name.isdecimal():
            continue
        fds = task / "fd"
        try:
            children = list(fds.iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for fd in children:
            try:
                target = os.readlink(fd)
                path = Path(target.removesuffix(" (deleted)"))
                if path.parent.parent == parent and RUN_NAME.fullmatch(path.parent.name):
                    result.add(path.parent.name)
            except (FileNotFoundError, PermissionError, OSError):
                continue
    return result


def _validate_parent(parent):
    parent = Path(parent)
    if not parent.is_absolute() or parent.is_symlink() or parent.resolve() != parent or not parent.is_dir():
        raise ValueError("invalid_diagnostic_parent")
    if stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise ValueError("diagnostic_parent_permissions")
    return parent


def archive_preflight(parent, *, active_runs=(), keep_recent=KEEP_RECENT):
    """Archive oldest closed runs without deleting data or breaking old paths.

    An interrupted rename is repaired by restoring the relative link during
    the next preflight. A conflicting path or changed hash fails before work.
    """
    parent = _validate_parent(parent)
    if type(keep_recent) is not int or keep_recent < 1:
        raise ValueError("invalid_diagnostic_retention")
    archive = parent / "archive"
    created = not archive.exists()
    archive.mkdir(mode=0o700, exist_ok=True)
    if created and os.name == "posix":
        owner = parent.stat()
        os.chown(archive, owner.st_uid, owner.st_gid)
    if archive.is_symlink() or archive.resolve() != archive or not archive.is_dir():
        raise ValueError("unsafe_diagnostic_archive")
    if stat.S_IMODE(archive.stat().st_mode) != 0o700:
        raise ValueError("diagnostic_archive_permissions")
    names = set(active_runs) | _open_run_names(parent)
    entries = sorted(parent.glob("run-*"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    for path in entries:
        if not RUN_NAME.fullmatch(path.name):
            raise ValueError("unrecognized_diagnostic_run")
        if path.is_symlink():
            expected = archive / path.name
            if path.resolve(strict=True) != expected or not expected.is_dir():
                raise ValueError("diagnostic_archive_reference_invalid")
        elif not path.is_dir() or path.resolve() != path:
            raise ValueError("unsafe_diagnostic_run")
    # Recover a move interrupted before the reference link was created.
    for target in archive.iterdir():
        if not RUN_NAME.fullmatch(target.name) or target.is_symlink() or not target.is_dir():
            raise ValueError("unsafe_archived_diagnostic_run")
        manifest = target / ".archive-manifest.json"
        if not manifest.is_file() or manifest.is_symlink():
            raise ValueError("diagnostic_archive_manifest_missing")
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
        if recorded != _regular_files_without_manifest(target):
            raise ValueError("diagnostic_archive_hash_mismatch")
        link = parent / target.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(Path("archive") / target.name, target_is_directory=True)
        if not link.is_symlink() or link.resolve(strict=True) != target:
            raise ValueError("diagnostic_archive_reference_invalid")
    live = [path for path in entries if not path.is_symlink()]
    protected = {path.name for path in live[-keep_recent:]} | names
    moved = []
    for path in live:
        if path.name in protected:
            continue
        digest = _regular_files_without_manifest(path)
        manifest = path / ".archive-manifest.json"
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")) != digest:
                raise ValueError("diagnostic_archive_hash_mismatch")
        else:
            with manifest.open("x", encoding="utf-8") as output:
                json.dump(digest, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
        target = archive / path.name
        if target.exists() or target.is_symlink():
            raise ValueError("diagnostic_archive_collision")
        path.rename(target)
        (parent / path.name).symlink_to(Path("archive") / path.name, target_is_directory=True)
        if _regular_files_without_manifest(target) != digest:
            raise ValueError("diagnostic_archive_hash_mismatch")
        moved.append(path.name)
    all_bytes = sum(file.stat().st_size for file in parent.rglob("*") if file.is_file() and not file.is_symlink())
    free = shutil.disk_usage(parent).free
    archived_total = len(list(archive.iterdir()))
    if all_bytes >= WARN_BYTES or free < WARN_FREE_BYTES or archived_total + len(live) - len(moved) >= 32:
        print(f"diagnostic_capacity_warning bytes={all_bytes} free={free} runs={archived_total + len(live) - len(moved)}",
              file=sys.stderr, flush=True)
    return {"archived": tuple(moved), "live": len(live) - len(moved),
            "archived_total": archived_total, "bytes": all_bytes, "free": free}


def _regular_files_without_manifest(path):
    result = _regular_files(path)
    result.pop(".archive-manifest.json", None)
    return result
