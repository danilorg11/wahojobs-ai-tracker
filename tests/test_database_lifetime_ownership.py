from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import pickle
import queue
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import wahojobs.database_lifetime_ownership as ownership_module
from wahojobs.database_lifetime_ownership import (
    DatabaseLifetimeOwnership,
    DatabaseLifetimeOwnershipError,
    ROLE_DURABLE_RUNTIME,
    ROLE_OFFLINE_OPERATOR,
    acquire_database_lifetime_ownership,
    database_lifetime_ownership_is_released,
    release_database_lifetime_ownership,
    require_database_lifetime_ownership,
)


ROOT = Path(__file__).resolve().parents[1]
ROLES = (ROLE_DURABLE_RUNTIME, ROLE_OFFLINE_OPERATOR)


class _ArbitraryAcquisitionAbort(BaseException):
    pass


def _create_database(directory, name="owned.sqlite3", *, marker="one"):
    path = Path(directory) / name
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("CREATE TABLE records (position INTEGER, value TEXT)")
        connection.executemany(
            "INSERT INTO records VALUES (?, ?)",
            ((2, marker + "-two"), (1, marker + "-one")),
        )
        connection.commit()
    finally:
        connection.close()
    return path.resolve(strict=True)


def _database_projection(path):
    payload = path.read_bytes()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        schema = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
            )
        )
        rows = tuple(connection.execute("SELECT * FROM records ORDER BY position"))
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    return hashlib.sha256(payload).hexdigest(), schema, rows, journal


def _child_environment():
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    inherited_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT)
        if not inherited_pythonpath
        else str(ROOT) + os.pathsep + inherited_pythonpath
    )
    return environment


_ATTEMPT_SCRIPT = r"""
import json
import sys
from wahojobs.database_lifetime_ownership import (
    DatabaseLifetimeOwnershipError,
    acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
)
path, role = sys.argv[1:3]
try:
    owner = acquire_database_lifetime_ownership(path, role=role)
except DatabaseLifetimeOwnershipError as exc:
    print(json.dumps({"result": exc.category}, sort_keys=True))
    raise SystemExit(0)
print(json.dumps({"result": "owned"}, sort_keys=True))
release_database_lifetime_ownership(owner, role=role, database_path=path)
"""


_HOLD_SCRIPT = r"""
import os
import sys
from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership
path, role = sys.argv[1:3]
owner = acquire_database_lifetime_ownership(path, role=role)
print("owned", flush=True)
command = sys.stdin.readline().strip()
if command == "abnormal":
    os._exit(17)
raise SystemExit(0)
"""


_FIELD_COPIED_OWNER_SCRIPT = r"""
import json
import os
from pathlib import Path
import sys

import wahojobs.database_lifetime_ownership as module
from wahojobs.database_lifetime_ownership import (
    DatabaseLifetimeOwnership,
    DatabaseLifetimeOwnershipError,
    ROLE_DURABLE_RUNTIME,
    acquire_database_lifetime_ownership,
    database_lifetime_ownership_is_released,
    release_database_lifetime_ownership,
    require_database_lifetime_ownership,
)

path = Path(sys.argv[1]).resolve(strict=True)
authentic = acquire_database_lifetime_ownership(
    path,
    role=ROLE_DURABLE_RUNTIME,
)
forged = object.__new__(DatabaseLifetimeOwnership)
slot_names = tuple(
    f"_{DatabaseLifetimeOwnership.__name__}{slot}"
    for slot in DatabaseLifetimeOwnership.__slots__
)
for slot_name in slot_names:
    object.__setattr__(
        forged,
        slot_name,
        object.__getattribute__(authentic, slot_name),
    )

key = object.__getattribute__(
    authentic,
    "_DatabaseLifetimeOwnership__key",
)
with module._REGISTRY_LOCK:
    record = module._OWNERS[key]
    record_before = tuple(
        (name, object.__getattribute__(record, name))
        for name in type(record).__slots__
    )
coordination = module._coordination_path_for_database(path)
coordination_before = os.lstat(coordination)

try:
    release_database_lifetime_ownership(
        forged,
        role=ROLE_DURABLE_RUNTIME,
        database_path=path,
    )
    forged_result = "returned"
except DatabaseLifetimeOwnershipError as exc:
    forged_result = exc.category

with module._REGISTRY_LOCK:
    record_unchanged = module._OWNERS.get(key) is record and all(
        object.__getattribute__(record, name) is value
        for name, value in record_before
    )
coordination_after = os.lstat(coordination)
coordination_unchanged = (
    (
        coordination_before.st_dev,
        coordination_before.st_ino,
        coordination_before.st_mode,
        coordination_before.st_nlink,
        coordination_before.st_size,
        coordination_before.st_mtime_ns,
        coordination_before.st_ctime_ns,
    )
    == (
        coordination_after.st_dev,
        coordination_after.st_ino,
        coordination_after.st_mode,
        coordination_after.st_nlink,
        coordination_after.st_size,
        coordination_after.st_mtime_ns,
        coordination_after.st_ctime_ns,
    )
    and coordination_after.st_size == 0
)
try:
    authentic_valid = require_database_lifetime_ownership(
        authentic,
        role=ROLE_DURABLE_RUNTIME,
        database_path=path,
    )
except DatabaseLifetimeOwnershipError as exc:
    authentic_valid = exc.category

print(
    json.dumps(
        {
            "authentic_released": database_lifetime_ownership_is_released(
                authentic
            ),
            "authentic_valid": authentic_valid,
            "coordination_unchanged": coordination_unchanged,
            "exact_type": type(forged) is DatabaseLifetimeOwnership,
            "forged_release": forged_result,
            "record_unchanged": record_unchanged,
            "slots_copied": len(slot_names),
        },
        sort_keys=True,
    ),
    flush=True,
)

command = sys.stdin.readline().strip()
try:
    release_database_lifetime_ownership(
        authentic,
        role=ROLE_DURABLE_RUNTIME,
        database_path=path,
    )
    authentic_release = "returned"
except DatabaseLifetimeOwnershipError as exc:
    authentic_release = exc.category
print(
    json.dumps(
        {
            "authentic_release": authentic_release,
            "authentic_released": database_lifetime_ownership_is_released(
                authentic
            ),
            "command": command,
        },
        sort_keys=True,
    ),
    flush=True,
)
"""


