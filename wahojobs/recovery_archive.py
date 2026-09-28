"""Bounded, self-contained journal packaging for recovery format v2.

Logical names are never native extraction paths until canonical validation.
Original identities are provenance only. No live evidence is removed.
"""
from collections import defaultdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import zipfile
import zlib

VERSION = 'private_beta_cold_snapshot_v2'
INDEX_VERSION = 'private_beta_journal_segment_v1'
INDEX_NAME = 'JOURNAL-INDEX.json'
MAX_SEGMENTS = 4096
MAX_MEMBERS = 512
MAX_LOGICAL_FILES = 1_000_000
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SEGMENT_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 264 * 1024 * 1024
MAX_CENTRAL_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
MAX_INDEX_BYTES = 2_000_000
MAX_PLAN_FILES = 20_000
MAX_PLAN_BYTES = 512 * 1024 * 1024
DISK_RESERVE = 64 * 1024 * 1024
CHUNK_VERSION = 'private_beta_journal_lineage_chunk_v1'


def _fail():
    raise ValueError('recovery_archive_invalid')


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail()
        value[key] = item
    return value


def load_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError):
        _fail()


def logical_name(name):
    if not isinstance(name, str) or len(name.encode('utf-8')) > 1024:
        _fail()
    parts = PurePosixPath(name).parts
    if (not name.startswith('journal/') or len(parts) < 2 or len(parts) > 16
            or PurePosixPath(name).as_posix() != name or '\\' in name or ':' in name
            or any(part in ('', '.', '..') or part.endswith((' ', '.'))
                   or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                   or re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)
                   for part in parts)):
        _fail()
    return name


