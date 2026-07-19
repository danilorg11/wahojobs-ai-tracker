import json

import scripts.persistent_profile_canonical_v2_migration as migration_005
from tests.persistent_profiles_test_support import (
    digest,
    install_persistent_profiles,
    stable_id,
    timestamp,
)


def install_canonical_v2_profiles(path):
    conn = install_persistent_profiles(path)
    migration_005.apply_persistent_profile_canonical_v2_migration(conn)
    return conn


def canonical_v2_document(profile_id, *, extra=None):
    document = {
        "schema_version": "canonical_profile_v2",
        "identity": {"profile_id": profile_id},
        "provenance": {},
    }
    if extra:
        document.update(extra)
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def lifecycle_source_content(action):
    return json.dumps(
        {
            "action": action,
            "schema_version": "confirmed_lifecycle_action_v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def insert_source_v2(
    conn,
    *,
    source_id,
    revision_id,
    profile_id,
    principal_id,
    environment,
    source_content,
    accepted_at,
    source_type="confirmed_about_you_text",
    source_format="text/plain",
    source_schema_version="confirmed_source_v1",
    ordinal=1,
):
    conn.execute(
        "INSERT INTO product_profile_sources "
        "(source_id, revision_id, profile_id, principal_id, environment_namespace, "
        "source_ordinal, source_type, source_format, source_content, "
        "source_content_sha256, source_schema_version, parser_version, accepted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            source_id,
            revision_id,
            profile_id,
            principal_id,
            environment,
            ordinal,
            source_type,
            source_format,
            source_content,
            digest(source_content),
            source_schema_version,
            accepted_at,
        ),
    )


def insert_revision_v2(
    conn,
    *,
    revision_id,
    profile_id,
    principal_id,
    environment,
    revision_number,
    previous_revision_id,
    revision_kind,
    lifecycle_status,
    structured_json,
    created_at,
    correction_of_revision_id=None,
    source_count=1,
    structured_hash=None,
    canonical_schema_version="canonical_profile_v2",
):
    conn.execute(
        "INSERT INTO product_profile_revisions "
        "(revision_id, profile_id, principal_id, environment_namespace, revision_number, "
        "previous_revision_id, correction_of_revision_id, revision_kind, lifecycle_status, "
        "canonical_schema_version, structured_profile_json, structured_profile_sha256, "
        "source_count, source_bundle_sha256, normalizer_version, reviewer_version, actor_type, "
        "reason_code, idempotency_key, request_fingerprint, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'baseline_v2', 'review_v1', 'development_service', 'test_revision', ?, ?, ?)",
        (
            revision_id,
            profile_id,
            principal_id,
            environment,
            revision_number,
            previous_revision_id,
            correction_of_revision_id,
            revision_kind,
            lifecycle_status,
            canonical_schema_version,
            structured_json,
            structured_hash or digest(structured_json),
            source_count,
            digest(f"bundle:{revision_id}"),
            f"profile-revision-{revision_id}",
            digest(f"request:{revision_id}"),
            created_at,
        ),
    )


def create_v2_profile(
    conn,
    principal_id,
    *,
    suffix="1",
    environment="test",
    source_content="Confirmed profile input.",
    structured_json=None,
):
    profile_id = stable_id("prf", suffix)
    revision_id = stable_id("pvr", suffix)
    when = timestamp()
    structured_json = structured_json or canonical_v2_document(profile_id)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO product_profiles "
            "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (profile_id, principal_id, environment, revision_id, when),
        )
        insert_source_v2(
            conn,
            source_id=stable_id("pfs", suffix),
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal_id,
            environment=environment,
            source_content=source_content,
            accepted_at=when,
        )
        insert_revision_v2(
            conn,
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal_id,
            environment=environment,
            revision_number=1,
            previous_revision_id=None,
            revision_kind="initial",
            lifecycle_status="active",
            structured_json=structured_json,
            created_at=when,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return profile_id, revision_id


def append_v2_revision(
    conn,
    profile_id,
    principal_id,
    *,
    revision_number,
    previous_revision_id,
    suffix,
    revision_kind="edit",
    lifecycle_status="active",
    correction_of_revision_id=None,
    structured_json=None,
    source_type=None,
    source_content=None,
    source_count=1,
    ordinal=1,
    structured_hash=None,
):
    revision_id = stable_id("pvr", suffix)
    when = timestamp(revision_number)
    previous_json = conn.execute(
        "SELECT structured_profile_json FROM product_profile_revisions WHERE revision_id=?",
        (previous_revision_id,),
    ).fetchone()[0]
    structured_json = structured_json or previous_json
    lifecycle = revision_kind in {"archive", "reactivate", "deletion_request"}
    source_type = source_type or (
        "confirmed_lifecycle_action" if lifecycle else "user_confirmed_correction"
    )
    source_content = source_content or (
        lifecycle_source_content(revision_kind)
        if lifecycle
        else json.dumps({"confirmed_revision": revision_number}, separators=(",", ":"))
    )
    conn.execute("BEGIN")
    try:
        insert_source_v2(
            conn,
            source_id=stable_id("pfs", suffix),
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal_id,
            environment="test",
            source_content=source_content,
            accepted_at=when,
            source_type=source_type,
            source_format="application/json",
            source_schema_version=(
                "confirmed_lifecycle_action_v1"
                if source_type == "confirmed_lifecycle_action"
                else "confirmed_source_v1"
            ),
            ordinal=ordinal,
        )
        insert_revision_v2(
            conn,
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal_id,
            environment="test",
            revision_number=revision_number,
            previous_revision_id=previous_revision_id,
            correction_of_revision_id=correction_of_revision_id,
            revision_kind=revision_kind,
            lifecycle_status=lifecycle_status,
            structured_json=structured_json,
            structured_hash=structured_hash,
            source_count=source_count,
            created_at=when,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return revision_id
