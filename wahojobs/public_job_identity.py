"""Dormant permanent public-job identity and path authority.

This module owns no HTTP route.  Production route cutover and legacy backfill are
separate, reviewed milestones; callers must explicitly install migration 009 in
the database they intend to use.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import itertools
import json
import re
import secrets
import sqlite3
import unicodedata
from urllib.parse import unquote_to_bytes


PUBLIC_JOB_REGISTRY_FORMAT = "wahojobs-public-job-registry-v1"
PUBLIC_JOB_ID_PATTERN = re.compile(r"j[0-9a-f]{32}\Z")
NEW_PUBLIC_JOB_PATH_PATTERN = re.compile(
    r"/job/[a-z0-9]+(?:-[a-z0-9]+)*-j[0-9a-f]{32}\Z"
)
MAX_FROZEN_SLUG_CHARACTERS = 80
MAX_NEW_PUBLIC_JOB_PATH_BYTES = 119
MAX_PUBLIC_JOB_PATH_BYTES = 2_048
PUBLIC_JOB_ID_COLLISION_RETRIES = 8
MAX_PUBLIC_JOB_REGISTRY_ARTIFACT_BYTES = 32 * 1024 * 1024

_URL_PATH_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~!$&'()*+,;=:@/"
)
_SAVEPOINTS = itertools.count(1)


class PublicJobIdentityError(RuntimeError):
    """Base failure for the dormant public identity authority."""


class InvalidPublicJobIdentity(PublicJobIdentityError, ValueError):
    """An ID, path, timestamp, or registry payload violates its contract."""


class PublicJobIdentityInvariantError(PublicJobIdentityError):
    """Persisted public identity state is internally inconsistent."""


class PublicJobIdCollisionExhausted(PublicJobIdentityError):
    """The allocator exhausted its bounded collision retry budget."""


class StalePublicJobBinding(PublicJobIdentityError):
    """A compare-and-swap binding update used an obsolete version."""


@dataclass(frozen=True)
class PublicJobIdAllocator:
    """Explicit minting capability for the single authoritative allocator.

    Importing databases should never construct this capability.  They import the
    portable registry with :func:`import_public_job_registry` and establish only
    a database-local binding.
    """

    authority_name: str
    random_source: Callable[[int], bytes] = secrets.token_bytes

    def __post_init__(self) -> None:
        if not isinstance(self.authority_name, str) or not self.authority_name.strip():
            raise InvalidPublicJobIdentity("allocator authority_name is required")
        if not callable(self.random_source):
            raise InvalidPublicJobIdentity("allocator random_source must be callable")

    def mint(self) -> str:
        return generate_public_job_id(self.random_source)


@dataclass(frozen=True)
class PublicJobAllocation:
    public_job_id: str
    primary_path: str
    canonical_opportunity_id: int
    binding_version: int


@dataclass(frozen=True)
class PublicJobRouteDecision:
    kind: str
    public_job_id: str
    requested_path: str
    target_public_job_id: str | None = None
    primary_path: str | None = None
    canonical_opportunity_id: int | None = None
    location: str | None = None


@dataclass(frozen=True)
class PublicJobIdentityFinding:
    code: str
    public_job_id: str | None = None
    path: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class PublicJobRegistryArtifact:
    canonical_json: bytes
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.canonical_json) is not bytes
            or not 1 <= len(self.canonical_json) <= MAX_PUBLIC_JOB_REGISTRY_ARTIFACT_BYTES
            or type(self.sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
            or not hmac.compare_digest(
                hashlib.sha256(self.canonical_json).hexdigest(),
                self.sha256,
            )
        ):
            raise InvalidPublicJobIdentity("public job registry artifact is invalid")


@dataclass(frozen=True)
class PublicJobRegistryTransferVerification:
    sha256: str
    byte_size: int
    identity_count: int
    path_count: int
    binding_count: int


def generate_public_job_id(
    random_source: Callable[[int], bytes] | None = None,
) -> str:
    """Return ``j`` plus 32 lowercase hex characters from exactly 128 bits."""

    source = random_source or secrets.token_bytes
    raw = source(16)
    if type(raw) is not bytes or len(raw) != 16:
        raise InvalidPublicJobIdentity(
            "public job ID entropy source must return exactly 16 bytes"
        )
    public_job_id = "j" + raw.hex()
    require_public_job_id(public_job_id)
    return public_job_id


def require_public_job_id(value: object) -> str:
    if type(value) is not str or PUBLIC_JOB_ID_PATTERN.fullmatch(value) is None:
        raise InvalidPublicJobIdentity(
            "public_job_id must be j followed by 32 lowercase hexadecimal characters"
        )
    return value


def frozen_job_slug(company_slug: object, canonical_title: object) -> str:
    """Create the one-time readable slug from issuance inputs only."""

    company_words = _ascii_words(company_slug) or ("company",)
    title_words = _ascii_words(canonical_title)
    candidate_words = company_words + (title_words or ("job",))
    slug = _words_within_limit(candidate_words, MAX_FROZEN_SLUG_CHARACTERS)
    if not slug:
        slug = _words_within_limit(
            company_words + ("job",), MAX_FROZEN_SLUG_CHARACTERS
        )
    return slug or "company-job"


def new_public_job_path(
    company_slug: object,
    canonical_title: object,
    public_job_id: object,
) -> str:
    public_job_id = require_public_job_id(public_job_id)
    path = f"/job/{frozen_job_slug(company_slug, canonical_title)}-{public_job_id}"
    validate_new_public_job_path(path)
    return path


def normalized_public_job_path(path: object) -> str:
    path = validate_public_job_path(path)
    return path.lower()


def validate_public_job_path(path: object) -> str:
    """Validate one exact registered path without rewriting its legacy spelling."""

    if type(path) is not str:
        raise InvalidPublicJobIdentity("public job path must be text")
    try:
        encoded = path.encode("ascii")
    except UnicodeEncodeError as error:
        raise InvalidPublicJobIdentity(
            "public job path must use ASCII or percent-encoded bytes"
        ) from error
    if not 6 <= len(encoded) <= MAX_PUBLIC_JOB_PATH_BYTES:
        raise InvalidPublicJobIdentity("public job path length is outside 6..2048 bytes")
    if path[:5].lower() != "/job/" or path.lower() == "/job/":
        raise InvalidPublicJobIdentity("public job path must begin with /job/")
    if "%" in path:
        raise InvalidPublicJobIdentity(
            "percent-encoded public job paths require explicit future adjudication"
        )
    if "//" in path or "\\" in path or "?" in path or "#" in path:
        raise InvalidPublicJobIdentity("public job path contains an unsafe delimiter")

    index = 0
    while index < len(path):
        character = path[index]
        if character not in _URL_PATH_CHARACTERS:
            raise InvalidPublicJobIdentity("public job path contains an unsafe character")
        index += 1

    for raw_segment in path.split("/"):
        decoded_segment = unquote_to_bytes(raw_segment)
        if decoded_segment in {b".", b".."}:
            raise InvalidPublicJobIdentity("public job path contains a dot segment")
        if any(
            byte in decoded_segment
            for byte in (0, ord("/"), ord("\\"), ord("?"), ord("#"))
        ):
            raise InvalidPublicJobIdentity(
                "public job path contains an encoded unsafe delimiter"
            )
        if any(byte < 32 or byte == 127 for byte in decoded_segment):
            raise InvalidPublicJobIdentity("public job path contains a control byte")
    return path


def validate_new_public_job_path(path: object) -> str:
    path = validate_public_job_path(path)
    if len(path.encode("ascii")) > MAX_NEW_PUBLIC_JOB_PATH_BYTES:
        raise InvalidPublicJobIdentity("new public job path must be below 120 bytes")
    if NEW_PUBLIC_JOB_PATH_PATTERN.fullmatch(path) is None:
        raise InvalidPublicJobIdentity(
            "new public job path must use a frozen lowercase slug and immutable ID"
        )
    return path


def allocate_public_job(
    connection: sqlite3.Connection,
    *,
    allocator: PublicJobIdAllocator,
    company_slug: object,
    canonical_title: object,
    canonical_opportunity_id: object,
    primary_path: str | None = None,
    now: datetime | None = None,
    collision_retries: int = PUBLIC_JOB_ID_COLLISION_RETRIES,
) -> PublicJobAllocation:
    """Issue an identity, immutable primary path, and first local binding atomically."""

    if not isinstance(allocator, PublicJobIdAllocator):
        raise InvalidPublicJobIdentity(
            "allocation requires an explicit authoritative PublicJobIdAllocator"
        )
    canonical_opportunity_id = _positive_integer(
        canonical_opportunity_id, "canonical_opportunity_id"
    )
    if type(collision_retries) is not int or collision_retries < 1:
        raise InvalidPublicJobIdentity("collision_retries must be a positive integer")
    timestamp = _timestamp(now)
    legacy_primary = (
        validate_public_job_path(primary_path) if primary_path is not None else None
    )

    with _savepoint(connection):
        _require_canonical_opportunity(connection, canonical_opportunity_id)
        for _attempt in range(collision_retries):
            public_job_id = allocator.mint()
            path = legacy_primary or new_public_job_path(
                company_slug, canonical_title, public_job_id
            )
            try:
                connection.execute(
                    "INSERT INTO public_job_identities "
                    "(public_job_id, disposition, redirect_target_public_job_id, "
                    "created_at, updated_at) VALUES (?, 'serving', NULL, ?, ?)",
                    (public_job_id, timestamp, timestamp),
                )
            except sqlite3.IntegrityError:
                if _identity_exists(connection, public_job_id):
                    continue
                raise
            connection.execute(
                "INSERT INTO public_job_paths "
                "(path, normalized_path, public_job_id, path_role, created_at) "
                "VALUES (?, ?, ?, 'primary', ?)",
                (path, normalized_public_job_path(path), public_job_id, timestamp),
            )
            connection.execute(
                "INSERT INTO public_job_bindings "
                "(public_job_id, canonical_opportunity_id, binding_version, "
                "bound_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                (public_job_id, canonical_opportunity_id, timestamp, timestamp),
            )
            return PublicJobAllocation(
                public_job_id=public_job_id,
                primary_path=path,
                canonical_opportunity_id=canonical_opportunity_id,
                binding_version=1,
            )
    raise PublicJobIdCollisionExhausted(
        f"public job ID collision persisted for {collision_retries} attempts"
    )


def register_public_job_alias(
    connection: sqlite3.Connection,
    public_job_id: object,
    path: object,
    *,
    now: datetime | None = None,
) -> str:
    public_job_id = require_public_job_id(public_job_id)
    path = validate_public_job_path(path)
    timestamp = _timestamp(now)
    with _savepoint(connection):
        disposition = _require_identity(connection, public_job_id)[0]
        if disposition != "serving":
            raise PublicJobIdentityInvariantError(
                "aliases may be registered only before a serving identity is retired"
            )
        _require_primary_path(connection, public_job_id)
        connection.execute(
            "INSERT INTO public_job_paths "
            "(path, normalized_path, public_job_id, path_role, created_at) "
            "VALUES (?, ?, ?, 'alias', ?)",
            (path, normalized_public_job_path(path), public_job_id, timestamp),
        )
    return path


def bind_imported_public_job(
    connection: sqlite3.Connection,
    public_job_id: object,
    canonical_opportunity_id: object,
    *,
    now: datetime | None = None,
) -> int:
    public_job_id = require_public_job_id(public_job_id)
    canonical_opportunity_id = _positive_integer(
        canonical_opportunity_id, "canonical_opportunity_id"
    )
    timestamp = _timestamp(now)
    with _savepoint(connection):
        _require_canonical_opportunity(connection, canonical_opportunity_id)
        disposition = _require_identity(connection, public_job_id)[0]
        if disposition not in {"serving", "gone"}:
            raise PublicJobIdentityInvariantError(
                "redirect identities cannot receive local canonical bindings"
            )
        _require_primary_path(connection, public_job_id)
        connection.execute(
            "INSERT INTO public_job_bindings "
            "(public_job_id, canonical_opportunity_id, binding_version, "
            "bound_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            (public_job_id, canonical_opportunity_id, timestamp, timestamp),
        )
    return 1


def rebind_public_job(
    connection: sqlite3.Connection,
    public_job_id: object,
    *,
    expected_version: object,
    canonical_opportunity_id: object,
    now: datetime | None = None,
) -> int:
    """Explicitly change a local binding with compare-and-swap semantics."""

    public_job_id = require_public_job_id(public_job_id)
    expected_version = _positive_integer(expected_version, "expected_version")
    canonical_opportunity_id = _positive_integer(
        canonical_opportunity_id, "canonical_opportunity_id"
    )
    timestamp = _timestamp(now)
    with _savepoint(connection):
        _require_canonical_opportunity(connection, canonical_opportunity_id)
        cursor = connection.execute(
            "UPDATE public_job_bindings SET canonical_opportunity_id = ?, "
            "binding_version = binding_version + 1, updated_at = ? "
            "WHERE public_job_id = ? AND binding_version = ?",
            (
                canonical_opportunity_id,
                timestamp,
                public_job_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            current = connection.execute(
                "SELECT binding_version FROM public_job_bindings "
                "WHERE public_job_id = ?",
                (public_job_id,),
            ).fetchone()
            if current is None:
                raise PublicJobIdentityInvariantError(
                    "public job identity has no local canonical binding"
                )
            raise StalePublicJobBinding(
                f"expected binding version {expected_version}; current is {current[0]}"
            )
    return expected_version + 1


def merge_public_jobs(
    connection: sqlite3.Connection,
    loser_public_job_id: object,
    survivor_public_job_id: object,
    *,
    now: datetime | None = None,
) -> None:
    """Retire one identity to a direct, one-hop redirect to a serving survivor."""

    loser = require_public_job_id(loser_public_job_id)
    survivor = require_public_job_id(survivor_public_job_id)
    if loser == survivor:
        raise InvalidPublicJobIdentity("a public job identity cannot merge into itself")
    timestamp = _timestamp(now)
    with _savepoint(connection):
        loser_state = _require_identity(connection, loser)[0]
        survivor_state = _require_identity(connection, survivor)[0]
        if loser_state != "serving" or survivor_state != "serving":
            raise PublicJobIdentityInvariantError(
                "both merge participants must currently be serving identities"
            )
        _require_primary_path(connection, loser)
        _require_primary_path(connection, survivor)
        locally_bound = {
            row[0]
            for row in connection.execute(
                "SELECT public_job_id FROM public_job_bindings "
                "WHERE public_job_id IN (?, ?)",
                (loser, survivor),
            )
        }
        if locally_bound != {loser, survivor}:
            raise PublicJobIdentityInvariantError(
                "both merge participants require database-local canonical bindings"
            )

        connection.execute(
            "UPDATE public_job_identities SET redirect_target_public_job_id = ?, "
            "updated_at = ? WHERE disposition = 'redirect' "
            "AND redirect_target_public_job_id = ?",
            (survivor, timestamp, loser),
        )
        connection.execute(
            "UPDATE public_job_identities SET disposition = 'redirect', "
            "redirect_target_public_job_id = ?, updated_at = ? "
            "WHERE public_job_id = ?",
            (survivor, timestamp, loser),
        )
        if connection.execute(
            "SELECT 1 FROM public_job_bindings WHERE public_job_id = ?",
            (loser,),
        ).fetchone() is not None:
            raise PublicJobIdentityInvariantError(
                "merge transition did not retire the loser binding"
            )


def mark_public_job_gone(
    connection: sqlite3.Connection,
    public_job_id: object,
    *,
    now: datetime | None = None,
) -> None:
    public_job_id = require_public_job_id(public_job_id)
    timestamp = _timestamp(now)
    with _savepoint(connection):
        if _require_identity(connection, public_job_id)[0] != "serving":
            raise PublicJobIdentityInvariantError(
                "only a serving identity can become gone"
            )
        _require_primary_path(connection, public_job_id)
        if connection.execute(
            "SELECT 1 FROM public_job_bindings WHERE public_job_id = ?",
            (public_job_id,),
        ).fetchone() is None:
            raise PublicJobIdentityInvariantError(
                "a gone transition requires the existing local binding"
            )
        if connection.execute(
            "SELECT 1 FROM public_job_identities "
            "WHERE disposition = 'redirect' AND redirect_target_public_job_id = ?",
            (public_job_id,),
        ).fetchone() is not None:
            raise PublicJobIdentityInvariantError(
                "an identity with incoming redirects requires explicit adjudication"
            )
        connection.execute(
            "UPDATE public_job_identities SET disposition = 'gone', "
            "redirect_target_public_job_id = NULL, updated_at = ? "
            "WHERE public_job_id = ?",
            (timestamp, public_job_id),
        )


def restore_public_job(
    connection: sqlite3.Connection,
    public_job_id: object,
    *,
    now: datetime | None = None,
) -> None:
    public_job_id = require_public_job_id(public_job_id)
    timestamp = _timestamp(now)
    with _savepoint(connection):
        if _require_identity(connection, public_job_id)[0] != "gone":
            raise PublicJobIdentityInvariantError("only a gone identity can be restored")
        _require_primary_path(connection, public_job_id)
        if connection.execute(
            "SELECT 1 FROM public_job_bindings WHERE public_job_id = ?",
            (public_job_id,),
        ).fetchone() is None:
            raise PublicJobIdentityInvariantError(
                "a restored identity requires its existing local binding"
            )
        connection.execute(
            "UPDATE public_job_identities SET disposition = 'serving', "
            "redirect_target_public_job_id = NULL, updated_at = ? "
            "WHERE public_job_id = ?",
            (timestamp, public_job_id),
        )


def resolve_public_job_path(
    connection: sqlite3.Connection,
    path: object,
) -> PublicJobRouteDecision | None:
    """Resolve only a complete registered path; never infer ownership from an ID."""

    try:
        path = validate_public_job_path(path)
    except InvalidPublicJobIdentity:
        return None
    row = connection.execute(
        "SELECT path.path, path.path_role, path.public_job_id, "
        "identity.disposition, identity.redirect_target_public_job_id "
        "FROM public_job_paths path "
        "JOIN public_job_identities identity "
        "ON identity.public_job_id = path.public_job_id "
        "WHERE path.path = ?",
        (path,),
    ).fetchone()
    exact = row is not None
    if row is None:
        row = connection.execute(
            "SELECT path.path, path.path_role, path.public_job_id, "
            "identity.disposition, identity.redirect_target_public_job_id "
            "FROM public_job_paths path "
            "JOIN public_job_identities identity "
            "ON identity.public_job_id = path.public_job_id "
            "WHERE path.normalized_path = ?",
            (normalized_public_job_path(path),),
        ).fetchone()
    if row is None:
        return None

    registered_path, path_role, public_job_id, disposition, redirect_target = row
    if disposition == "gone":
        return PublicJobRouteDecision(
            kind="gone",
            public_job_id=public_job_id,
            requested_path=path,
            primary_path=_require_primary_path(connection, public_job_id),
        )

    effective_public_job_id = public_job_id
    if disposition == "redirect":
        target = _require_identity(connection, redirect_target)
        if target[0] != "serving":
            raise PublicJobIdentityInvariantError(
                "redirect target is not a serving identity"
            )
        effective_public_job_id = redirect_target
    elif disposition != "serving":
        raise PublicJobIdentityInvariantError(
            f"unknown public job disposition {disposition!r}"
        )

    primary_path = _require_primary_path(connection, effective_public_job_id)
    binding = connection.execute(
        "SELECT canonical_opportunity_id FROM public_job_bindings "
        "WHERE public_job_id = ?",
        (effective_public_job_id,),
    ).fetchone()
    if binding is None:
        return PublicJobRouteDecision(
            kind="unbound",
            public_job_id=public_job_id,
            requested_path=path,
            target_public_job_id=(
                effective_public_job_id
                if effective_public_job_id != public_job_id
                else None
            ),
            primary_path=primary_path,
        )
    if (
        disposition == "redirect"
        or path_role != "primary"
        or not exact
        or registered_path != primary_path
    ):
        return PublicJobRouteDecision(
            kind="redirect",
            public_job_id=public_job_id,
            requested_path=path,
            target_public_job_id=effective_public_job_id,
            primary_path=primary_path,
            canonical_opportunity_id=int(binding[0]),
            location=primary_path,
        )
    return PublicJobRouteDecision(
        kind="serve",
        public_job_id=public_job_id,
        requested_path=path,
        primary_path=primary_path,
        canonical_opportunity_id=int(binding[0]),
    )


def primary_public_job_path_for_canonical(
    connection: sqlite3.Connection,
    canonical_opportunity_id: object,
) -> str | None:
    canonical_opportunity_id = _positive_integer(
        canonical_opportunity_id, "canonical_opportunity_id"
    )
    row = connection.execute(
        "SELECT path.path FROM public_job_bindings binding "
        "JOIN public_job_identities identity "
        "ON identity.public_job_id = binding.public_job_id "
        "JOIN public_job_paths path ON path.public_job_id = binding.public_job_id "
        "AND path.path_role = 'primary' "
        "WHERE binding.canonical_opportunity_id = ? "
        "AND identity.disposition = 'serving'",
        (canonical_opportunity_id,),
    ).fetchone()
    return row[0] if row is not None else None


def resolve_public_job_canonical(
    connection: sqlite3.Connection,
    canonical_opportunity_id: object,
) -> PublicJobRouteDecision | None:
    """Resolve a locally bound serving/gone identity by canonical row number."""

    canonical_opportunity_id = _positive_integer(
        canonical_opportunity_id, "canonical_opportunity_id"
    )
    row = connection.execute(
        "SELECT path.path FROM public_job_bindings binding "
        "JOIN public_job_identities identity "
        "ON identity.public_job_id = binding.public_job_id "
        "JOIN public_job_paths path ON path.public_job_id = binding.public_job_id "
        "AND path.path_role = 'primary' "
        "WHERE binding.canonical_opportunity_id = ? "
        "AND identity.disposition IN ('serving', 'gone')",
        (canonical_opportunity_id,),
    ).fetchone()
    return resolve_public_job_path(connection, row[0]) if row is not None else None


def export_public_job_registry(connection: sqlite3.Connection) -> dict:
    """Export only portable identity/path state; local bindings are excluded."""

    identities = [
        {
            "public_job_id": row[0],
            "disposition": row[1],
            "redirect_target_public_job_id": row[2],
            "created_at": row[3],
            "updated_at": row[4],
        }
        for row in connection.execute(
            "SELECT public_job_id, disposition, redirect_target_public_job_id, "
            "created_at, updated_at FROM public_job_identities "
            "ORDER BY public_job_id"
        )
    ]
    paths = [
        {
            "path": row[0],
            "normalized_path": row[1],
            "public_job_id": row[2],
            "path_role": row[3],
            "created_at": row[4],
        }
        for row in connection.execute(
            "SELECT path, normalized_path, public_job_id, path_role, created_at "
            "FROM public_job_paths ORDER BY normalized_path, path"
        )
    ]
    return {
        "format": PUBLIC_JOB_REGISTRY_FORMAT,
        "identities": identities,
        "paths": paths,
    }


def canonical_public_job_registry_json(payload: object) -> bytes:
    """Return the unique ASCII JSON representation of one portable registry."""

    identities, paths = _validated_registry_payload(payload)
    canonical = {
        "format": PUBLIC_JOB_REGISTRY_FORMAT,
        "identities": sorted(identities, key=lambda item: item["public_job_id"]),
        "paths": sorted(
            paths,
            key=lambda item: (item["normalized_path"], item["path"]),
        ),
    }
    encoded = (
        json.dumps(
            canonical,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    if len(encoded) > MAX_PUBLIC_JOB_REGISTRY_ARTIFACT_BYTES:
        raise InvalidPublicJobIdentity("public job registry artifact is too large")
    return encoded


def public_job_registry_artifact(payload: object) -> PublicJobRegistryArtifact:
    canonical_json = canonical_public_job_registry_json(payload)
    return PublicJobRegistryArtifact(
        canonical_json=canonical_json,
        sha256=hashlib.sha256(canonical_json).hexdigest(),
    )


def export_public_job_registry_artifact(
    connection: sqlite3.Connection,
) -> PublicJobRegistryArtifact:
    return public_job_registry_artifact(export_public_job_registry(connection))


def decode_public_job_registry_artifact(
    artifact: PublicJobRegistryArtifact,
) -> dict:
    """Verify digest, strict JSON, and canonical bytes before import."""

    if type(artifact) is not PublicJobRegistryArtifact:
        raise InvalidPublicJobIdentity("public job registry artifact is required")
    canonical_json = artifact.canonical_json
    sha256 = artifact.sha256
    if (
        type(canonical_json) is not bytes
        or not 1 <= len(canonical_json) <= MAX_PUBLIC_JOB_REGISTRY_ARTIFACT_BYTES
        or type(sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or not hmac.compare_digest(
            hashlib.sha256(canonical_json).hexdigest(),
            sha256,
        )
    ):
        raise InvalidPublicJobIdentity(
            "public job registry artifact digest is invalid"
        )
    try:
        payload = json.loads(
            canonical_json.decode("ascii", "strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, TypeError):
        raise InvalidPublicJobIdentity(
            "public job registry artifact JSON is invalid"
        ) from None
    if canonical_public_job_registry_json(payload) != canonical_json:
        raise InvalidPublicJobIdentity(
            "public job registry artifact is not canonically serialized"
        )
    return payload


def import_public_job_registry_artifact(
    connection: sqlite3.Connection,
    artifact: PublicJobRegistryArtifact,
) -> None:
    import_public_job_registry(
        connection,
        decode_public_job_registry_artifact(artifact),
    )


def import_public_job_registry(
    connection: sqlite3.Connection,
    payload: object,
) -> None:
    """Import portable authority into an empty registry without minting IDs."""

    identities, paths = _validated_registry_payload(payload)
    with _savepoint(connection):
        occupied = connection.execute(
            "SELECT (SELECT COUNT(*) FROM public_job_identities) + "
            "(SELECT COUNT(*) FROM public_job_paths) + "
            "(SELECT COUNT(*) FROM public_job_bindings)"
        ).fetchone()[0]
        if occupied:
            raise PublicJobIdentityInvariantError(
                "portable registry import requires empty identity authorities"
            )
        for record in sorted(
            identities,
            key=lambda item: (item["disposition"] == "redirect", item["public_job_id"]),
        ):
            connection.execute(
                "INSERT INTO public_job_identities "
                "(public_job_id, disposition, redirect_target_public_job_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    record["public_job_id"],
                    record["disposition"],
                    record["redirect_target_public_job_id"],
                    record["created_at"],
                    record["updated_at"],
                ),
            )
        for record in paths:
            connection.execute(
                "INSERT INTO public_job_paths "
                "(path, normalized_path, public_job_id, path_role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record["path"],
                    record["normalized_path"],
                    record["public_job_id"],
                    record["path_role"],
                    record["created_at"],
                ),
            )


def verify_disposable_public_job_registry_transfer(
    source_connection: sqlite3.Connection,
    target_connection: sqlite3.Connection,
    *,
    local_bindings: Mapping[str, int],
    now: datetime | None = None,
) -> PublicJobRegistryTransferVerification:
    """Import and locally rebind an exact registry in a disposable target DB."""

    if (
        type(source_connection) is not sqlite3.Connection
        or type(target_connection) is not sqlite3.Connection
        or source_connection is target_connection
        or not isinstance(local_bindings, Mapping)
    ):
        raise InvalidPublicJobIdentity(
            "disposable registry verification inputs are invalid"
        )
    from wahojobs.public_job_identity_schema import (
        attest_public_job_identity_schema,
    )

    if (
        attest_public_job_identity_schema(source_connection)["state"]
        != "correctly_installed"
        or attest_public_job_identity_schema(target_connection)["state"]
        != "correctly_installed"
    ):
        raise PublicJobIdentityInvariantError(
            "disposable registry verification requires exact M009 databases"
        )
    assert_public_job_identity_consistent(source_connection)
    artifact = export_public_job_registry_artifact(source_connection)
    payload = decode_public_job_registry_artifact(artifact)
    bindable = {
        item["public_job_id"]
        for item in payload["identities"]
        if item["disposition"] in {"serving", "gone"}
    }
    normalized_bindings: dict[str, int] = {}
    for raw_public_job_id, raw_canonical_id in local_bindings.items():
        public_job_id = require_public_job_id(raw_public_job_id)
        if public_job_id in normalized_bindings:
            raise InvalidPublicJobIdentity("local binding repeats a public job ID")
        normalized_bindings[public_job_id] = _positive_integer(
            raw_canonical_id,
            "canonical_opportunity_id",
        )
    if set(normalized_bindings) != bindable:
        raise InvalidPublicJobIdentity(
            "local bindings must cover every bindable imported identity exactly"
        )

    with _savepoint(target_connection):
        import_public_job_registry_artifact(target_connection, artifact)
        if target_connection.execute(
            "SELECT COUNT(*) FROM public_job_bindings"
        ).fetchone()[0] != 0:
            raise PublicJobIdentityInvariantError(
                "portable registry import unexpectedly included local bindings"
            )
        for public_job_id in sorted(normalized_bindings):
            bind_imported_public_job(
                target_connection,
                public_job_id,
                normalized_bindings[public_job_id],
                now=now,
            )
        assert_public_job_identity_consistent(target_connection)
        imported_artifact = export_public_job_registry_artifact(target_connection)
        if imported_artifact != artifact:
            raise PublicJobIdentityInvariantError(
                "portable registry changed during disposable import and rebinding"
            )

    return PublicJobRegistryTransferVerification(
        sha256=artifact.sha256,
        byte_size=len(artifact.canonical_json),
        identity_count=len(payload["identities"]),
        path_count=len(payload["paths"]),
        binding_count=len(normalized_bindings),
    )


def reconcile_public_job_identity(
    connection: sqlite3.Connection,
) -> tuple[PublicJobIdentityFinding, ...]:
    findings: list[PublicJobIdentityFinding] = []
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        findings.append(PublicJobIdentityFinding("foreign_keys_disabled"))
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    required_tables = {
        "public_job_identities",
        "public_job_paths",
        "public_job_bindings",
    }
    for table in sorted(required_tables - tables):
        findings.append(
            PublicJobIdentityFinding("required_table_missing", detail=table)
        )
    if findings:
        return tuple(findings)

    schema_objects = {
        (row[0], row[1])
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('index', 'trigger')"
        )
    }
    required_schema_objects = {
        ("index", "idx_public_job_paths_one_primary"),
        ("index", "idx_public_job_paths_owner_role"),
        ("index", "idx_public_job_identities_redirect_target"),
        ("trigger", "trg_public_job_identities_immutable_identity"),
        ("trigger", "trg_public_job_identities_update_guard"),
        ("trigger", "trg_public_job_identities_redirect_insert_guard"),
        ("trigger", "trg_public_job_identities_redirect_update_guard"),
        ("trigger", "trg_public_job_identities_incoming_redirect_guard"),
        ("trigger", "trg_public_job_identities_no_replace"),
        ("trigger", "trg_public_job_identities_retire_binding"),
        ("trigger", "trg_public_job_identities_no_delete"),
        ("trigger", "trg_public_job_paths_no_update"),
        ("trigger", "trg_public_job_paths_no_replace"),
        ("trigger", "trg_public_job_paths_no_delete"),
        ("trigger", "trg_public_job_bindings_insert_guard"),
        ("trigger", "trg_public_job_bindings_no_replace"),
        ("trigger", "trg_public_job_bindings_update_guard"),
        ("trigger", "trg_public_job_bindings_no_delete"),
    }
    for object_type, name in sorted(required_schema_objects - schema_objects):
        findings.append(
            PublicJobIdentityFinding(
                "required_schema_object_missing",
                detail=f"{object_type}:{name}",
            )
        )

    identity_rows = list(
        connection.execute(
            "SELECT public_job_id, disposition, redirect_target_public_job_id "
            "FROM public_job_identities ORDER BY public_job_id"
        )
    )
    identity_counts = Counter(row[0] for row in identity_rows)
    identities = {row[0]: (row[1], row[2]) for row in identity_rows}
    path_rows = list(
        connection.execute(
            "SELECT path, normalized_path, public_job_id, path_role "
            "FROM public_job_paths ORDER BY normalized_path, path"
        )
    )
    binding_rows = list(
        connection.execute(
            "SELECT public_job_id, canonical_opportunity_id, binding_version "
            "FROM public_job_bindings"
        )
    )
    bindings = {
        row[0]: (row[1], row[2])
        for row in binding_rows
    }
    primary_counts = Counter(
        row[2] for row in path_rows if row[3] == "primary"
    )
    normalized_path_counts = Counter(row[1] for row in path_rows)
    exact_path_counts = Counter(row[0] for row in path_rows)
    binding_owner_counts = Counter(row[0] for row in binding_rows)
    canonical_binding_counts = Counter(row[1] for row in binding_rows)

    for public_job_id, count in identity_counts.items():
        if count > 1:
            findings.append(
                PublicJobIdentityFinding(
                    "duplicate_public_job_id",
                    public_job_id,
                    detail=str(count),
                )
            )
    for path, count in exact_path_counts.items():
        if count > 1:
            findings.append(
                PublicJobIdentityFinding(
                    "duplicate_exact_path", path=path, detail=str(count)
                )
            )
    for normalized_path, count in normalized_path_counts.items():
        if count > 1:
            findings.append(
                PublicJobIdentityFinding(
                    "normalized_path_collision",
                    path=normalized_path,
                    detail=str(count),
                )
            )
    for public_job_id, count in binding_owner_counts.items():
        if count > 1:
            findings.append(
                PublicJobIdentityFinding(
                    "duplicate_binding_owner",
                    public_job_id,
                    detail=str(count),
                )
            )
    for canonical_id, count in canonical_binding_counts.items():
        if count > 1:
            findings.append(
                PublicJobIdentityFinding(
                    "duplicate_canonical_binding",
                    detail=f"{canonical_id}:{count}",
                )
            )

    for public_job_id, (disposition, target) in identities.items():
        try:
            require_public_job_id(public_job_id)
        except InvalidPublicJobIdentity as error:
            findings.append(
                PublicJobIdentityFinding(
                    "invalid_public_job_id", public_job_id, detail=str(error)
                )
            )
        if primary_counts[public_job_id] != 1:
            findings.append(
                PublicJobIdentityFinding(
                    "primary_path_count",
                    public_job_id,
                    detail=str(primary_counts[public_job_id]),
                )
            )
        if disposition == "serving":
            if target is not None:
                findings.append(
                    PublicJobIdentityFinding(
                        "serving_identity_has_redirect_target", public_job_id
                    )
                )
            if public_job_id not in bindings:
                findings.append(
                    PublicJobIdentityFinding(
                        "serving_identity_unbound", public_job_id
                    )
                )
        elif disposition == "redirect":
            target_state = identities.get(target)
            if target == public_job_id:
                findings.append(
                    PublicJobIdentityFinding("redirect_self_target", public_job_id)
                )
            elif target_state is None:
                findings.append(
                    PublicJobIdentityFinding("redirect_target_missing", public_job_id)
                )
            elif target_state[0] != "serving":
                findings.append(
                    PublicJobIdentityFinding(
                        "redirect_target_not_serving", public_job_id, detail=target
                    )
                )
            if public_job_id in bindings:
                findings.append(
                    PublicJobIdentityFinding(
                        "redirect_identity_has_binding", public_job_id
                    )
                )
        elif disposition == "gone":
            if target is not None:
                findings.append(
                    PublicJobIdentityFinding(
                        "gone_identity_has_redirect_target", public_job_id
                    )
                )
        else:
            findings.append(
                PublicJobIdentityFinding(
                    "invalid_disposition", public_job_id, detail=str(disposition)
                )
            )

    for path, normalized_path, public_job_id, path_role in path_rows:
        if public_job_id not in identities:
            findings.append(
                PublicJobIdentityFinding(
                    "path_owner_missing", public_job_id, path=path
                )
            )
        try:
            expected = normalized_public_job_path(path)
        except InvalidPublicJobIdentity as error:
            findings.append(
                PublicJobIdentityFinding(
                    "invalid_public_job_path",
                    public_job_id,
                    path,
                    str(error),
                )
            )
        else:
            if normalized_path != expected:
                findings.append(
                    PublicJobIdentityFinding(
                        "normalized_path_mismatch",
                        public_job_id,
                        path,
                        normalized_path,
                    )
                )
        if path_role not in {"primary", "alias"}:
            findings.append(
                PublicJobIdentityFinding(
                    "invalid_path_role", public_job_id, path, path_role
                )
            )

    for public_job_id, (canonical_id, version) in bindings.items():
        if public_job_id not in identities:
            findings.append(
                PublicJobIdentityFinding("binding_owner_missing", public_job_id)
            )
        if type(version) is not int or version < 1:
            findings.append(
                PublicJobIdentityFinding(
                    "invalid_binding_version", public_job_id, detail=str(version)
                )
            )
        if connection.execute(
            "SELECT 1 FROM canonical_opportunities WHERE id = ?", (canonical_id,)
        ).fetchone() is None:
            findings.append(
                PublicJobIdentityFinding(
                    "binding_canonical_missing",
                    public_job_id,
                    detail=str(canonical_id),
                )
            )
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                item.code,
                item.public_job_id or "",
                item.path or "",
                item.detail or "",
            ),
        )
    )


def assert_public_job_identity_consistent(connection: sqlite3.Connection) -> None:
    findings = reconcile_public_job_identity(connection)
    if findings:
        summary = ", ".join(
            f"{item.code}:{item.public_job_id or item.path or item.detail or '-'}"
            for item in findings
        )
        raise PublicJobIdentityInvariantError(summary)


def _validated_registry_payload(payload: object) -> tuple[list[dict], list[dict]]:
    if (
        not isinstance(payload, Mapping)
        or payload.get("format") != PUBLIC_JOB_REGISTRY_FORMAT
        or set(payload) != {"format", "identities", "paths"}
    ):
        raise InvalidPublicJobIdentity("unsupported public job registry format")
    raw_identities = payload.get("identities")
    raw_paths = payload.get("paths")
    if not isinstance(raw_identities, Sequence) or isinstance(
        raw_identities, (str, bytes)
    ):
        raise InvalidPublicJobIdentity("registry identities must be a sequence")
    if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
        raise InvalidPublicJobIdentity("registry paths must be a sequence")

    identities: list[dict] = []
    identity_ids: set[str] = set()
    for raw in raw_identities:
        if not isinstance(raw, Mapping) or set(raw) != {
            "public_job_id",
            "disposition",
            "redirect_target_public_job_id",
            "created_at",
            "updated_at",
        }:
            raise InvalidPublicJobIdentity("registry identity must be an object")
        public_job_id = require_public_job_id(raw.get("public_job_id"))
        if public_job_id in identity_ids:
            raise InvalidPublicJobIdentity("registry repeats a public job identity")
        identity_ids.add(public_job_id)
        disposition = raw.get("disposition")
        target = raw.get("redirect_target_public_job_id")
        if disposition not in {"serving", "redirect", "gone"}:
            raise InvalidPublicJobIdentity("registry identity disposition is invalid")
        if disposition == "redirect":
            target = require_public_job_id(target)
            if target == public_job_id:
                raise InvalidPublicJobIdentity("registry contains a self redirect")
        elif target is not None:
            raise InvalidPublicJobIdentity(
                "non-redirect registry identity has a redirect target"
            )
        created_at = _require_timestamp_text(raw.get("created_at"))
        updated_at = _require_timestamp_text(raw.get("updated_at"))
        if updated_at < created_at:
            raise InvalidPublicJobIdentity("registry identity time moved backward")
        identities.append(
            {
                "public_job_id": public_job_id,
                "disposition": disposition,
                "redirect_target_public_job_id": target,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    identity_states = {
        item["public_job_id"]: item["disposition"] for item in identities
    }
    for item in identities:
        if item["disposition"] == "redirect" and identity_states.get(
            item["redirect_target_public_job_id"]
        ) != "serving":
            raise InvalidPublicJobIdentity(
                "registry redirect must target a serving identity directly"
            )

    paths: list[dict] = []
    exact_paths: set[str] = set()
    normalized_paths: set[str] = set()
    primary_counts: Counter[str] = Counter()
    for raw in raw_paths:
        if not isinstance(raw, Mapping) or set(raw) != {
            "path",
            "normalized_path",
            "public_job_id",
            "path_role",
            "created_at",
        }:
            raise InvalidPublicJobIdentity("registry path must be an object")
        path = validate_public_job_path(raw.get("path"))
        normalized = normalized_public_job_path(path)
        if raw.get("normalized_path") != normalized:
            raise InvalidPublicJobIdentity("registry normalized path is not authoritative")
        if path in exact_paths or normalized in normalized_paths:
            raise InvalidPublicJobIdentity("registry contains a path ownership collision")
        exact_paths.add(path)
        normalized_paths.add(normalized)
        public_job_id = require_public_job_id(raw.get("public_job_id"))
        if public_job_id not in identity_ids:
            raise InvalidPublicJobIdentity("registry path owner is missing")
        path_role = raw.get("path_role")
        if path_role not in {"primary", "alias"}:
            raise InvalidPublicJobIdentity("registry path role is invalid")
        if path_role == "primary":
            primary_counts[public_job_id] += 1
        paths.append(
            {
                "path": path,
                "normalized_path": normalized,
                "public_job_id": public_job_id,
                "path_role": path_role,
                "created_at": _require_timestamp_text(raw.get("created_at")),
            }
        )
    if any(primary_counts[public_job_id] != 1 for public_job_id in identity_ids):
        raise InvalidPublicJobIdentity(
            "every imported public identity must own exactly one primary path"
        )
    return identities, paths


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate_or_invalid_json_key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("invalid_json_constant")


def _ascii_words(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return tuple(re.findall(r"[a-z0-9]+", folded.lower()))


def _words_within_limit(words: Sequence[str], limit: int) -> str:
    selected: list[str] = []
    length = 0
    for word in words:
        candidate_length = length + (1 if selected else 0) + len(word)
        if candidate_length > limit:
            break
        selected.append(word)
        length = candidate_length
    return "-".join(selected)


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise InvalidPublicJobIdentity(f"{name} must be a positive integer")
    return value


def _timestamp(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if (
        type(current) is not datetime
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise InvalidPublicJobIdentity("now must be a timezone-aware datetime")
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _require_timestamp_text(value: object) -> str:
    if type(value) is not str or len(value) != 25 or not value.endswith("+00:00"):
        raise InvalidPublicJobIdentity("registry timestamp must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidPublicJobIdentity("registry timestamp is invalid") from error
    if _timestamp(parsed) != value:
        raise InvalidPublicJobIdentity("registry timestamp must omit subsecond data")
    return value


def _require_canonical_opportunity(
    connection: sqlite3.Connection,
    canonical_opportunity_id: int,
) -> None:
    if connection.execute(
        "SELECT 1 FROM canonical_opportunities WHERE id = ?",
        (canonical_opportunity_id,),
    ).fetchone() is None:
        raise InvalidPublicJobIdentity(
            f"canonical opportunity {canonical_opportunity_id} does not exist locally"
        )


def _identity_exists(connection: sqlite3.Connection, public_job_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM public_job_identities WHERE public_job_id = ?",
            (public_job_id,),
        ).fetchone()
        is not None
    )


def _require_identity(
    connection: sqlite3.Connection,
    public_job_id: str,
) -> tuple[str, str | None]:
    row = connection.execute(
        "SELECT disposition, redirect_target_public_job_id "
        "FROM public_job_identities WHERE public_job_id = ?",
        (public_job_id,),
    ).fetchone()
    if row is None:
        raise PublicJobIdentityInvariantError(
            f"public job identity {public_job_id} is not registered"
        )
    return row[0], row[1]


def _require_primary_path(
    connection: sqlite3.Connection,
    public_job_id: str,
) -> str:
    rows = connection.execute(
        "SELECT path FROM public_job_paths "
        "WHERE public_job_id = ? AND path_role = 'primary'",
        (public_job_id,),
    ).fetchall()
    if len(rows) != 1:
        raise PublicJobIdentityInvariantError(
            f"public job identity {public_job_id} must own exactly one primary path"
        )
    return rows[0][0]


@contextmanager
def _savepoint(connection: sqlite3.Connection):
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise PublicJobIdentityInvariantError(
            "public job identity mutations require SQLite foreign keys"
        )
    name = f"public_job_identity_{next(_SAVEPOINTS)}"
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        connection.execute(f"RELEASE SAVEPOINT {name}")