def record_valid(record):
    if (type(record) is not dict or set(record) != {'identity', 'bytes', 'sha256'}
            or type(record['bytes']) is not int or not 0 <= record['bytes'] <= MAX_FILE_BYTES
            or not isinstance(record['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
        _fail()
    identity = record['identity']
    if (type(identity) is not dict or set(identity) != {'path', 'device', 'inode'}
            or not isinstance(identity['path'], str) or not identity['path']
            or type(identity['device']) is not int or type(identity['inode']) is not int):
        _fail()


def _alias_check(name, aliases):
    parts = name.split('/')
    for length in range(1, len(parts) + 1):
        prefix = '/'.join(parts[:length])
        value = (prefix, length == len(parts))
        key = prefix.casefold()
        prior = aliases.get(key)
        if prior is not None and (prior != value or value[1]):
            _fail()
        aliases[key] = value


def inventory_summary(records):
    digest, count, size, previous, aliases = sha256(), 0, 0, None, {}
    for name, record in records:
        logical_name(name)
        record_valid(record)
        if previous is not None and name <= previous:
            _fail()
        previous = name
        _alias_check(name, aliases)
        count += 1
        size += record['bytes']
        if count > MAX_LOGICAL_FILES or size > MAX_TOTAL_BYTES:
            raise ValueError('recovery_archive_limit')
        digest.update(json.dumps([name, record], sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n')
    return dict(files=count, bytes=size, sha256=digest.hexdigest())


def source_records(files):
    from wahojobs.beta_recovery import _file, _hash
    from wahojobs.evidence_maintenance import database_identity
    return {name: dict(identity=database_identity(_file(path)), bytes=path.stat().st_size, sha256=_hash(path))
            for name, path in sorted(files.items()) if name.startswith('journal/')}


def _groups(records):
    group, size = {}, 0
    for name, record in records.items():
        record_valid(record)
        if group and (len(group) == MAX_MEMBERS or size + record['bytes'] > MAX_SEGMENT_BYTES):
            yield group
            group, size = {}, 0
        group[name] = record
        size += record['bytes']
    if group:
        yield group


def preflight(files, target, *, prepared=False):
    """Use current bytes, not format capacity, to reserve online/cold space."""
    journals = {name: path for name, path in files.items() if name.startswith('journal/')}
    plans = defaultdict(lambda: [0, 0])
    total, index_allowance = 0, 0
    for name, path in journals.items():
        logical_name(name)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError('recovery_archive_limit')
        total += size
        # Bound the actual encoded paths plus all fixed record fields/indentation.
        index_allowance += len(json.dumps([name, str(path)], ensure_ascii=True).encode('ascii')) + 512
        group = plans[name.split('/')[1]]
        group[0] += 1
        group[1] += size
    if len(journals) > MAX_LOGICAL_FILES or total > MAX_TOTAL_BYTES:
        raise ValueError('recovery_archive_limit')
    # Only actual plans are reconstructed. Opaque retained files remain evidence.
    scratch = 0
    for plan, (count, size) in plans.items():
        if 'journal/' + plan + '/plan.json' in journals:
            if count > MAX_PLAN_FILES or size > MAX_PLAN_BYTES:
                raise ValueError('recovery_archive_plan_limit')
            scratch = max(scratch, size)
    ordinary = sum(path.stat().st_size for name, path in files.items() if not name.startswith('journal/'))
    required = ordinary + DISK_RESERVE
    if not prepared:
        # Deflate worst case is below this margin. Index allowance uses the full
        # bounded per-member name/identity size rather than optimistic compression.
        required += total + total // 20 + index_allowance + scratch
    parent = Path(target)
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < required:
        raise ValueError('recovery_archive_insufficient_space')
    return required


def pack_journal(files, target, *, records=None):
    from wahojobs.beta_recovery import _json, _hash, _file
    records = source_records(files) if records is None else dict(sorted(records.items()))
    summary = inventory_summary(records.items())
    segments = {}
    for number, members in enumerate(_groups(records)):
        if number >= MAX_SEGMENTS:
            raise ValueError('recovery_archive_limit')
        name = f'journal-segments/{number:06d}.zip'
        output = Path(target) / name
        output.parent.mkdir(mode=0o700, exist_ok=True)
        raw_index = _json(dict(version=INDEX_VERSION, files=members))
        if len(raw_index) > MAX_INDEX_BYTES:
            raise ValueError('recovery_archive_index_limit')
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w+b') as stream:
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=False) as archive:
                for logical in members:
                    archive.write(_file(files[logical]), logical)
                info = zipfile.ZipInfo(INDEX_NAME)
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, raw_index)
            stream.flush()
            os.fsync(stream.fileno())
        segments[name] = dict(sha256=_hash(output), index_sha256=sha256(raw_index).hexdigest(),
            members=len(members), bytes=sum(r['bytes'] for r in members.values()),
            first=next(iter(members)), last=next(reversed(members)))
    result = dict(journal_segments=segments, journal_inventory=summary)
    # Validate the physical encoding and logical hashes before it can become a
    # trusted online proof. The caller separately validates copied plan chains.
    for _ in iter_journal(target, result):
        pass
    return result


def _structure(path):
    from wahojobs.beta_recovery import _file
    path = _file(path)
    size = path.stat().st_size
    if size < 22 or size > MAX_ARCHIVE_BYTES:
        _fail()
    with path.open('rb') as stream:
        stream.seek(size - 22)
        end = stream.read(22)
    signature, disk, central_disk, disk_count, count, length, offset, comment = struct.unpack('<4s4H2LH', end)
    if (signature != b'PK\x05\x06' or disk or central_disk or disk_count != count
            or not 1 <= count <= MAX_MEMBERS + 1 or comment or length > MAX_CENTRAL_BYTES
            or offset + length != size - 22 or count == 65535):
        _fail()
    return offset, count


def _zip_members(path):
    offset, count = _structure(path)
    archive = zipfile.ZipFile(path, 'r', allowZip64=False)
    try:
        infos = archive.infolist()
        if len(infos) != count or archive.comment:
            _fail()
        position, names = 0, set()
        with Path(path).open('rb') as stream:
            for info in infos:
                mode = info.external_attr >> 16
                if (info.filename in names or info.orig_filename != info.filename or info.is_dir()
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or info.flag_bits & ~0x800 or info.extra or info.comment
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.external_attr & 0x10
                        or info.header_offset != position or info.volume != 0
                        or info.extract_version >= 45):
                    _fail()
                names.add(info.filename)
                limit = MAX_INDEX_BYTES if info.filename == INDEX_NAME else MAX_FILE_BYTES
                if info.file_size > limit or info.compress_size > MAX_ARCHIVE_BYTES:
                    _fail()
                stream.seek(position)
                header = stream.read(30)
                if len(header) != 30:
                    _fail()
                fields = struct.unpack('<4s5H3L2H', header)
                sig, version, flags, method, _, _, crc, compressed, expanded, name_len, extra_len = fields
                encoded = stream.read(name_len)
                try:
                    local_name = encoded.decode('utf-8' if flags & 0x800 else 'cp437')
                except UnicodeError:
                    _fail()
                if (sig != b'PK\x03\x04' or version >= 45 or flags != info.flag_bits or method != info.compress_type
                        or crc != info.CRC or compressed != info.compress_size or expanded != info.file_size
                        or local_name != info.filename or extra_len):
                    _fail()
                position += 30 + name_len + compressed
        if position != offset:
            _fail()
        return archive, {info.filename: info for info in infos}
    except BaseException:
        archive.close()
        raise


def _copy_member(archive, info, expected, output=None):
    # ZipExtFile truncates output to the declared uncompressed size. Decode the
    # bounded compressed range ourselves so hidden expansion beyond that claimed
    # length cannot pass with a checksum of only the visible prefix.
    digest, length, crc = sha256(), 0, 0
    source = archive.fp
    source.seek(info.header_offset)
    header = source.read(30)
    name_length, extra_length = struct.unpack('<HH', header[26:30])
    source.seek(info.header_offset + 30 + name_length + extra_length)
    remaining = info.compress_size
    decoder = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    def consume(block):
        nonlocal length, crc
        length += len(block)
        if length > expected['bytes']:
            _fail()
        digest.update(block)
        crc = zlib.crc32(block, crc)
        if output is not None:
            output.write(block)
    try:
        while remaining:
            compressed = source.read(min(64 * 1024, remaining))
            if not compressed:
                _fail()
            remaining -= len(compressed)
            if decoder is None:
                consume(compressed)
                continue
            while compressed:
                block = decoder.decompress(compressed, min(1024 * 1024, expected['bytes'] - length + 1))
                compressed = decoder.unconsumed_tail
                consume(block)
                if decoder.eof:
                    if decoder.unused_data or compressed or remaining:
                        _fail()
                    break
        if decoder is not None and (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail):
            _fail()
    except zlib.error as error:
        raise ValueError('recovery_archive_invalid') from error
    if length != expected['bytes'] or digest.hexdigest() != expected['sha256'] or crc != info.CRC:
        _fail()


def _segment(snapshot, name, expected, *, verify_contents):
    from wahojobs.beta_recovery import _hash
    path = Path(snapshot) / name
    if (type(expected) is not dict or set(expected) != {'sha256', 'index_sha256', 'members', 'bytes', 'first', 'last'}
            or type(expected['members']) is not int or type(expected['bytes']) is not int
            or not 1 <= expected['members'] <= MAX_MEMBERS or not 0 <= expected['bytes'] <= MAX_SEGMENT_BYTES
            or any(not isinstance(expected[key], str) or not re.fullmatch('[0-9a-f]{64}', expected[key])
                   for key in ('sha256', 'index_sha256'))):
        _fail()
    _structure(path)  # Bound compressed input before hashing any content.
    if _hash(path) != expected.get('sha256'):
        _fail()
    archive, infos = _zip_members(path)
    try:
        if INDEX_NAME not in infos:
            _fail()
        buffer = io.BytesIO()
        _copy_member(archive, infos[INDEX_NAME],
            dict(bytes=infos[INDEX_NAME].file_size, sha256=expected['index_sha256']), buffer)
        raw = buffer.getvalue()
        if len(raw) > MAX_INDEX_BYTES or sha256(raw).hexdigest() != expected.get('index_sha256'):
            _fail()
        index = load_json(raw)
        if type(index) is not dict or index.get('version') != INDEX_VERSION or type(index.get('files')) is not dict:
            _fail()
        records = index['files']
        names = sorted(records)
        if (not 1 <= len(names) <= MAX_MEMBERS or set(infos) != {*names, INDEX_NAME}
                or expected.get('members') != len(names) or expected.get('first') != names[0]
                or expected.get('last') != names[-1]):
            _fail()
        size = 0
        for logical in names:
            logical_name(logical)
            record = records[logical]
            record_valid(record)
            if infos[logical].file_size != record['bytes']:
                _fail()
            size += record['bytes']
            if size > MAX_SEGMENT_BYTES:
                _fail()
            if verify_contents:
                _copy_member(archive, infos[logical], record)
        if size != expected.get('bytes'):
            _fail()
        return {name: records[name] for name in names}
    finally:
        archive.close()


def iter_journal(snapshot, manifest, *, verify_contents=True):
    segments = manifest.get('journal_segments')
    if type(segments) is not dict or len(segments) > MAX_SEGMENTS:
        _fail()
    def records():
        try:
            for number, name in enumerate(sorted(segments)):
                if name != f'journal-segments/{number:06d}.zip' or type(segments[name]) is not dict:
                    _fail()
                yield from _segment(snapshot, name, segments[name], verify_contents=verify_contents).items()
        except (zipfile.BadZipFile, RuntimeError, EOFError, struct.error, NotImplementedError) as error:
            raise ValueError('recovery_archive_invalid') from error
    # Inventory validation is incremental; callers must exhaust the iterator.
    digest, count, size, previous, aliases = sha256(), 0, 0, None, {}
    for name, record in records():
        if previous is not None and name <= previous:
            _fail()
        previous = name
        _alias_check(name, aliases)
        count += 1
        size += record['bytes']
        if count > MAX_LOGICAL_FILES or size > MAX_TOTAL_BYTES:
            _fail()
        digest.update(json.dumps([name, record], sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n')
        yield name, record
    if manifest.get('journal_inventory') != dict(files=count, bytes=size, sha256=digest.hexdigest()):
        _fail()


def extract_journal(snapshot, manifest, target, *, selected=None, _validated_segments=None):
    """Exclusive streaming writes only; never use extract/extractall."""
    for name in sorted(manifest['journal_segments']):
        if _validated_segments is not None:
            records = _validated_segments.get(name)
            if records is None:
                continue
        else:
            records = _segment(snapshot, name, manifest['journal_segments'][name], verify_contents=False)
        archive, infos = _zip_members(Path(snapshot) / name)
        try:
            for logical, record in records.items():
                if selected is not None and logical not in selected:
                    continue
                output = Path(target) / logical
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                from wahojobs.workos_authkit_staging import _require_no_reparse_components
                _require_no_reparse_components(output.parent)
                descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    _copy_member(archive, infos[logical], record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
        except (zipfile.BadZipFile, RuntimeError, EOFError) as error:
            raise ValueError('recovery_archive_invalid') from error
        finally:
            archive.close()


def validate_chains(snapshot, manifest):
    from wahojobs.beta_recovery import report
    plans, plan_segments, validated = defaultdict(dict), defaultdict(set), {}
    # Authenticate each segment once. A plan's reconstruction touches only its
    # segments; current retained history must not cost plans × all history bytes.
    for segment, expected in sorted(manifest['journal_segments'].items()):
        records = _segment(snapshot, segment, expected, verify_contents=False)
        validated[segment] = records
        for name, record in records.items():
            parts = name.split('/')
            if len(parts) >= 3:
                plans[parts[1]][name] = record
                plan_segments[parts[1]].add(segment)
    if inventory_summary((name, record) for segment in sorted(validated)
                         for name, record in validated[segment].items()) != manifest['journal_inventory']:
        _fail()
    for plan, records in plans.items():
        if f'journal/{plan}/plan.json' not in records:
            continue
        if len(records) > MAX_PLAN_FILES or sum(r['bytes'] for r in records.values()) > MAX_PLAN_BYTES:
            raise ValueError('recovery_archive_plan_limit')
        with tempfile.TemporaryDirectory(prefix='recovery-plan-', dir=Path(snapshot).parent) as directory:
            extract_journal(snapshot, manifest, Path(directory), selected=records,
                _validated_segments={segment: validated[segment] for segment in plan_segments[plan]})
            report(Path(directory) / 'journal', plan)