_ARBITRARY_BASE_EXCEPTION_PROBE_SCRIPT = r"""
import json
from pathlib import Path
import sys

import wahojobs.database_lifetime_ownership as module
from wahojobs.database_lifetime_ownership import (
    ROLE_DURABLE_RUNTIME,
    acquire_database_lifetime_ownership,
)


class ProbeAbort(BaseException):
    pass


path = Path(sys.argv[1]).resolve(strict=True)
interruption = ProbeAbort("native_acquired", 73)
record = None
descriptor = None
returned = False
exception_result = None


def interrupt(name):
    global record, descriptor
    if name == "native_acquired":
        with module._REGISTRY_LOCK:
            record = next(iter(module._OWNERS.values()))
            descriptor = record.descriptor
        raise interruption


try:
    acquire_database_lifetime_ownership(
        path,
        role=ROLE_DURABLE_RUNTIME,
        _checkpoint=interrupt,
    )
    returned = True
except ProbeAbort as exc:
    exception_result = {
        "args": list(exc.args),
        "direct_base": ProbeAbort.__bases__ == (BaseException,),
        "exact_object": exc is interruption,
        "is_exception": isinstance(exc, Exception),
        "type": type(exc).__name__,
    }
except BaseException as exc:
    exception_result = {
        "args": list(exc.args),
        "direct_base": ProbeAbort.__bases__ == (BaseException,),
        "exact_object": exc is interruption,
        "is_exception": isinstance(exc, Exception),
        "type": type(exc).__name__,
    }

with module._REGISTRY_LOCK:
    record_registered = (
        record is not None and module._OWNERS.get(record.key) is record
    )
coordination = module._coordination_path_for_database(path)
try:
    coordination_empty = coordination.read_bytes() == b""
except OSError as exc:
    coordination_empty = f"unreadable:{type(exc).__name__}"
sidecars = tuple(
    str(path) + suffix
    for suffix in ("-journal", "-shm", "-wal")
    if Path(str(path) + suffix).exists()
)
print(
    json.dumps(
        {
            "coordination_empty": coordination_empty,
            "coordination_exists": coordination.exists(),
            "descriptor_closed": (
                descriptor is None or descriptor.closed is True
            ),
            "descriptor_is_none": (
                record is None or record.descriptor is None
            ),
            "exception": exception_result,
            "lease_is_none": record is None or record.lease is None,
            "native_locked": (
                None if record is None else record.native_locked
            ),
            "record_registered": record_registered,
            "record_state": None if record is None else record.state,
            "returned": returned,
            "same_process_acquisition_attempts": 1,
            "sidecars": sidecars,
            "token_is_none": record is None or record.lease_token is None,
        },
        sort_keys=True,
    ),
    flush=True,
)
command = sys.stdin.readline().strip()
print(json.dumps({"command": command}, sort_keys=True), flush=True)
"""


def _child_attempt(path, role):
    completed = subprocess.run(
        [sys.executable, "-B", "-c", _ATTEMPT_SCRIPT, str(path), role],
        cwd=ROOT,
        env=_child_environment(),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0 or completed.stderr:
        raise AssertionError(
            f"child attempt failed: code={completed.returncode}, stderr={completed.stderr!r}"
        )
    return json.loads(completed.stdout)["result"]


def _unused_loopback_port():
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]
    finally:
        candidate.close()


