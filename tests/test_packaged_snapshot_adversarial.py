"""Synthetic, offline adversarial controls for immutable packaged recovery journals."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

from wahojobs import beta_recovery as recovery
from wahojobs.evidence_maintenance import pin_journal
from tests.evidence_maintenance_support import new_inventory

V2 = "private_beta_cold_snapshot_v2"


class PackagedSnapshotAdversarialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="packaged-adversarial-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.database = new_inventory(self.source)
        self.journal = self.source / "journal"
        self.journal.mkdir()
        pin_journal(self.database, self.journal)
        self.snapshot = self.root / "snapshot"
        self.prepared = self.root / "prepared"
        self.restored = self.root / "restored"
        self.options = dict(run_id="synthetic-adversarial-v2", code_commit="c" * 40,
                            configuration_revision="synthetic-config-v1")

    def prepare(self):
        return recovery.prepare_snapshot_journal(self.database, self.prepared,
            snapshot_version=V2, **self.options)

    def create(self, receipt):
        return recovery.create_snapshot(self.database, self.snapshot,
            prepared_journal=self.prepared,
            expected_preparation_sha256=sha256(recovery._json(receipt)).hexdigest(),
            **self.options)

    def package(self):
        return self.create(self.prepare())

    def seed_opaque(self, count=3):
        directory = self.journal / "opaque-synthetic-evidence"
        directory.mkdir()
        for index in range(count):
            (directory / f"{index:06d}.raw").write_bytes(f"SYNTHETIC immutable evidence {index}\n".encode())

    def segment(self, manifest):
        name = next(iter(manifest["journal_segments"]))
        return name, self.snapshot / name

    def reseal(self, manifest, *, index_raw=None):
        # Recomputing public checksum files must not bypass deep archive checks.
        name, path = self.segment(manifest)
        digest = sha256(path.read_bytes()).hexdigest()
        manifest["files"][name]["sha256"] = digest
        manifest["journal_segments"][name]["sha256"] = digest
        if index_raw is not None:
            manifest["journal_segments"][name]["index_sha256"] = sha256(index_raw).hexdigest()
        raw = recovery._json(manifest)
        (self.snapshot / "manifest.json").write_bytes(raw)
        (self.snapshot / "COMPLETE.sha256").write_text(sha256(raw).hexdigest() + "\n", encoding="ascii")

    def rewrite_segment(self, manifest, transform):
        _, path = self.segment(manifest)
        with zipfile.ZipFile(path) as stream:
            records = [(member, stream.read(member)) for member in stream.infolist()]
        records = transform(records)
        with warnings.catch_warnings(), zipfile.ZipFile(path, "w") as stream:
            warnings.simplefilter("ignore", UserWarning)
            for member, raw in records:
                stream.writestr(member, raw)
        index_raw = next(raw for member, raw in records if member.filename == "JOURNAL-INDEX.json")
        self.reseal(manifest, index_raw=index_raw)

    def assert_rejected_without_ready(self):
        with self.assertRaises(ValueError):
            recovery.verify_snapshot(self.snapshot)
        with self.assertRaises(ValueError):
            recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertFalse((self.restored / "RECOVERY-READY.json").exists())
        self.assertFalse((self.root / "escaped.raw").exists())

    def test_more_than_10000_logical_files_roundtrip_without_widening_v1(self):
        from wahojobs.storage_relocation import reconcile_relocation
        self.seed_opaque(recovery.MAX_FILES + 17)
        source_hash = sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "file_limit"):
            recovery.create_snapshot(self.database, self.root / "v1-rejected",
                code_commit=self.options["code_commit"],
                configuration_revision=self.options["configuration_revision"])
        self.assertFalse((self.root / "v1-rejected" / "COMPLETE.sha256").exists())
        manifest = self.package()
        self.assertEqual(manifest["version"], V2)
        self.assertLessEqual(len(manifest["files"]), recovery.MAX_FILES)
        self.assertLessEqual((self.snapshot / "manifest.json").stat().st_size, recovery.MAX_MANIFEST_BYTES)
        recovery.verify_snapshot(self.snapshot)
        recovery.restore_snapshot(self.snapshot, self.restored)
        original = {path.relative_to(self.journal).as_posix(): sha256(path.read_bytes()).hexdigest()
                    for path in self.journal.rglob("*") if path.is_file()}
        restored = {path.relative_to(self.restored / "journal").as_posix(): sha256(path.read_bytes()).hexdigest()
                    for path in (self.restored / "journal").rglob("*") if path.is_file()}
        self.assertEqual(original, restored)
        self.assertGreater(len(restored), recovery.MAX_FILES)
        self.assertEqual(sha256(self.database.read_bytes()).hexdigest(), source_hash)
        reconcile_relocation(self.snapshot, self.restored, self.database)
        self.assertEqual(sha256(self.database.read_bytes()).hexdigest(), source_hash)
        self.assertEqual(len([path for path in self.journal.rglob("*") if path.is_file()]), len(original))

    def test_interrupted_packaged_snapshot_and_restore_never_claim_complete(self):
        self.seed_opaque()
        receipt = self.prepare()
        original_write = recovery._write
        def interrupted_complete(path, raw):
            if path.name == "COMPLETE.sha256":
                raise OSError("SYNTHETIC completion interruption")
            return original_write(path, raw)
        with patch.object(recovery, "_write", side_effect=interrupted_complete):
            with self.assertRaisesRegex(OSError, "SYNTHETIC"):
                self.create(receipt)
        self.assertFalse((self.snapshot / "COMPLETE.sha256").exists())
        self.prepared = self.root / "fresh-prepared"
        self.snapshot = self.root / "fresh-snapshot"
        self.package()
        with patch.object(recovery.shutil, "copyfileobj", side_effect=OSError("SYNTHETIC restore interruption")):
            with self.assertRaisesRegex(OSError, "SYNTHETIC"):
                recovery.restore_snapshot(self.snapshot, self.restored)
        self.assertFalse((self.restored / "RECOVERY-READY.json").exists())

    def test_changed_live_evidence_after_preparation_never_completes(self):
        self.seed_opaque()
        receipt = self.prepare()
        next(self.journal.rglob("*.raw")).write_bytes(b"SYNTHETIC later evidence")
        with self.assertRaises(ValueError):
            self.create(receipt)
        self.assertFalse((self.snapshot / "COMPLETE.sha256").exists())

    def test_mutable_preparation_checksum_is_not_authority(self):
        self.seed_opaque()
        receipt = self.prepare()
        forged = json.loads((self.prepared / "PREPARED.json").read_bytes())
        forged["run_id"] = "forged-other-run"
        raw = recovery._json(forged)
        (self.prepared / "PREPARED.json").write_bytes(raw)
        (self.prepared / "PREPARED.sha256").write_text(sha256(raw).hexdigest() + "\n", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "integrity|binding"):
            self.create(receipt)
        self.assertFalse((self.snapshot / "COMPLETE.sha256").exists())

    def test_archive_metadata_attacks_rejected_even_when_outer_checksum_resealed(self):
        self.seed_opaque()
        baseline = self.package()
        _, segment = self.segment(baseline)
        original = segment.read_bytes()
        for attack in ("link", "directory", "duplicate", "bzip2", "index_duplicate_key"):
            with self.subTest(attack=attack):
                segment.write_bytes(original)
                manifest = json.loads(json.dumps(baseline))
                def transform(records):
                    if attack == "index_duplicate_key":
                        changed = []
                        for member, raw in records:
                            if member.filename == "JOURNAL-INDEX.json":
                                value = json.loads(raw)
                                raw = ("{\"version\":" + json.dumps(value["version"]) + "," + raw.decode().lstrip()[1:]).encode()
                            changed.append((member, raw))
                        return changed
                    position = next(i for i, (member, _) in enumerate(records) if member.filename != "JOURNAL-INDEX.json")
                    member, raw = records[position]
                    if attack == "link":
                        member.create_system = 3
                        member.external_attr = (stat.S_IFLNK | 0o777) << 16
                    elif attack == "directory":
                        member.create_system = 3
                        member.external_attr = (stat.S_IFDIR | 0o700) << 16
                    elif attack == "bzip2":
                        member.compress_type = zipfile.ZIP_BZIP2
                    elif attack == "duplicate":
                        records.append((member, raw))
                    return records
                self.rewrite_segment(manifest, transform)
                self.assert_rejected_without_ready()

    def test_zip64_local_headers_rejected_even_without_zip64_end_record(self):
        self.seed_opaque()
        manifest = self.package()
        _, segment = self.segment(manifest)
        with zipfile.ZipFile(segment) as stream:
            members = [(info, stream.read(info)) for info in stream.infolist()]
        with zipfile.ZipFile(segment, "w") as stream:
            for info, raw in members:
                with stream.open(info, "w", force_zip64=True) as output:
                    output.write(raw)
        self.assertNotIn(b"PK\x06\x06", segment.read_bytes())
        self.reseal(manifest)
        self.assert_rejected_without_ready()
    def test_hidden_deflate_suffix_is_rejected_for_payload_and_index(self):
        self.seed_opaque()
        baseline = self.package()
        _, segment = self.segment(baseline)
        original = segment.read_bytes()
        for index_attack in (False, True):
            with self.subTest(index=index_attack):
                segment.write_bytes(original)
                manifest = json.loads(json.dumps(baseline))
                with zipfile.ZipFile(segment) as stream:
                    records = [(member, stream.read(member)) for member in stream.infolist()]
                victim, visible = next((member, raw) for member, raw in records
                    if (member.filename == "JOURNAL-INDEX.json") == index_attack)
                victim_name = victim.filename
                with zipfile.ZipFile(segment, "w") as stream:
                    for member, raw in records:
                        member.compress_type = zipfile.ZIP_DEFLATED
                        stream.writestr(member, raw + b"HIDDEN expansion" * 131072
                            if member.filename == victim_name else raw)
                raw = bytearray(segment.read_bytes())
                end = raw.rfind(b"PK\x05\x06")
                central = struct.unpack_from("<I", raw, end + 16)[0]
                while raw[central:central + 4] == b"PK\x01\x02":
                    name_length, extra_length, comment_length = struct.unpack_from("<3H", raw, central + 28)
                    name = raw[central + 46:central + 46 + name_length].decode("utf-8")
                    if name == victim_name:
                        local = struct.unpack_from("<I", raw, central + 42)[0]
                        for crc_offset, size_offset in ((central + 16, central + 24), (local + 14, local + 22)):
                            struct.pack_into("<I", raw, crc_offset, zlib.crc32(visible))
                            struct.pack_into("<I", raw, size_offset, len(visible))
                        break
                    central += 46 + name_length + extra_length + comment_length
                else:
                    self.fail("Synthetic victim missing from central directory")
                segment.write_bytes(raw)
                # Demonstrate why checks using ZipExtFile alone miss this case.
                with zipfile.ZipFile(segment) as stream:
                    self.assertEqual(stream.read(victim_name), visible)
                self.reseal(manifest)
                self.assert_rejected_without_ready()

    def test_valid_deflate_members_cross_output_and_input_chunk_boundaries(self):
        self.seed_opaque(2)
        paths = sorted(self.journal.rglob("*.raw"))
        # Compressible data crosses the 1 MiB output bound; deterministic noise
        # also crosses the 64 KiB compressed-input bound without random fixtures.
        paths[0].write_bytes(b"SYNTHETIC " * (1024 * 1024 // 10 + 17))
        noise = b"".join(sha256(str(index).encode("ascii")).digest() for index in range(40000))
        paths[1].write_bytes(noise)
        manifest = self.package()
        self.assertEqual(recovery.verify_snapshot(self.snapshot), manifest)
        recovery.restore_snapshot(self.snapshot, self.restored)
        for path in paths:
            self.assertEqual((self.restored / "journal" / path.relative_to(self.journal)).read_bytes(), path.read_bytes())
    def test_raw_zip_header_attacks_rejected_after_resealing(self):
        self.seed_opaque()
        baseline = self.package()
        _, segment = self.segment(baseline)
        original = segment.read_bytes()
        for attack in ("encrypted", "multidisk", "bad_crc", "oversize"):
            with self.subTest(attack=attack):
                raw = bytearray(original)
                local = raw.find(b"PK\x03\x04")
                central = raw.find(b"PK\x01\x02")
                if attack == "encrypted":
                    for offset in (local + 6, central + 8):
                        struct.pack_into("<H", raw, offset, struct.unpack_from("<H", raw, offset)[0] | 1)
                elif attack == "multidisk":
                    end = raw.rfind(b"PK\x05\x06")
                    struct.pack_into("<H", raw, end + 4, 1)
                elif attack == "bad_crc":
                    for offset in (local + 14, central + 16):
                        struct.pack_into("<I", raw, offset, 1)
                elif attack == "oversize":
                    for offset in (local + 22, central + 24):
                        struct.pack_into("<I", raw, offset, 129 * 1024 * 1024)
                segment.write_bytes(raw)
                manifest = json.loads(json.dumps(baseline))
                self.reseal(manifest)
                self.assert_rejected_without_ready()

    def test_path_alias_and_traversal_rejected_with_consistent_index(self):
        self.seed_opaque()
        baseline = self.package()
        _, segment = self.segment(baseline)
        original = segment.read_bytes()
        for attack in ("journal/../../escaped.raw", "journal/CON.raw", "journal/x/../escaped.raw"):
            with self.subTest(name=attack):
                segment.write_bytes(original)
                manifest = json.loads(json.dumps(baseline))
                def transform(records):
                    old = next(member.filename for member, _ in records if member.filename != "JOURNAL-INDEX.json")
                    changed = []
                    for member, raw in records:
                        if member.filename == old:
                            member.filename = attack
                        elif member.filename == "JOURNAL-INDEX.json":
                            index = json.loads(raw)
                            index["files"][attack] = index["files"].pop(old)
                            raw = recovery._json(index)
                        changed.append((member, raw))
                    return changed
                self.rewrite_segment(manifest, transform)
                self.assert_rejected_without_ready()

    def test_directory_case_aliases_and_file_directory_conflicts_are_not_restorable(self):
        self.seed_opaque()
        baseline = self.package()
        _, segment = self.segment(baseline)
        original = segment.read_bytes()
        for replacement in ("journal/OPAQUE-SYNTHETIC-EVIDENCE/000000.raw",
                            "journal/opaque-synthetic-evidence"):
            with self.subTest(replacement=replacement):
                segment.write_bytes(original)
                manifest = json.loads(json.dumps(baseline))
                def transform(records):
                    old = next(member.filename for member, _ in records if member.filename != "JOURNAL-INDEX.json")
                    updated = []
                    for member, raw in records:
                        if member.filename == old:
                            member.filename = replacement
                        elif member.filename == "JOURNAL-INDEX.json":
                            index = json.loads(raw)
                            index["files"][replacement] = index["files"].pop(old)
                            names = sorted(index["files"])
                            digest = sha256()
                            for name in names:
                                digest.update(json.dumps([name, index["files"][name]], sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")
                            manifest["journal_inventory"]["sha256"] = digest.hexdigest()
                            segment_name = next(iter(manifest["journal_segments"]))
                            manifest["journal_segments"][segment_name].update(first=names[0], last=names[-1])
                            raw = recovery._json(index)
                        updated.append((member, raw))
                    return updated
                self.rewrite_segment(manifest, transform)
                self.assert_rejected_without_ready()
    def test_compressed_and_central_directory_bounds_checked_before_zip_parser(self):
        from wahojobs import recovery_archive as archive
        self.seed_opaque()
        manifest = self.package()
        _, segment = self.segment(manifest)
        for bound, limit in (("MAX_ARCHIVE_BYTES", segment.stat().st_size - 1), ("MAX_CENTRAL_BYTES", 1)):
            with self.subTest(bound=bound), patch.object(archive, bound, limit), \
                    patch.object(zipfile, "ZipFile", side_effect=AssertionError("unbounded ZIP parser reached")):
                with self.assertRaises(ValueError):
                    recovery.verify_snapshot(self.snapshot)

    def test_v2_relocation_interruption_preserves_fence_and_identical_retry(self):
        from wahojobs import storage_relocation as relocation
        self.seed_opaque()
        self.package()
        recovery.restore_snapshot(self.snapshot, self.restored)
        target = self.restored / "product.sqlite3"
        def interrupted(stage):
            if stage == "source_retired":
                raise OSError("SYNTHETIC activation interruption")
        with self.assertRaisesRegex(OSError, "SYNTHETIC"):
            relocation.reconcile_relocation(self.snapshot, self.restored, self.database,
                checkpoint=interrupted)
        self.assertTrue(relocation.sidecar(self.database, relocation.RETIRED).exists())
        self.assertFalse(relocation.sidecar(target, relocation.LINEAGE).exists())
        with self.assertRaises(ValueError):
            relocation.require_storage_activation(target)
        receipt = relocation.reconcile_relocation(self.snapshot, self.restored, self.database)
        self.assertEqual(receipt["version"], relocation.VERSION_V2)
        relocation.require_storage_activation(target)
        with self.assertRaisesRegex(ValueError, "retired"):
            relocation.require_storage_activation(self.database)
        self.assertEqual(len(list(self.journal.rglob("*.raw"))), 3)
        self.assertEqual(len(list((self.restored / "journal").rglob("*.raw"))), 3)

    def test_v2_lineage_chunk_bounds_precede_hashing_and_predecessor_is_bound(self):
        from wahojobs import storage_relocation as relocation
        from wahojobs import recovery_archive as archive
        self.seed_opaque()
        self.package()
        recovery.restore_snapshot(self.snapshot, self.restored)
        target = self.restored / "product.sqlite3"
        receipt = relocation.reconcile_relocation(self.snapshot, self.restored, self.database)
        chunk_name = receipt["journal_chunks"][0]["sha256"] + ".json"
        original_hash = recovery._hash
        def bounded_hash(path):
            if Path(path).name == chunk_name:
                raise AssertionError("oversized lineage chunk hashed before its size check")
            return original_hash(path)
        with patch.object(archive, "MAX_INDEX_BYTES", 1), patch.object(recovery, "_hash", side_effect=bounded_hash):
            with self.assertRaises(ValueError):
                relocation.require_storage_activation(target)
        receipt["predecessor"] = "f" * 64
        relocation.sidecar(target, relocation.LINEAGE).write_bytes(recovery._json(receipt))
        with self.assertRaises(ValueError):
            relocation.require_storage_activation(target)
    def test_no_disk_space_fails_during_online_preparation(self):
        self.seed_opaque()
        before = sha256(self.database.read_bytes()).hexdigest()
        with patch.object(shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 100, 0)):
            with self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse((self.prepared / "PREPARED.json").exists())
        self.assertEqual(sha256(self.database.read_bytes()).hexdigest(), before)
        self.assertEqual(len(list(self.journal.rglob("*.raw"))), 3)


if __name__ == "__main__":
    unittest.main()
