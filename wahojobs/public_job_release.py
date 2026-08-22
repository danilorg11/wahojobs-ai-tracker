"""Exact Preview and production publication contracts for public job routes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from wahojobs import public_job_identity


PUBLIC_JOB_PREVIEW_BINDINGS_FORMAT = "wahojobs-public-job-preview-bindings-v1"
PUBLIC_JOB_PREVIEW_RELEASE_FORMAT = "wahojobs-public-job-preview-release-v1"
PUBLIC_JOB_PRODUCTION_BINDINGS_FORMAT = (
    "wahojobs-public-job-production-bindings-v1"
)
PUBLIC_JOB_PRODUCTION_RELEASE_FORMAT = "wahojobs-public-job-production-release-v1"
KARL_PUBLIC_JOB_PATH = "/job/oneforma-karl-llm-1"
KARL_PUBLIC_JOB_ID = "j7b8550e11700c9b26ac68deb753e1f82"
KARL_CANONICAL_KEY = "oneforma::177080"
HANDSHAKE_CANARY_PUBLIC_JOB_PATH = (
    "/job/handshake-ai-evaluation-specialist-"
    "j125e8ced56da8007c92ab964f58f9f0f"
)
HANDSHAKE_CANARY_PUBLIC_JOB_ID = "j125e8ced56da8007c92ab964f58f9f0f"
HANDSHAKE_CANARY_CANONICAL_KEY = (
    "raw::fb45713051f6db962b98c9ba2ef14ad27c795a231530eb4ba3e03c91e41e6109"
)
PREVIEW_PUBLIC_JOB_COUNT = 2
PRODUCTION_PUBLIC_JOB_COUNT = 1
MAX_RELEASE_ARTIFACT_BYTES = 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PublicJobReleaseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublicJobBindingPublication:
    public_job_id: str
    canonical_key: str


@dataclass(frozen=True, slots=True)
class PublishedPublicJobPath:
    path: str
    public_job_id: str
    canonical_key: str


@dataclass(frozen=True, slots=True)
class PublicJobPreviewRelease:
    release_id: str
    database_sha256: str
    registry_sha256: str
    published_details: tuple[PublishedPublicJobPath, ...]

    @property
    def public_job_ids(self) -> tuple[str, ...]:
        return tuple(item.public_job_id for item in self.published_details)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.published_details)

    def as_dict(self) -> dict:
        return {
            "format": PUBLIC_JOB_PREVIEW_RELEASE_FORMAT,
            "release_id": self.release_id,
            "database_sha256": self.database_sha256,
            "registry_sha256": self.registry_sha256,
            "published_details": [
                {
                    "path": item.path,
                    "public_job_id": item.public_job_id,
                    "canonical_key": item.canonical_key,
                }
                for item in self.published_details
            ],
        }


@dataclass(frozen=True, slots=True)
class PublicJobProductionRelease:
    release_id: str
    database_sha256: str
    registry_sha256: str
    published_details: tuple[PublishedPublicJobPath, ...]

    @property
    def public_job_ids(self) -> tuple[str, ...]:
        return tuple(item.public_job_id for item in self.published_details)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.published_details)

    def as_dict(self) -> dict:
        return {
            "format": PUBLIC_JOB_PRODUCTION_RELEASE_FORMAT,
            "release_id": self.release_id,
            "database_sha256": self.database_sha256,
            "registry_sha256": self.registry_sha256,
            "published_details": [
                {
                    "path": item.path,
                    "public_job_id": item.public_job_id,
                    "canonical_key": item.canonical_key,
                }
                for item in self.published_details
            ],
        }


def load_preview_registry_artifact(path_value) -> public_job_identity.PublicJobRegistryArtifact:
    path = _existing_file(path_value)
    payload = _bounded_bytes(path)
    artifact = public_job_identity.PublicJobRegistryArtifact(
        canonical_json=payload,
        sha256=sha256(payload).hexdigest(),
    )
    try:
        registry = public_job_identity.decode_public_job_registry_artifact(artifact)
        _validate_preview_registry(registry)
    except public_job_identity.PublicJobIdentityError:
        raise PublicJobReleaseError("invalid_preview_registry") from None
    return artifact


def load_preview_binding_publications(path_value) -> tuple[PublicJobBindingPublication, ...]:
    document = _strict_json(_bounded_bytes(_existing_file(path_value)))
    if (
        type(document) is not dict
        or set(document) != {"format", "bindings"}
        or document.get("format") != PUBLIC_JOB_PREVIEW_BINDINGS_FORMAT
        or type(document.get("bindings")) is not list
    ):
        raise PublicJobReleaseError("invalid_preview_bindings")
    publications = []
    seen_ids = set()
    seen_keys = set()
    for item in document["bindings"]:
        if type(item) is not dict or set(item) != {"public_job_id", "canonical_key"}:
            raise PublicJobReleaseError("invalid_preview_bindings")
        try:
            public_job_id = public_job_identity.require_public_job_id(
                item.get("public_job_id")
            )
        except public_job_identity.InvalidPublicJobIdentity:
            raise PublicJobReleaseError("invalid_preview_bindings") from None
        canonical_key = item.get("canonical_key")
        if (
            type(canonical_key) is not str
            or not canonical_key
            or len(canonical_key.encode("utf-8")) > 512
            or public_job_id in seen_ids
            or canonical_key in seen_keys
        ):
            raise PublicJobReleaseError("invalid_preview_bindings")
        seen_ids.add(public_job_id)
        seen_keys.add(canonical_key)
        publications.append(PublicJobBindingPublication(public_job_id, canonical_key))
    publications.sort(key=lambda item: item.public_job_id)
    if len(publications) != PREVIEW_PUBLIC_JOB_COUNT:
        raise PublicJobReleaseError("invalid_preview_binding_count")
    return tuple(publications)


def load_production_registry_artifact(
    path_value,
) -> public_job_identity.PublicJobRegistryArtifact:
    path = _existing_file(path_value)
    payload = _bounded_bytes(path)
    artifact = public_job_identity.PublicJobRegistryArtifact(
        canonical_json=payload,
        sha256=sha256(payload).hexdigest(),
    )
    try:
        registry = public_job_identity.decode_public_job_registry_artifact(artifact)
        _validate_production_registry(registry)
    except public_job_identity.PublicJobIdentityError:
        raise PublicJobReleaseError("invalid_production_registry") from None
    return artifact


def load_production_binding_publications(
    path_value,
) -> tuple[PublicJobBindingPublication, ...]:
    document = _strict_json(_bounded_bytes(_existing_file(path_value)))
    if (
        type(document) is not dict
        or set(document) != {"format", "bindings"}
        or document.get("format") != PUBLIC_JOB_PRODUCTION_BINDINGS_FORMAT
        or type(document.get("bindings")) is not list
        or len(document["bindings"]) != PRODUCTION_PUBLIC_JOB_COUNT
    ):
        raise PublicJobReleaseError("invalid_production_bindings")
    item = document["bindings"][0]
    if (
        type(item) is not dict
        or set(item) != {"public_job_id", "canonical_key"}
        or item.get("public_job_id") != HANDSHAKE_CANARY_PUBLIC_JOB_ID
        or item.get("canonical_key") != HANDSHAKE_CANARY_CANONICAL_KEY
    ):
        raise PublicJobReleaseError("invalid_production_bindings")
    return (
        PublicJobBindingPublication(
            HANDSHAKE_CANARY_PUBLIC_JOB_ID,
            HANDSHAKE_CANARY_CANONICAL_KEY,
        ),
    )


def build_preview_release(
    *,
    database_sha256: str,
    registry_artifact: public_job_identity.PublicJobRegistryArtifact,
    bindings: Sequence[PublicJobBindingPublication],
) -> PublicJobPreviewRelease:
    if _SHA256.fullmatch(database_sha256 or "") is None:
        raise PublicJobReleaseError("invalid_projection_digest")
    if type(registry_artifact) is not public_job_identity.PublicJobRegistryArtifact:
        raise PublicJobReleaseError("invalid_registry_artifact")
    try:
        registry = public_job_identity.decode_public_job_registry_artifact(
            registry_artifact
        )
        _validate_preview_registry(registry)
    except public_job_identity.PublicJobIdentityError:
        raise PublicJobReleaseError("invalid_preview_registry") from None
    normalized_bindings = _binding_map(bindings)
    registry_ids = {item["public_job_id"] for item in registry["identities"]}
    if set(normalized_bindings) != registry_ids:
        raise PublicJobReleaseError("preview_binding_registry_mismatch")
    paths = []
    for item in registry["paths"]:
        public_job_id = item["public_job_id"]
        paths.append(
            PublishedPublicJobPath(
                path=item["path"],
                public_job_id=public_job_id,
                canonical_key=normalized_bindings[public_job_id],
            )
        )
    paths.sort(key=lambda item: item.path)
    payload = _release_payload(
        database_sha256,
        registry_artifact.sha256,
        tuple(paths),
    )
    release_id = sha256(_canonical_json(payload)).hexdigest()
    return PublicJobPreviewRelease(
        release_id=release_id,
        database_sha256=database_sha256,
        registry_sha256=registry_artifact.sha256,
        published_details=tuple(paths),
    )


def build_production_release(
    *,
    database_sha256: str,
    registry_artifact: public_job_identity.PublicJobRegistryArtifact,
    bindings: Sequence[PublicJobBindingPublication],
) -> PublicJobProductionRelease:
    if _SHA256.fullmatch(database_sha256 or "") is None:
        raise PublicJobReleaseError("invalid_projection_digest")
    if type(registry_artifact) is not public_job_identity.PublicJobRegistryArtifact:
        raise PublicJobReleaseError("invalid_registry_artifact")
    try:
        registry = public_job_identity.decode_public_job_registry_artifact(
            registry_artifact
        )
        _validate_production_registry(registry)
    except public_job_identity.PublicJobIdentityError:
        raise PublicJobReleaseError("invalid_production_registry") from None
    normalized_bindings = _production_binding_map(bindings)
    registry_ids = {item["public_job_id"] for item in registry["identities"]}
    if set(normalized_bindings) != registry_ids:
        raise PublicJobReleaseError("production_binding_registry_mismatch")
    path = registry["paths"][0]
    published = (
        PublishedPublicJobPath(
            path=path["path"],
            public_job_id=path["public_job_id"],
            canonical_key=normalized_bindings[path["public_job_id"]],
        ),
    )
    payload = _production_release_payload(
        database_sha256,
        registry_artifact.sha256,
        published,
    )
    return PublicJobProductionRelease(
        release_id=sha256(_canonical_json(payload)).hexdigest(),
        database_sha256=database_sha256,
        registry_sha256=registry_artifact.sha256,
        published_details=published,
    )


def canonical_preview_release_json(release: PublicJobPreviewRelease) -> bytes:
    if type(release) is not PublicJobPreviewRelease:
        raise PublicJobReleaseError("invalid_preview_release")
    validate_preview_release_manifest(release.as_dict())
    return _canonical_json(release.as_dict())


def canonical_production_release_json(release: PublicJobProductionRelease) -> bytes:
    if type(release) is not PublicJobProductionRelease:
        raise PublicJobReleaseError("invalid_production_release")
    validate_production_release_manifest(release.as_dict())
    return _canonical_json(release.as_dict())


def validate_preview_release_manifest(document: object) -> PublicJobPreviewRelease:
    if (
        type(document) is not dict
        or set(document)
        != {
            "format",
            "release_id",
            "database_sha256",
            "registry_sha256",
            "published_details",
        }
        or document.get("format") != PUBLIC_JOB_PREVIEW_RELEASE_FORMAT
        or _SHA256.fullmatch(document.get("release_id") or "") is None
        or _SHA256.fullmatch(document.get("database_sha256") or "") is None
        or _SHA256.fullmatch(document.get("registry_sha256") or "") is None
        or type(document.get("published_details")) is not list
    ):
        raise PublicJobReleaseError("invalid_preview_release")
    published = []
    seen_paths = set()
    seen_ids = set()
    for item in document["published_details"]:
        if type(item) is not dict or set(item) != {
            "path",
            "public_job_id",
            "canonical_key",
        }:
            raise PublicJobReleaseError("invalid_preview_release")
        path = item.get("path")
        public_job_id = item.get("public_job_id")
        canonical_key = item.get("canonical_key")
        try:
            public_job_identity.validate_public_job_path(path)
            public_job_identity.require_public_job_id(public_job_id)
        except public_job_identity.InvalidPublicJobIdentity:
            raise PublicJobReleaseError("invalid_preview_release") from None
        if (
            type(canonical_key) is not str
            or not canonical_key
            or path in seen_paths
            or public_job_id in seen_ids
        ):
            raise PublicJobReleaseError("invalid_preview_release")
        seen_paths.add(path)
        seen_ids.add(public_job_id)
        published.append(PublishedPublicJobPath(path, public_job_id, canonical_key))
    published.sort(key=lambda item: item.path)
    if len(published) != PREVIEW_PUBLIC_JOB_COUNT:
        raise PublicJobReleaseError("invalid_preview_release_count")
    _validate_published_path_shapes(published)
    payload = _release_payload(
        document["database_sha256"],
        document["registry_sha256"],
        tuple(published),
    )
    expected_release_id = sha256(_canonical_json(payload)).hexdigest()
    if expected_release_id != document["release_id"]:
        raise PublicJobReleaseError("preview_release_digest_mismatch")
    return PublicJobPreviewRelease(
        release_id=document["release_id"],
        database_sha256=document["database_sha256"],
        registry_sha256=document["registry_sha256"],
        published_details=tuple(published),
    )


def load_preview_release_manifest(path_value) -> PublicJobPreviewRelease:
    document = _strict_json(_bounded_bytes(_existing_file(path_value)))
    return validate_preview_release_manifest(document)


def validate_production_release_manifest(document: object) -> PublicJobProductionRelease:
    if (
        type(document) is not dict
        or set(document)
        != {
            "format",
            "release_id",
            "database_sha256",
            "registry_sha256",
            "published_details",
        }
        or document.get("format") != PUBLIC_JOB_PRODUCTION_RELEASE_FORMAT
        or _SHA256.fullmatch(document.get("release_id") or "") is None
        or _SHA256.fullmatch(document.get("database_sha256") or "") is None
        or _SHA256.fullmatch(document.get("registry_sha256") or "") is None
        or type(document.get("published_details")) is not list
        or len(document["published_details"]) != PRODUCTION_PUBLIC_JOB_COUNT
    ):
        raise PublicJobReleaseError("invalid_production_release")
    item = document["published_details"][0]
    if (
        type(item) is not dict
        or set(item) != {"path", "public_job_id", "canonical_key"}
        or item.get("path") != HANDSHAKE_CANARY_PUBLIC_JOB_PATH
        or item.get("public_job_id") != HANDSHAKE_CANARY_PUBLIC_JOB_ID
        or item.get("canonical_key") != HANDSHAKE_CANARY_CANONICAL_KEY
    ):
        raise PublicJobReleaseError("invalid_production_release_scope")
    try:
        public_job_identity.validate_new_public_job_path(item["path"])
    except public_job_identity.InvalidPublicJobIdentity:
        raise PublicJobReleaseError("invalid_production_release_scope") from None
    published = (
        PublishedPublicJobPath(
            item["path"],
            item["public_job_id"],
            item["canonical_key"],
        ),
    )
    expected = sha256(
        _canonical_json(
            _production_release_payload(
                document["database_sha256"],
                document["registry_sha256"],
                published,
            )
        )
    ).hexdigest()
    if expected != document["release_id"]:
        raise PublicJobReleaseError("production_release_digest_mismatch")
    return PublicJobProductionRelease(
        release_id=document["release_id"],
        database_sha256=document["database_sha256"],
        registry_sha256=document["registry_sha256"],
        published_details=published,
    )


def load_production_release_manifest(path_value) -> PublicJobProductionRelease:
    document = _strict_json(_bounded_bytes(_existing_file(path_value)))
    return validate_production_release_manifest(document)


def _validate_preview_registry(registry: Mapping) -> None:
    identities = registry.get("identities", ())
    paths = registry.get("paths", ())
    if (
        len(identities) != PREVIEW_PUBLIC_JOB_COUNT
        or len(paths) != PREVIEW_PUBLIC_JOB_COUNT
        or any(item.get("disposition") != "serving" for item in identities)
        or any(item.get("path_role") != "primary" for item in paths)
        or {item["public_job_id"] for item in identities}
        != {item["public_job_id"] for item in paths}
    ):
        raise PublicJobReleaseError("invalid_preview_registry_scope")
    _validate_published_path_shapes(
        tuple(
            PublishedPublicJobPath(item["path"], item["public_job_id"], "pending")
            for item in paths
        )
    )


def _validate_production_registry(registry: Mapping) -> None:
    identities = registry.get("identities", ())
    paths = registry.get("paths", ())
    if (
        len(identities) != PRODUCTION_PUBLIC_JOB_COUNT
        or len(paths) != PRODUCTION_PUBLIC_JOB_COUNT
        or identities[0].get("public_job_id") != HANDSHAKE_CANARY_PUBLIC_JOB_ID
        or identities[0].get("disposition") != "serving"
        or identities[0].get("redirect_target_public_job_id") is not None
        or paths[0].get("path") != HANDSHAKE_CANARY_PUBLIC_JOB_PATH
        or paths[0].get("normalized_path") != HANDSHAKE_CANARY_PUBLIC_JOB_PATH
        or paths[0].get("public_job_id") != HANDSHAKE_CANARY_PUBLIC_JOB_ID
        or paths[0].get("path_role") != "primary"
    ):
        raise PublicJobReleaseError("invalid_production_registry_scope")
    try:
        public_job_identity.validate_new_public_job_path(paths[0]["path"])
    except public_job_identity.InvalidPublicJobIdentity:
        raise PublicJobReleaseError("invalid_production_registry_scope") from None


def _validate_published_path_shapes(paths: Sequence[PublishedPublicJobPath]) -> None:
    legacy = [item for item in paths if item.path == KARL_PUBLIC_JOB_PATH]
    new = [item for item in paths if item.path != KARL_PUBLIC_JOB_PATH]
    if (
        len(legacy) != 1
        or len(new) != 1
        or legacy[0].public_job_id != KARL_PUBLIC_JOB_ID
        or legacy[0].canonical_key not in {"pending", KARL_CANONICAL_KEY}
        or not new[0].path.endswith("-" + new[0].public_job_id)
    ):
        raise PublicJobReleaseError("invalid_preview_path_scope")
    try:
        public_job_identity.validate_new_public_job_path(new[0].path)
    except public_job_identity.InvalidPublicJobIdentity:
        raise PublicJobReleaseError("invalid_preview_new_path") from None


def _binding_map(bindings: Sequence[PublicJobBindingPublication]) -> dict[str, str]:
    if isinstance(bindings, (str, bytes)) or not isinstance(bindings, Sequence):
        raise PublicJobReleaseError("invalid_preview_bindings")
    result = {}
    keys = set()
    for item in bindings:
        if type(item) is not PublicJobBindingPublication:
            raise PublicJobReleaseError("invalid_preview_bindings")
        if item.public_job_id in result or item.canonical_key in keys:
            raise PublicJobReleaseError("invalid_preview_bindings")
        result[item.public_job_id] = item.canonical_key
        keys.add(item.canonical_key)
    if len(result) != PREVIEW_PUBLIC_JOB_COUNT:
        raise PublicJobReleaseError("invalid_preview_binding_count")
    return result


def _production_binding_map(
    bindings: Sequence[PublicJobBindingPublication],
) -> dict[str, str]:
    if (
        isinstance(bindings, (str, bytes))
        or not isinstance(bindings, Sequence)
        or len(bindings) != PRODUCTION_PUBLIC_JOB_COUNT
    ):
        raise PublicJobReleaseError("invalid_production_bindings")
    item = bindings[0]
    if (
        type(item) is not PublicJobBindingPublication
        or item.public_job_id != HANDSHAKE_CANARY_PUBLIC_JOB_ID
        or item.canonical_key != HANDSHAKE_CANARY_CANONICAL_KEY
    ):
        raise PublicJobReleaseError("invalid_production_bindings")
    return {item.public_job_id: item.canonical_key}


def _release_payload(database_sha256, registry_sha256, published_details):
    return {
        "format": PUBLIC_JOB_PREVIEW_RELEASE_FORMAT,
        "database_sha256": database_sha256,
        "registry_sha256": registry_sha256,
        "published_details": [
            {
                "path": item.path,
                "public_job_id": item.public_job_id,
                "canonical_key": item.canonical_key,
            }
            for item in published_details
        ],
    }


def _production_release_payload(
    database_sha256,
    registry_sha256,
    published_details,
):
    return {
        "format": PUBLIC_JOB_PRODUCTION_RELEASE_FORMAT,
        "database_sha256": database_sha256,
        "registry_sha256": registry_sha256,
        "published_details": [
            {
                "path": item.path,
                "public_job_id": item.public_job_id,
                "canonical_key": item.canonical_key,
            }
            for item in published_details
        ],
    }


def _canonical_json(document) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _strict_json(payload: bytes):
    try:
        return json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, TypeError):
        raise PublicJobReleaseError("invalid_release_json") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


def _existing_file(value) -> Path:
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PublicJobReleaseError("release_file_unavailable") from None
    if not resolved.is_file():
        raise PublicJobReleaseError("release_file_unavailable")
    return resolved


def _bounded_bytes(path: Path) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError:
        raise PublicJobReleaseError("release_file_unavailable") from None
    if not 1 <= len(payload) <= MAX_RELEASE_ARTIFACT_BYTES:
        raise PublicJobReleaseError("release_file_unavailable")
    return payload


__all__ = [
    "HANDSHAKE_CANARY_CANONICAL_KEY",
    "HANDSHAKE_CANARY_PUBLIC_JOB_ID",
    "HANDSHAKE_CANARY_PUBLIC_JOB_PATH",
    "KARL_PUBLIC_JOB_PATH",
    "KARL_PUBLIC_JOB_ID",
    "KARL_CANONICAL_KEY",
    "PUBLIC_JOB_PREVIEW_BINDINGS_FORMAT",
    "PUBLIC_JOB_PREVIEW_RELEASE_FORMAT",
    "PUBLIC_JOB_PRODUCTION_BINDINGS_FORMAT",
    "PUBLIC_JOB_PRODUCTION_RELEASE_FORMAT",
    "PREVIEW_PUBLIC_JOB_COUNT",
    "PRODUCTION_PUBLIC_JOB_COUNT",
    "PublicJobBindingPublication",
    "PublicJobPreviewRelease",
    "PublicJobProductionRelease",
    "PublicJobReleaseError",
    "PublishedPublicJobPath",
    "build_preview_release",
    "build_production_release",
    "canonical_preview_release_json",
    "canonical_production_release_json",
    "load_production_binding_publications",
    "load_production_registry_artifact",
    "load_production_release_manifest",
    "load_preview_binding_publications",
    "load_preview_registry_artifact",
    "load_preview_release_manifest",
    "validate_preview_release_manifest",
    "validate_production_release_manifest",
]