class DatabaseLifetimeOwnershipTests(unittest.TestCase):
    def test_module_import_is_dormant(self):
        script = r"""
import json, pathlib, sys, tempfile, threading
with tempfile.TemporaryDirectory() as directory:
    before = tuple(pathlib.Path(directory).iterdir())
    threads = tuple(thread.ident for thread in threading.enumerate())
    import wahojobs.database_lifetime_ownership as module
    after = tuple(pathlib.Path(directory).iterdir())
    print(json.dumps({
        "files": before == after == (),
        "threads": threads == tuple(thread.ident for thread in threading.enumerate()),
        "owners": not module._OWNERS,
        "epoch": module._PROCESS_EPOCH is None,
        "fork_hook": module._AT_FORK_REGISTERED is False,
        "sqlite": "sqlite3" not in sys.modules,
        "socket": "socket" not in sys.modules,
        "ssl": "ssl" not in sys.modules,
    }, sort_keys=True))
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            env=_child_environment(),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        self.assertFalse(completed.stderr)
        self.assertEqual(set(json.loads(completed.stdout).values()), {True})

    def test_invalid_role_and_database_identity_fail_before_coordination(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            coordination = ownership_module._coordination_path_for_database(database)
            for role in (
                None,
                "runtime",
                "operator",
                "",
                1,
                [],
                {},
                set(),
            ):
                with self.subTest(role=role):
                    with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                        acquire_database_lifetime_ownership(database, role=role)
                    self.assertEqual(caught.exception.category, "invalid_request")
                    self.assertFalse(coordination.exists())

            invalid = Path(directory) / "not-a-database"
            invalid.write_bytes(b"not sqlite")
            with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                acquire_database_lifetime_ownership(
                    invalid.resolve(strict=True), role=ROLE_DURABLE_RUNTIME
                )
            self.assertEqual(caught.exception.category, "invalid_request")
            self.assertFalse(
                ownership_module._coordination_path_for_database(invalid).exists()
            )

            with self.assertRaises(DatabaseLifetimeOwnershipError):
                acquire_database_lifetime_ownership(
                    database.name, role=ROLE_DURABLE_RUNTIME
                )
            self.assertFalse(coordination.exists())

    def test_identity_capture_close_control_propagates_before_coordination(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            coordination = ownership_module._coordination_path_for_database(
                database
            )
            original_file_io = ownership_module.io.FileIO

            class InterruptingIdentityDescriptor:
                def __init__(self, descriptor, *, mode, closefd):
                    self._descriptor = original_file_io(
                        descriptor,
                        mode=mode,
                        closefd=closefd,
                    )

                @property
                def closed(self):
                    return self._descriptor.closed

                def fileno(self):
                    return self._descriptor.fileno()

                def read(self, size):
                    return self._descriptor.read(size)

                def close(self):
                    self._descriptor.close()
                    raise KeyboardInterrupt("identity_close_complete")

            with mock.patch.object(
                ownership_module.io,
                "FileIO",
                InterruptingIdentityDescriptor,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    acquire_database_lifetime_ownership(
                        database,
                        role=ROLE_DURABLE_RUNTIME,
                    )
            self.assertFalse(coordination.exists())

    def test_acquire_release_is_authentic_idempotent_and_nonmutating(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            before = _database_projection(database)
            ownership = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            self.assertTrue(
                require_database_lifetime_ownership(
                    ownership,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            )
            self.assertFalse(database_lifetime_ownership_is_released(ownership))
            self.assertTrue(
                release_database_lifetime_ownership(
                    ownership,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            )
            self.assertTrue(
                release_database_lifetime_ownership(
                    ownership,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            )
            self.assertTrue(database_lifetime_ownership_is_released(ownership))
            self.assertEqual(_database_projection(database), before)
            coordination = ownership_module._coordination_path_for_database(database)
            self.assertTrue(coordination.is_file())
            self.assertEqual(coordination.read_bytes(), b"")
            self.assertFalse(Path(str(database) + "-journal").exists())
            self.assertFalse(Path(str(database) + "-wal").exists())
            self.assertFalse(Path(str(database) + "-shm").exists())

    def test_legitimate_sqlite_writes_preserve_file_identity_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            owner = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO records VALUES (?, ?)",
                    (3, "three"),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertTrue(
                require_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            )
            release_database_lifetime_ownership(
                owner,
                role=ROLE_DURABLE_RUNTIME,
                database_path=database,
            )
            replacement = acquire_database_lifetime_ownership(
                database, role=ROLE_OFFLINE_OPERATOR
            )
            release_database_lifetime_ownership(
                replacement,
                role=ROLE_OFFLINE_OPERATOR,
                database_path=database,
            )
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM records WHERE position = 3"
                    ).fetchone(),
                    ("three",),
                )
            finally:
                connection.close()

    def test_same_process_role_matrix_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            for first_role in ROLES:
                for second_role in ROLES:
                    with self.subTest(first=first_role, second=second_role):
                        first = acquire_database_lifetime_ownership(
                            database, role=first_role
                        )
                        try:
                            with self.assertRaises(
                                DatabaseLifetimeOwnershipError
                            ) as caught:
                                acquire_database_lifetime_ownership(
                                    database, role=second_role
                                )
                            self.assertEqual(caught.exception.category, "contention")
                        finally:
                            release_database_lifetime_ownership(
                                first,
                                role=first_role,
                                database_path=database,
                            )

    def test_concurrent_same_process_acquisition_has_one_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            start = threading.Barrier(8)
            release = threading.Event()
            outcomes = []
            outcomes_lock = threading.Lock()

            def attempt(index):
                start.wait()
                try:
                    owner = acquire_database_lifetime_ownership(
                        database,
                        role=(ROLES[index % 2]),
                    )
                except DatabaseLifetimeOwnershipError as exc:
                    with outcomes_lock:
                        outcomes.append(exc.category)
                    return
                with outcomes_lock:
                    outcomes.append("owned")
                release.wait(5)
                release_database_lifetime_ownership(
                    owner,
                    role=ROLES[index % 2],
                    database_path=database,
                )

            threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 5
            while len(outcomes) < 8 and time.monotonic() < deadline:
                time.sleep(0.01)
            release.set()
            for thread in threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(outcomes.count("owned"), 1)
            self.assertEqual(outcomes.count("contention"), 7)

    def test_cross_process_role_matrix_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            for first_role in ROLES:
                for second_role in ROLES:
                    with self.subTest(first=first_role, second=second_role):
                        first = acquire_database_lifetime_ownership(
                            database, role=first_role
                        )
                        try:
                            self.assertEqual(
                                _child_attempt(database, second_role),
                                "contention",
                            )
                        finally:
                            release_database_lifetime_ownership(
                                first,
                                role=first_role,
                                database_path=database,
                            )

    def test_different_databases_are_independently_ownable(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path = _create_database(directory, "first.sqlite3")
            second_path = _create_database(directory, "second.sqlite3")
            first = acquire_database_lifetime_ownership(
                first_path, role=ROLE_DURABLE_RUNTIME
            )
            second = acquire_database_lifetime_ownership(
                second_path, role=ROLE_OFFLINE_OPERATOR
            )
            try:
                self.assertTrue(
                    require_database_lifetime_ownership(
                        first,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=first_path,
                    )
                )
                self.assertTrue(
                    require_database_lifetime_ownership(
                        second,
                        role=ROLE_OFFLINE_OPERATOR,
                        database_path=second_path,
                    )
                )
            finally:
                release_database_lifetime_ownership(
                    second,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=second_path,
                )
                release_database_lifetime_ownership(
                    first,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=first_path,
                )

    def test_abnormal_process_death_recovers_without_file_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    _HOLD_SCRIPT,
                    str(database),
                    ROLE_DURABLE_RUNTIME,
                ],
                cwd=ROOT,
                env=_child_environment(),
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "owned")
                self.assertEqual(
                    _child_attempt(database, ROLE_OFFLINE_OPERATOR),
                    "contention",
                )
                process.stdin.write("abnormal\n")
                process.stdin.flush()
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 17)
                self.assertFalse(process.stderr.read())
                later = acquire_database_lifetime_ownership(
                    database, role=ROLE_OFFLINE_OPERATOR
                )
                release_database_lifetime_ownership(
                    later,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=database,
                )
                coordination = ownership_module._coordination_path_for_database(
                    database
                )
                self.assertTrue(coordination.exists())
                self.assertEqual(coordination.read_bytes(), b"")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_stale_unlocked_coordination_file_is_harmless(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            first = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            release_database_lifetime_ownership(
                first,
                role=ROLE_DURABLE_RUNTIME,
                database_path=database,
            )
            coordination = ownership_module._coordination_path_for_database(database)
            before_identity = os.stat(coordination)
            second = acquire_database_lifetime_ownership(
                database, role=ROLE_OFFLINE_OPERATOR
            )
            release_database_lifetime_ownership(
                second,
                role=ROLE_OFFLINE_OPERATOR,
                database_path=database,
            )
            after_identity = os.stat(coordination)
            self.assertEqual(
                (before_identity.st_dev, before_identity.st_ino),
                (after_identity.st_dev, after_identity.st_ino),
            )
            self.assertEqual(coordination.read_bytes(), b"")

    def test_linked_database_and_unsafe_coordination_links_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            database_link = Path(directory) / "database-hard-link.sqlite3"
            os.link(database, database_link)
            with self.assertRaises(DatabaseLifetimeOwnershipError):
                acquire_database_lifetime_ownership(
                    database, role=ROLE_DURABLE_RUNTIME
                )
            database_link.unlink()

            coordination = ownership_module._coordination_path_for_database(database)
            unrelated = Path(directory) / "unrelated-empty"
            unrelated.touch()
            os.link(unrelated, coordination)
            with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                acquire_database_lifetime_ownership(
                    database, role=ROLE_DURABLE_RUNTIME
                )
            self.assertEqual(caught.exception.category, "invalid_request")
            self.assertEqual(unrelated.read_bytes(), b"")

    def test_symlink_database_and_coordination_fail_closed_when_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            database_link = Path(directory) / "database-link.sqlite3"
            try:
                os.symlink(database, database_link)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable on this host: {type(exc).__name__}")
            with self.assertRaises(DatabaseLifetimeOwnershipError):
                acquire_database_lifetime_ownership(
                    database_link, role=ROLE_DURABLE_RUNTIME
                )
            database_link.unlink()

            coordination = ownership_module._coordination_path_for_database(database)
            empty = Path(directory) / "empty-target"
            empty.touch()
            os.symlink(empty, coordination)
            with self.assertRaises(DatabaseLifetimeOwnershipError):
                acquire_database_lifetime_ownership(
                    database, role=ROLE_DURABLE_RUNTIME
                )

    def test_database_replacement_during_acquisition_fails_and_does_not_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory, marker="original")
            replacement = _create_database(
                directory, "replacement.sqlite3", marker="replacement"
            )

            def replace_at_native_lock(name):
                if name == "native_acquired":
                    os.replace(replacement, database)

            with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                acquire_database_lifetime_ownership(
                    database,
                    role=ROLE_DURABLE_RUNTIME,
                    _checkpoint=replace_at_native_lock,
                )
            self.assertIn(caught.exception.category, {"ownership_lost", "unavailable"})
            later = acquire_database_lifetime_ownership(
                database, role=ROLE_OFFLINE_OPERATOR
            )
            release_database_lifetime_ownership(
                later,
                role=ROLE_OFFLINE_OPERATOR,
                database_path=database,
            )

    def test_detected_database_replacement_irreversibly_latches_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory, marker="original")
            saved_original = Path(directory) / "saved-original.sqlite3"
            intruder = _create_database(
                directory,
                "intruder.sqlite3",
                marker="intruder",
            )
            owner = acquire_database_lifetime_ownership(
                database,
                role=ROLE_DURABLE_RUNTIME,
            )
            os.replace(database, saved_original)
            shutil.copy2(intruder, database)
            with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                require_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            self.assertEqual(caught.exception.category, "ownership_lost")
            database.unlink()
            os.replace(saved_original, database)
            with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                require_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            self.assertEqual(caught.exception.category, "ownership_lost")
            self.assertEqual(
                _child_attempt(database, ROLE_OFFLINE_OPERATOR),
                "contention",
            )
            release_database_lifetime_ownership(
                owner,
                role=ROLE_DURABLE_RUNTIME,
                database_path=database,
            )
            self.assertEqual(
                _child_attempt(database, ROLE_OFFLINE_OPERATOR),
                "owned",
            )

    @unittest.skipIf(os.name == "nt", "Windows denies replacement of the open locked file")
    def test_coordination_replacement_invalidates_owner_without_unlocking_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            owner = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            coordination = ownership_module._coordination_path_for_database(database)
            displaced = coordination.with_suffix(coordination.suffix + ".old")
            coordination.rename(displaced)
            coordination.touch(mode=0o600)
            with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                require_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            self.assertEqual(caught.exception.category, "ownership_lost")
            release_database_lifetime_ownership(
                owner,
                role=ROLE_DURABLE_RUNTIME,
                database_path=database,
            )
            replacement_owner = acquire_database_lifetime_ownership(
                database, role=ROLE_OFFLINE_OPERATOR
            )
            release_database_lifetime_ownership(
                replacement_owner,
                role=ROLE_OFFLINE_OPERATOR,
                database_path=database,
            )

    def test_capability_is_sealed_and_wrong_authorities_cannot_release(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory, "first.sqlite3")
            other_database = _create_database(directory, "second.sqlite3")
            owner = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            try:
                with self.assertRaises(TypeError):
                    copy.copy(owner)
                with self.assertRaises(TypeError):
                    copy.deepcopy(owner)
                with self.assertRaises(TypeError):
                    pickle.dumps(owner)
                with self.assertRaises(AttributeError):
                    owner.anything = True
                forged = object.__new__(DatabaseLifetimeOwnership)
                for candidate, role, path in (
                    (forged, ROLE_DURABLE_RUNTIME, database),
                    (owner, ROLE_OFFLINE_OPERATOR, database),
                    (owner, ROLE_DURABLE_RUNTIME, other_database),
                ):
                    with self.subTest(candidate=type(candidate).__name__, role=role):
                        with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                            release_database_lifetime_ownership(
                                candidate,
                                role=role,
                                database_path=path,
                            )
                        self.assertEqual(
                            caught.exception.category, "invalid_capability"
                        )
                self.assertTrue(
                    require_database_lifetime_ownership(
                        owner,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=database,
                    )
                )
            finally:
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )

    def test_field_copied_capability_cannot_release_native_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-B",
                    "-c",
                    _FIELD_COPIED_OWNER_SCRIPT,
                    str(database),
                ],
                cwd=ROOT,
                env=_child_environment(),
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_queue = queue.Queue()
            stderr_lines = []

            def collect_stdout():
                for line in process.stdout:
                    stdout_queue.put(line)

            def collect_stderr():
                stderr_lines.extend(process.stderr)

            stdout_reader = threading.Thread(
                target=collect_stdout,
                name="pb-own-copied-capability-stdout",
                daemon=False,
            )
            stderr_reader = threading.Thread(
                target=collect_stderr,
                name="pb-own-copied-capability-stderr",
                daemon=False,
            )
            stdout_reader.start()
            stderr_reader.start()
            initial = None
            release = None
            contender_while_held = None
            contender_after_release = None
            try:
                try:
                    initial = json.loads(stdout_queue.get(timeout=10))
                except queue.Empty:
                    self.fail("copied-capability owner did not publish state")
                contender_while_held = _child_attempt(
                    database,
                    ROLE_OFFLINE_OPERATOR,
                )
                process.stdin.write("release\n")
                process.stdin.flush()
                process.stdin.close()
                try:
                    release = json.loads(stdout_queue.get(timeout=10))
                except queue.Empty:
                    self.fail("authentic owner did not publish release state")
                self.assertEqual(process.wait(timeout=10), 0)
                contender_after_release = _child_attempt(
                    database,
                    ROLE_OFFLINE_OPERATOR,
                )
            finally:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                stdout_reader.join(timeout=10)
                stderr_reader.join(timeout=10)
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

            self.assertFalse(stdout_reader.is_alive())
            self.assertFalse(stderr_reader.is_alive())
            self.assertEqual("".join(stderr_lines), "")
            self.assertEqual(
                {
                    "contender_after_release": contender_after_release,
                    "contender_while_held": contender_while_held,
                    "initial": initial,
                    "release": release,
                },
                {
                    "contender_after_release": "owned",
                    "contender_while_held": "contention",
                    "initial": {
                        "authentic_released": False,
                        "authentic_valid": True,
                        "coordination_unchanged": True,
                        "exact_type": True,
                        "forged_release": "invalid_capability",
                        "record_unchanged": True,
                        "slots_copied": 5,
                    },
                    "release": {
                        "authentic_release": "returned",
                        "authentic_released": True,
                        "command": "release",
                    },
                },
            )

    def test_arbitrary_base_exception_releases_while_probe_remains_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-B",
                    "-c",
                    _ARBITRARY_BASE_EXCEPTION_PROBE_SCRIPT,
                    str(database),
                ],
                cwd=ROOT,
                env=_child_environment(),
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_queue = queue.Queue()
            stderr_lines = []

            def collect_stdout():
                for line in process.stdout:
                    stdout_queue.put(line)

            def collect_stderr():
                stderr_lines.extend(process.stderr)

            stdout_reader = threading.Thread(
                target=collect_stdout,
                name="pb-own-base-exception-stdout",
                daemon=False,
            )
            stderr_reader = threading.Thread(
                target=collect_stderr,
                name="pb-own-base-exception-stderr",
                daemon=False,
            )
            stdout_reader.start()
            stderr_reader.start()
            initial = None
            acknowledgement = None
            first_contender = None
            second_contender = None
            alive_before_contenders = None
            alive_after_first = None
            alive_after_second = None
            try:
                try:
                    initial = json.loads(stdout_queue.get(timeout=10))
                except queue.Empty:
                    self.fail("BaseException probe did not publish state")
                alive_before_contenders = process.poll() is None
                first_contender = _child_attempt(
                    database,
                    ROLE_OFFLINE_OPERATOR,
                )
                alive_after_first = process.poll() is None
                second_contender = _child_attempt(
                    database,
                    ROLE_DURABLE_RUNTIME,
                )
                alive_after_second = process.poll() is None
                process.stdin.write("exit\n")
                process.stdin.flush()
                process.stdin.close()
                try:
                    acknowledgement = json.loads(
                        stdout_queue.get(timeout=10)
                    )
                except queue.Empty:
                    self.fail("BaseException probe did not acknowledge exit")
                self.assertEqual(process.wait(timeout=10), 0)
            finally:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                stdout_reader.join(timeout=10)
                stderr_reader.join(timeout=10)
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

            self.assertFalse(stdout_reader.is_alive())
            self.assertFalse(stderr_reader.is_alive())
            self.assertEqual("".join(stderr_lines), "")
            self.assertEqual(
                {
                    "acknowledgement": acknowledgement,
                    "alive_after_first": alive_after_first,
                    "alive_after_second": alive_after_second,
                    "alive_before_contenders": alive_before_contenders,
                    "first_contender": first_contender,
                    "initial": initial,
                    "second_contender": second_contender,
                },
                {
                    "acknowledgement": {"command": "exit"},
                    "alive_after_first": True,
                    "alive_after_second": True,
                    "alive_before_contenders": True,
                    "first_contender": "owned",
                    "initial": {
                        "coordination_empty": True,
                        "coordination_exists": True,
                        "descriptor_closed": True,
                        "descriptor_is_none": True,
                        "exception": {
                            "args": ["native_acquired", 73],
                            "direct_base": True,
                            "exact_object": True,
                            "is_exception": False,
                            "type": "ProbeAbort",
                        },
                        "lease_is_none": True,
                        "native_locked": False,
                        "record_registered": False,
                        "record_state": "retired",
                        "returned": False,
                        "same_process_acquisition_attempts": 1,
                        "sidecars": [],
                        "token_is_none": True,
                    },
                    "second_contender": "owned",
                },
            )

    def test_release_replay_cannot_affect_replacement_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            stale = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            release_database_lifetime_ownership(
                stale,
                role=ROLE_DURABLE_RUNTIME,
                database_path=database,
            )
            replacement = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            try:
                self.assertTrue(
                    release_database_lifetime_ownership(
                        stale,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=database,
                    )
                )
                self.assertTrue(
                    require_database_lifetime_ownership(
                        replacement,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=database,
                    )
                )
                self.assertEqual(
                    _child_attempt(database, ROLE_OFFLINE_OPERATOR),
                    "contention",
                )
            finally:
                release_database_lifetime_ownership(
                    replacement,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )

    def test_acquisition_interruptions_after_native_lock_leave_no_owner(self):
        boundaries = (
            "identity_captured",
            "coordination_open",
            "native_acquired",
            "published",
        )
        exception_types = (
            KeyboardInterrupt,
            _ArbitraryAcquisitionAbort,
        )
        for boundary in boundaries:
            for exception_type in exception_types:
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ), tempfile.TemporaryDirectory() as directory:
                    database = _create_database(directory)
                    coordination = (
                        ownership_module._coordination_path_for_database(
                            database
                        )
                    )
                    interruption = exception_type(boundary, 41)
                    observed = {}
                    returned = None
                    escaped = None
                    cleanup_was_needed = False

                    def interrupt(name, expected=boundary):
                        if name == expected:
                            with ownership_module._REGISTRY_LOCK:
                                record = next(
                                    iter(ownership_module._OWNERS.values())
                                )
                                observed["record"] = record
                                observed["descriptor"] = record.descriptor
                                observed["lease"] = record.lease
                            raise interruption

                    try:
                        returned = acquire_database_lifetime_ownership(
                            database,
                            role=ROLE_DURABLE_RUNTIME,
                            _checkpoint=interrupt,
                        )
                    except BaseException as exc:
                        escaped = exc
                    finally:
                        record = observed.get("record")
                        if record is not None:
                            with ownership_module._REGISTRY_LOCK:
                                cleanup_was_needed = (
                                    ownership_module._OWNERS.get(record.key)
                                    is record
                                )
                                if cleanup_was_needed:
                                    record.acquisition_failed = True
                                    record.state = (
                                        "cleanup_pending_locked"
                                        if record.native_locked
                                        else "cleanup_pending_unlocked"
                                    )
                                    self.assertTrue(
                                        ownership_module._retire_failed_acquisition_locked(
                                            record,
                                            observed.get("lease"),
                                        )
                                    )

                    self.assertIs(escaped, interruption)
                    self.assertEqual(escaped.args, (boundary, 41))
                    self.assertIsNone(returned)
                    self.assertFalse(cleanup_was_needed)
                    record = observed["record"]
                    descriptor = observed["descriptor"]
                    with ownership_module._REGISTRY_LOCK:
                        self.assertIsNone(
                            ownership_module._OWNERS.get(record.key)
                        )
                    self.assertEqual(record.state, "retired")
                    self.assertIsNone(record.descriptor)
                    self.assertFalse(record.native_locked)
                    self.assertIsNone(record.lease)
                    self.assertIsNone(record.lease_token)
                    published_lease = observed["lease"]
                    if published_lease is not None:
                        self.assertTrue(
                            database_lifetime_ownership_is_released(
                                published_lease
                            )
                        )
                    if descriptor is not None:
                        self.assertTrue(descriptor.closed)
                    if coordination.exists():
                        self.assertEqual(coordination.read_bytes(), b"")
                    for suffix in ("-journal", "-shm", "-wal"):
                        self.assertFalse(
                            Path(str(database) + suffix).exists()
                        )
                    later = acquire_database_lifetime_ownership(
                        database,
                        role=ROLE_OFFLINE_OPERATOR,
                    )
                    release_database_lifetime_ownership(
                        later,
                        role=ROLE_OFFLINE_OPERATOR,
                        database_path=database,
                    )

    def test_failed_acquisition_cleanup_remains_internally_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            original_close = (
                ownership_module._close_coordination_descriptor
            )
            close_calls = []

            def fail_first_close(descriptor):
                close_calls.append(descriptor)
                if len(close_calls) == 1:
                    raise OSError("controlled_close_failure")
                return original_close(descriptor)

            def interrupt(name):
                if name == "published":
                    raise KeyboardInterrupt(name)

            with mock.patch.object(
                ownership_module,
                "_close_coordination_descriptor",
                side_effect=fail_first_close,
            ):
                with self.assertRaises(
                    DatabaseLifetimeOwnershipError
                ) as caught:
                    acquire_database_lifetime_ownership(
                        database,
                        role=ROLE_DURABLE_RUNTIME,
                        _checkpoint=interrupt,
                    )
                self.assertEqual(
                    caught.exception.category,
                    "cleanup_incomplete",
                )
                later = acquire_database_lifetime_ownership(
                    database,
                    role=ROLE_OFFLINE_OPERATOR,
                )
                release_database_lifetime_ownership(
                    later,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=database,
                )
            self.assertEqual(len(close_calls), 3)

    def test_release_interruption_before_unlock_retains_exact_retry_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            owner = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )

            def interrupt(name):
                if name == "before_native_release":
                    raise KeyboardInterrupt(name)

            with self.assertRaises(KeyboardInterrupt):
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                    _checkpoint=interrupt,
                )
            self.assertEqual(
                _child_attempt(database, ROLE_OFFLINE_OPERATOR),
                "contention",
            )
            with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                acquire_database_lifetime_ownership(
                    database, role=ROLE_OFFLINE_OPERATOR
                )
            self.assertEqual(caught.exception.category, "contention")
            release_database_lifetime_ownership(
                owner,
                role=ROLE_DURABLE_RUNTIME,
                database_path=database,
            )
            self.assertEqual(
                _child_attempt(database, ROLE_OFFLINE_OPERATOR),
                "owned",
            )

    def test_release_interruption_after_unlock_is_terminal_and_truthful(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            for boundary in (
                "native_released",
                "descriptor_closed",
                "before_lease_retirement",
                "lease_retired",
            ):
                with self.subTest(boundary=boundary):
                    owner = acquire_database_lifetime_ownership(
                        database, role=ROLE_DURABLE_RUNTIME
                    )

                    def interrupt(name, expected=boundary):
                        if name == expected:
                            raise KeyboardInterrupt(expected)

                    with self.assertRaises(KeyboardInterrupt):
                        release_database_lifetime_ownership(
                            owner,
                            role=ROLE_DURABLE_RUNTIME,
                            database_path=database,
                            _checkpoint=interrupt,
                        )
                    self.assertTrue(
                        database_lifetime_ownership_is_released(owner)
                    )
                    later = acquire_database_lifetime_ownership(
                        database, role=ROLE_OFFLINE_OPERATOR
                    )
                    release_database_lifetime_ownership(
                        later,
                        role=ROLE_OFFLINE_OPERATOR,
                        database_path=database,
                    )

    def test_control_after_descriptor_close_is_terminal_and_replay_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            owner = acquire_database_lifetime_ownership(
                database,
                role=ROLE_DURABLE_RUNTIME,
            )
            original_close = (
                ownership_module._close_coordination_descriptor
            )

            def close_then_interrupt(descriptor):
                original_close(descriptor)
                raise KeyboardInterrupt("after_descriptor_close")

            with mock.patch.object(
                ownership_module,
                "_close_coordination_descriptor",
                side_effect=close_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    release_database_lifetime_ownership(
                        owner,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=database,
                    )
            self.assertTrue(database_lifetime_ownership_is_released(owner))
            replacement = acquire_database_lifetime_ownership(
                database,
                role=ROLE_OFFLINE_OPERATOR,
            )
            try:
                self.assertTrue(
                    release_database_lifetime_ownership(
                        owner,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=database,
                    )
                )
                self.assertEqual(
                    _child_attempt(database, ROLE_DURABLE_RUNTIME),
                    "contention",
                )
            finally:
                release_database_lifetime_ownership(
                    replacement,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=database,
                )

    def test_indeterminate_native_close_fails_closed_until_process_exit(self):
        script = r"""
