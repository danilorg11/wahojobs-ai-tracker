import hashlib
import json
from datetime import datetime, timedelta, timezone

from tests.ownership_test_support import (
    add_activation_event,
    add_active_user,
    add_binding,
    add_principal,
    install_ownership,
)

import scripts.persistent_profiles_migration as migration


NOW = datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc)


def timestamp(offset=0):
    return (NOW + timedelta(seconds=offset)).isoformat(timespec="seconds")


def install_persistent_profiles(path):
    conn = install_ownership(path)
    migration.apply_persistent_profiles_migration(conn)
    return conn


def add_development_principal(conn, suffix="1", *, environment="test"):
    principal_id = add_principal(
        conn,
        suffix=suffix,
        environment=environment,
        principal_type="development",
        status="active",
        claim_policy="nonclaimable",
        exclusive=0,
    )
    conn.commit()
    return principal_id


def add_account_principal(conn, suffix="2", *, environment="private_beta"):
    user_id = add_active_user(conn, f"profile-{suffix}")
    principal_id = add_principal(
        conn,
        suffix=suffix,
        environment=environment,
        principal_type="account_native",
        status="active",
        claim_policy="account_native",
        exclusive=1,
    )
    binding_id = add_binding(
        conn,
        principal_id,
        user_id,
        suffix=suffix,
        environment=environment,
    )
    add_activation_event(
        conn,
        principal_id,
        user_id,
        binding_id,
        suffix=suffix,
        environment=environment,
    )
    conn.commit()
    return principal_id


def stable_id(prefix, value):
    return f"{prefix}_{int(value):032x}"


def canonical_document(profile_id, *, extra=None):
    document = {
        "schema_version": "canonical_profile_v1",
        "identity": {"profile_id": profile_id},
        "provenance": {},
    }
    if extra:
        document.update(extra)
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_profile(
    conn,
    principal_id,
    *,
    suffix="1",
    environment="test",
    source_content="I build reliable software and review AI outputs.",
    structured_json=None,
    created_at=None,
):
    profile_id = stable_id("prf", suffix)
    revision_id = stable_id("pvr", suffix)
    source_id = stable_id("pfs", suffix)
    when = created_at or timestamp()
    structured_json = structured_json or canonical_document(profile_id)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO product_profiles "
            "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (profile_id, principal_id, environment, revision_id, when),
        )
        insert_source(
            conn,
            source_id=source_id,
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal_id,
            environment=environment,
            source_content=source_content,
            accepted_at=when,
        )
        insert_revision(
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


def append_revision(
    conn,
    profile_id,
    principal_id,
    *,
    revision_number,
    previous_revision_id,
    suffix,
    environment="test",
    revision_kind="edit",
    lifecycle_status="active",
    correction_of_revision_id=None,
    source_content=None,
    structured_json=None,
    created_at=None,
):
    revision_id = stable_id("pvr", suffix)
    source_id = stable_id("pfs", suffix)
    when = created_at or timestamp(revision_number)
    source_content = source_content or json.dumps(
        {"confirmed_revision": revision_number}, separators=(",", ":")
    )
    structured_json = structured_json or canonical_document(profile_id)
    conn.execute("BEGIN")
    try:
        insert_source(
            conn,
            source_id=source_id,
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal_id,
            environment=environment,
            source_type="user_confirmed_correction",
            source_format="application/json",
            source_content=source_content,
            accepted_at=when,
        )
        insert_revision(
            conn,
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal_id,
            environment=environment,
            revision_number=revision_number,
            previous_revision_id=previous_revision_id,
            correction_of_revision_id=correction_of_revision_id,
            revision_kind=revision_kind,
            lifecycle_status=lifecycle_status,
            structured_json=structured_json,
            created_at=when,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return revision_id


def insert_source(
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
    ordinal=1,
):
    conn.execute(
        "INSERT INTO product_profile_sources "
        "(source_id, revision_id, profile_id, principal_id, environment_namespace, "
        "source_ordinal, source_type, source_format, source_content, "
        "source_content_sha256, source_schema_version, parser_version, accepted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed_source_v1', NULL, ?)",
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
            accepted_at,
        ),
    )


def insert_revision(
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
):
    conn.execute(
        "INSERT INTO product_profile_revisions "
        "(revision_id, profile_id, principal_id, environment_namespace, revision_number, "
        "previous_revision_id, correction_of_revision_id, revision_kind, lifecycle_status, "
        "canonical_schema_version, structured_profile_json, structured_profile_sha256, "
        "source_count, source_bundle_sha256, normalizer_version, reviewer_version, actor_type, "
        "reason_code, idempotency_key, request_fingerprint, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'canonical_profile_v1', ?, ?, ?, ?, "
        "'baseline_v1', 'review_v1', 'development_service', 'test_revision', ?, ?, ?)",
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
            structured_json,
            digest(structured_json),
            source_count,
            digest(f"bundle:{revision_id}"),
            f"profile-revision-{revision_id}",
            digest(f"request:{revision_id}"),
            created_at,
        ),
    )