import json, sys
import wahojobs.database_lifetime_ownership as module
from wahojobs.database_lifetime_ownership import (
    DatabaseLifetimeOwnershipError,
    ROLE_DURABLE_RUNTIME,
    ROLE_OFFLINE_OPERATOR,
    acquire_database_lifetime_ownership,
    database_lifetime_ownership_is_released,
    release_database_lifetime_ownership,
)
path = sys.argv[1]
owner = acquire_database_lifetime_ownership(
    path,
    role=ROLE_DURABLE_RUNTIME,
)
original_close = module._close_coordination_descriptor
def close_then_error(descriptor):
    original_close(descriptor)
    raise OSError("indeterminate_native_close")
module._close_coordination_descriptor = close_then_error
try:
    release_database_lifetime_ownership(
        owner,
        role=ROLE_DURABLE_RUNTIME,
        database_path=path,
    )
    release_result = "returned"
except DatabaseLifetimeOwnershipError as exc:
    release_result = exc.category
try:
    acquire_database_lifetime_ownership(
        path,
        role=ROLE_OFFLINE_OPERATOR,
    )
    reacquire_result = "owned"
except DatabaseLifetimeOwnershipError as exc:
    reacquire_result = exc.category
print(json.dumps({
    "release": release_result,
    "released": database_lifetime_ownership_is_released(owner),
    "reacquire": reacquire_result,
}, sort_keys=True))
"""
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script, str(database)],
                cwd=ROOT,
                env=_child_environment(),
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            self.assertFalse(completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout),
                {
                    "reacquire": "contention",
                    "release": "cleanup_incomplete",
                    "released": False,
                },
            )
            self.assertEqual(
                _child_attempt(database, ROLE_OFFLINE_OPERATOR),
                "owned",
            )

    def test_descriptor_is_noninheritable_and_coordination_identity_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            owner = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            try:
                records = tuple(ownership_module._OWNERS.values())
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertFalse(
                    os.get_inheritable(record.descriptor.fileno())
                )
                current = os.stat(record.coordination_path)
                self.assertEqual(
                    (current.st_dev, current.st_ino),
                    (
                        record.coordination_identity.device,
                        record.coordination_identity.inode,
                    ),
                )
            finally:
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )

    def test_public_failures_and_representations_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            owner = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            try:
                with self.assertRaises(DatabaseLifetimeOwnershipError) as caught:
                    acquire_database_lifetime_ownership(
                        database, role=ROLE_OFFLINE_OPERATOR
                    )
                values = (
                    str(caught.exception),
                    repr(caught.exception),
                    repr(owner),
                    str(owner),
                )
                forbidden = (
                    str(database),
                    database.name,
                    str(os.getpid()),
                    "descriptor",
                    "inode",
                    "token",
                    "0x",
                )
                for value in values:
                    for fragment in forbidden:
                        self.assertNotIn(fragment, value)

                with mock.patch.object(
                    ownership_module,
                    "_revalidate_record_files",
                    side_effect=OSError(str(database)),
                ):
                    with self.assertRaises(
                        DatabaseLifetimeOwnershipError
                    ) as retained:
                        require_database_lifetime_ownership(
                            owner,
                            role=ROLE_DURABLE_RUNTIME,
                            database_path=database,
                        )
                self.assertEqual(
                    retained.exception.category,
                    "ownership_lost",
                )
                self.assertIsNone(retained.exception.__context__)
                self.assertIsNone(retained.exception.__cause__)
                self.assertNotIn(str(database), str(retained.exception))
            finally:
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )

    def test_native_backend_contracts_are_explicit_for_windows_and_posix(self):
        class FakeWindows:
            LK_NBLCK = 10
            LK_UNLCK = 11

            def __init__(self):
                self.calls = []

            def locking(self, descriptor, operation, length):
                self.calls.append((descriptor, operation, length))

        class FakePosix:
            LOCK_EX = 1
            LOCK_NB = 2
            LOCK_UN = 4

            def __init__(self):
                self.calls = []

            def flock(self, descriptor, operation):
                self.calls.append((descriptor, operation))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native"
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            try:
                windows = FakeWindows()
                windows_backend = ownership_module._WindowsNativeBackend(windows)
                windows_backend.acquire(descriptor)
                windows_backend.release(descriptor)
                self.assertEqual(
                    windows.calls,
                    [(descriptor, 10, 1), (descriptor, 11, 1)],
                )

                posix = FakePosix()
                posix_backend = ownership_module._PosixNativeBackend(posix)
                posix_backend.acquire(descriptor)
                posix_backend.release(descriptor)
                self.assertEqual(
                    posix.calls,
                    [(descriptor, 3), (descriptor, 4)],
                )
            finally:
                os.close(descriptor)

    def test_primitive_does_not_activate_product_or_network_subsystems(self):
        script = r"""
import json, sqlite3, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "db.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("create table t(x)")
    connection.commit()
    connection.close()
    before = set(sys.modules)
    from wahojobs.database_lifetime_ownership import (
        ROLE_DURABLE_RUNTIME,
        acquire_database_lifetime_ownership,
        release_database_lifetime_ownership,
    )
    owner = acquire_database_lifetime_ownership(path.resolve(), role=ROLE_DURABLE_RUNTIME)
    release_database_lifetime_ownership(owner, role=ROLE_DURABLE_RUNTIME, database_path=path.resolve())
    activated = sorted(name for name in set(sys.modules) - before if name.startswith((
        "wahojobs.accounts", "wahojobs.google", "wahojobs.matching",
        "wahojobs.persistent", "requests", "authlib", "ssl", "socket"
    )))
    print(json.dumps(activated))
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            env=_child_environment(),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(json.loads(completed.stdout), [])
        self.assertFalse(completed.stderr)

    def test_runtime_acquires_before_presecret_attestation_and_idle_use(self):
        from tests.durable_google_login_browser_test_support import (
            loopback_and_in_memory_provider_only,
            temporary_browser_login_state,
        )
        import wahojobs.durable_google_login_runtime as runtime_module

        events = []
        pending = None
        runtime = None
        with temporary_browser_login_state() as state:
            original_attest = runtime_module._attest_existing_database

            def checkpoint(name):
                events.append(("checkpoint", name))

            def prepare_presecret():
                events.append(("boundary", "presecret"))
                self.assertEqual(
                    _child_attempt(
                        state.database_path,
                        ROLE_OFFLINE_OPERATOR,
                    ),
                    "contention",
                )

            def attest(*args, **kwargs):
                events.append(("boundary", "attestation"))
                self.assertEqual(
                    _child_attempt(
                        state.database_path,
                        ROLE_OFFLINE_OPERATOR,
                    ),
                    "contention",
                )
                return original_attest(*args, **kwargs)

            with loopback_and_in_memory_provider_only(), mock.patch.object(
                runtime_module,
                "_attest_existing_database",
                side_effect=attest,
            ):
                try:
                    pending = (
                        runtime_module.prepare_durable_google_login_activation(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                            _pre_secret_preparer=prepare_presecret,
                            _checkpoint=checkpoint,
                        )
                    )
                    runtime = pending.complete_activation()
                    pending = None
                    self.assertTrue(
                        runtime.require_database_lifetime_ownership()
                    )
                    for role in ROLES:
                        self.assertEqual(
                            _child_attempt(state.database_path, role),
                            "contention",
                        )
                finally:
                    if runtime is not None:
                        runtime.close()
                    elif pending is not None:
                        pending.close()

            ownership_index = events.index(
                ("checkpoint", "database_lifetime_owned")
            )
            self.assertLess(
                ownership_index,
                events.index(("boundary", "presecret")),
            )
            self.assertLess(
                ownership_index,
                events.index(("boundary", "attestation")),
            )
            self.assertLess(
                events.index(("boundary", "attestation")),
                events.index(("boundary", "presecret")),
            )
            self.assertLess(
                events.index(("checkpoint", "database_lifetime_owned")),
                events.index(("checkpoint", "database_attested")),
            )
            self.assertLess(
                events.index(("checkpoint", "database_attested")),
                events.index(("boundary", "presecret")),
            )
            self.assertEqual(
                _child_attempt(
                    state.database_path,
                    ROLE_OFFLINE_OPERATOR,
                ),
                "owned",
            )
            for suffix in ("-journal", "-wal", "-shm"):
                self.assertFalse(
                    state.database_path.with_name(
                        state.database_path.name + suffix
                    ).exists()
                )

    def test_runtime_ownership_loss_still_allows_terminal_cleanup(self):
        from tests.durable_google_login_browser_test_support import (
            loopback_and_in_memory_provider_only,
            temporary_browser_login_state,
        )
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            with loopback_and_in_memory_provider_only():
                runtime = runtime_module.build_durable_google_login_runtime(
                    state.configuration_path,
                    _clock=state.clock,
                    _gateway_factory=state.gateway_factory,
                )
            displaced = state.database_path.with_name(
                "displaced-runtime.sqlite3"
            )
            replacement = state.database_path.with_name(
                "replacement-runtime.sqlite3"
            )
            shutil.copy2(state.database_path, replacement)
            os.replace(state.database_path, displaced)
            os.replace(replacement, state.database_path)
            with self.assertRaises(
                runtime_module.DurableGoogleLoginConfigurationError
            ):
                runtime.require_database_lifetime_ownership()
            report = runtime.close()
            self.assertTrue(report.cleanup_complete)
            self.assertEqual(
                _child_attempt(
                    state.database_path,
                    ROLE_OFFLINE_OPERATOR,
                ),
                "owned",
            )

    def test_startup_failure_after_acquisition_releases_ownership(self):
        from tests.durable_google_login_browser_test_support import (
            temporary_browser_login_state,
        )
        import wahojobs.durable_google_login_runtime as runtime_module

        checkpoints = []

        def fail_after_ownership():
            raise RuntimeError("controlled_presecret_failure")

        with temporary_browser_login_state() as state:
            with self.assertRaises(
                runtime_module.DurableGoogleLoginConfigurationError
            ):
                runtime_module.prepare_durable_google_login_activation(
                    state.configuration_path,
                    _pre_secret_preparer=fail_after_ownership,
                    _checkpoint=checkpoints.append,
                )
            self.assertIn("database_lifetime_owned", checkpoints)
            self.assertNotIn("secrets_loaded", checkpoints)
            self.assertEqual(
                _child_attempt(
                    state.database_path,
                    ROLE_OFFLINE_OPERATOR,
                ),
                "owned",
            )

    def test_terminal_cleanup_dependency_retains_ownership_until_database_closes(
        self,
    ):
        from tests.durable_google_login_browser_test_support import (
            temporary_browser_login_state,
        )
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            configuration = runtime_module._load_construction_configuration(
                state.configuration_path
            )
            target = configuration.database_target
            coordinator = runtime_module._CleanupCoordinator()
            lifetime = runtime_module._DatabaseLifetimeOwnershipResource(target)
            coordinator.own(
                "database_lifetime_ownership",
                lifetime,
                runtime_module._cleanup_database_lifetime_ownership_resource,
                probe=(
                    runtime_module._database_lifetime_ownership_resource_is_closed
                ),
                dependencies=("database_connections",),
                require_terminal_dependencies=True,
            )
            owner = acquire_database_lifetime_ownership(
                state.database_path,
                role=ROLE_DURABLE_RUNTIME,
                _publisher=lifetime.publish,
            )
            close_results = [False, True]
            database_resource = object()

            def close_database(_resource):
                return close_results.pop(0)

            coordinator.own(
                "database_connections",
                database_resource,
                close_database,
            )
            try:
                first = coordinator.cleanup()
                self.assertFalse(first.cleanup_complete)
                self.assertEqual(
                    set(first.unresolved_resources),
                    {
                        "database_connections",
                        "database_lifetime_ownership",
                    },
                )
                self.assertTrue(
                    require_database_lifetime_ownership(
                        owner,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=state.database_path,
                    )
                )
                self.assertEqual(
                    _child_attempt(
                        state.database_path,
                        ROLE_OFFLINE_OPERATOR,
                    ),
                    "contention",
                )
                second = coordinator.cleanup()
                self.assertTrue(second.cleanup_complete)
                self.assertTrue(
                    database_lifetime_ownership_is_released(owner)
                )
                self.assertEqual(
                    _child_attempt(
                        state.database_path,
                        ROLE_OFFLINE_OPERATOR,
                    ),
                    "owned",
                )
            finally:
                if not database_lifetime_ownership_is_released(owner):
                    release_database_lifetime_ownership(
                        owner,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=state.database_path,
                    )
                configuration = None
                target = None

    def test_launcher_contention_never_constructs_binds_or_prints_ready(self):
        from scripts import durable_google_login_app
        from tests.durable_google_login_browser_test_support import (
            temporary_browser_login_state,
        )

        with temporary_browser_login_state() as state:
            owner = acquire_database_lifetime_ownership(
                state.database_path,
                role=ROLE_OFFLINE_OPERATOR,
            )
            calls = []

            def forbidden_factory(*_args, **_kwargs):
                calls.append("called")
                raise AssertionError("post_ownership_factory_called")

            stdout = io.StringIO()
            stderr = io.StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = durable_google_login_app.main(
                        ["--config", str(state.configuration_path)],
                        _server_factory=forbidden_factory,
                        _tls_context_factory=forbidden_factory,
                    )
            finally:
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=state.database_path,
                )
            self.assertEqual(result, 2)
            self.assertEqual(calls, [])
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue().strip(),
                "Durable Google login could not start safely.",
            )
            combined = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(str(state.database_path), combined)
            self.assertNotIn(str(state.configuration_path), combined)

    def test_fully_idle_ready_launcher_excludes_both_roles_then_releases(self):
        from tests.durable_google_login_browser_test_support import (
            temporary_browser_login_state,
        )

        guarded_launcher = r"""
import ipaddress
import socket
import sys

original_socket = socket.socket
original_create_connection = socket.create_connection
original_getaddrinfo = socket.getaddrinfo

def allowed_host(value):
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except (TypeError, ValueError):
        return False

class GuardedSocket(original_socket):
    def connect(self, address):
        if not (isinstance(address, tuple) and allowed_host(address[0])):
            raise AssertionError("external_socket_forbidden")
        return super().connect(address)
    def connect_ex(self, address):
        if not (isinstance(address, tuple) and allowed_host(address[0])):
            raise AssertionError("external_socket_forbidden")
        return super().connect_ex(address)

def guarded_create_connection(address, *args, **kwargs):
    if not (isinstance(address, tuple) and allowed_host(address[0])):
        raise AssertionError("external_socket_forbidden")
    return original_create_connection(address, *args, **kwargs)

def guarded_getaddrinfo(host, *args, **kwargs):
    if host is not None and not allowed_host(host):
        raise AssertionError("external_socket_forbidden")
    return original_getaddrinfo(host, *args, **kwargs)

socket.socket = GuardedSocket
socket.create_connection = guarded_create_connection
socket.getaddrinfo = guarded_getaddrinfo

from scripts.durable_google_login_app import main
raise SystemExit(main(["--config", sys.argv[1]]))
"""

        with temporary_browser_login_state(
            port=_unused_loopback_port()
        ) as state:
            popen_arguments = {}
            if os.name == "nt":
                popen_arguments["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                popen_arguments["start_new_session"] = True
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-B",
                    "-c",
                    guarded_launcher,
                    str(state.configuration_path),
                ],
                cwd=ROOT,
                env=_child_environment(),
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **popen_arguments,
            )
            stdout_lines = []
            ready = threading.Event()

            def collect_stdout():
                for line in process.stdout:
                    stdout_lines.append(line)
                    if line.strip() == "Wahojobs durable Google login":
                        ready.set()

            reader = threading.Thread(
                target=collect_stdout,
                name="pb-own-ready-output-reader",
                daemon=False,
            )
            reader.start()
            stderr_text = ""
            try:
                self.assertTrue(
                    ready.wait(20),
                    "launcher did not reach ready state",
                )
                self.assertIsNone(process.poll())
                for role in ROLES:
                    self.assertEqual(
                        _child_attempt(state.database_path, role),
                        "contention",
                    )
                if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    expected_exit = 149
                else:
                    os.killpg(process.pid, signal.SIGINT)
                    expected_exit = 130
                self.assertEqual(process.wait(timeout=20), expected_exit)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                reader.join(timeout=10)
                if process.stderr is not None:
                    stderr_text = process.stderr.read()
                    process.stderr.close()
                if process.stdout is not None:
                    process.stdout.close()
            self.assertFalse(reader.is_alive())
            self.assertEqual(stderr_text, "")
            self.assertIn(
                "Wahojobs durable Google login\n",
                stdout_lines,
            )
            self.assertEqual(
                _child_attempt(
                    state.database_path,
                    ROLE_OFFLINE_OPERATOR,
                ),
                "owned",
            )

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX fork is unavailable on this host")
    def test_posix_fork_invalidates_child_authority_without_unlocking_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = _create_database(directory)
            owner = acquire_database_lifetime_ownership(
                database, role=ROLE_DURABLE_RUNTIME
            )
            read_descriptor, write_descriptor = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(read_descriptor)
                outcome = []
                try:
                    require_database_lifetime_ownership(
                        owner,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=database,
                    )
                    outcome.append("inherited-valid")
                except DatabaseLifetimeOwnershipError as exc:
                    outcome.append("inherited-" + exc.category)
                try:
                    acquire_database_lifetime_ownership(
                        database, role=ROLE_OFFLINE_OPERATOR
                    )
                    outcome.append("fresh-owned")
                except DatabaseLifetimeOwnershipError as exc:
                    outcome.append("fresh-" + exc.category)
                os.write(write_descriptor, json.dumps(outcome).encode("ascii"))
                os.close(write_descriptor)
                os._exit(0)
            os.close(write_descriptor)
            payload = os.read(read_descriptor, 4096)
            os.close(read_descriptor)
            _, status = os.waitpid(child, 0)
            self.assertEqual(status, 0)
            self.assertEqual(
                json.loads(payload),
                ["inherited-invalid_capability", "fresh-contention"],
            )
            self.assertTrue(
                require_database_lifetime_ownership(
                    owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database,
                )
            )
            release_database_lifetime_ownership(
                owner,
                role=ROLE_DURABLE_RUNTIME,
                database_path=database,
            )
            later = acquire_database_lifetime_ownership(
                database, role=ROLE_OFFLINE_OPERATOR
            )
            release_database_lifetime_ownership(
                later,
                role=ROLE_OFFLINE_OPERATOR,
                database_path=database,
            )


if __name__ == "__main__":
    unittest.main()
