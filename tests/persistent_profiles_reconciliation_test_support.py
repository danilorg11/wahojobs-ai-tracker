import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from tests.persistent_profiles_repository_test_support import (
    NOW,
    append_command,
    canonical_fixture,
    connect_repository_database,
    create_command,
    development_context,
    install_repository_database,
    reference,
)
from wahojobs.persistent_profile_canonical_v2_schema import (
    attest_persistent_profile_canonical_v2_schema,
)
from wahojobs.persistent_profiles_repository import (
    append_profile_revision,
    create_persistent_profile,
)
from wahojobs.persistent_profiles import SOURCE_BUNDLE_HASH_VERSION


PROFILE_TRIGGER_PREFIXES = (
    "trg_product_profiles_",
    "trg_product_profile_revisions_",
    "trg_product_profile_sources_",
)


def installed_database(path):
    setup = install_repository_database(path)
    setup.close()
    connection = connect_repository_database(path)
    connection.row_factory = sqlite3.Row
    return connection


def seed_profile(connection, suffix="101"):
    principal = development_context(connection, suffix)
    created = create_persistent_profile(
        connection,
        create_command(
            principal,
            idempotency_key=f"profile-create-{int(suffix):08d}",
        ),
    )
    return principal, created, reference(created, principal)


def append_revision(
    connection,
    principal,
    profile_reference,
    *,
    expected_revision=1,
    revision_kind="edit",
    correction_of_revision_id=None,
):
    return append_profile_revision(
        connection,
        append_command(
            principal,
            profile_reference,
            canonical_fixture(profile_reference.profile_id),
            expected_revision=expected_revision,
            revision_kind=revision_kind,
            correction_of_revision_id=correction_of_revision_id,
            idempotency_key=(
                f"profile-{revision_kind}-{expected_revision:08d}"
            ),
            accepted_at=NOW + timedelta(seconds=expected_revision),
        ),
    )


@contextmanager
def relaxed_profile_guards(connection):
    """Relax only a temporary database, restoring exact trigger SQL afterward."""
    connection.commit()
    trigger_rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()
    profile_triggers = [
        (name, sql)
        for name, sql in trigger_rows
        if name.startswith(PROFILE_TRIGGER_PREFIXES)
    ]
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA ignore_check_constraints = ON")
    for name, _sql in profile_triggers:
        connection.execute(f'DROP TRIGGER "{name}"')
    connection.commit()
    try:
        yield connection
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        for _name, sql in profile_triggers:
            connection.execute(sql)
        connection.execute("PRAGMA ignore_check_constraints = OFF")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        assert (
            attest_persistent_profile_canonical_v2_schema(connection)["state"]
            == "correctly_installed"
        )


def corrupt_one(connection, sql, parameters=()):
    with relaxed_profile_guards(connection):
        connection.execute(sql, parameters)


def revision_identity(connection):
    return connection.execute(
        "SELECT revision_id, profile_id, principal_id, environment_namespace, "
        "revision_number FROM product_profile_revisions ORDER BY revision_number LIMIT 1"
    ).fetchone()


def source_identity(connection):
    return connection.execute(
        "SELECT source_id, revision_id, profile_id, principal_id, "
        "environment_namespace, source_ordinal FROM product_profile_sources "
        "ORDER BY source_ordinal LIMIT 1"
    ).fetchone()


def canonical_json_for_profile(profile_id):
    return json.dumps(
        canonical_fixture(profile_id),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def finding_codes(report):
    return {code for code, _count in report.finding_counts_by_code}


def sidecars(path):
    return sorted(path.parent.glob(path.name + "-*"))


def query_only_fingerprint(path):
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def seed_many_profiles(
    connection,
    count,
    *,
    revisions_per_profile=2,
    sources_per_revision=2,
):
    """Create a realistic deterministic dataset under the exact installed guards."""
    if revisions_per_profile < 1 or sources_per_revision < 1:
        raise ValueError("invalid performance fixture dimensions")
    if sources_per_revision > 16:
        raise ValueError("source bundle exceeds installed limit")
    initial_timestamp = NOW.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    connection.execute("BEGIN")
    connection.execute("PRAGMA defer_foreign_keys = ON")
    for number in range(1, count + 1):
        principal_id = f"prn_{number:032x}"
        profile_id = f"prf_{number:032x}"
        initial_revision_number = (number - 1) * revisions_per_profile + 1
        initial_revision_id = f"pvr_{initial_revision_number:032x}"
        connection.execute(
            "INSERT INTO product_principals "
            "(principal_id, environment_namespace, principal_type, lifecycle_status, "
            "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
            "provenance_json) VALUES (?, 'test', 'development', 'active', "
            "'nonclaimable', 0, 1, ?, ?, '{}')",
            (principal_id, initial_timestamp, initial_timestamp),
        )
        profile_json = canonical_json_for_profile(profile_id)
        profile_digest = sha256_text(profile_json)
        connection.execute(
            "INSERT INTO product_profiles "
            "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
            "VALUES (?, ?, 'test', ?, ?)",
            (
                profile_id,
                principal_id,
                initial_revision_id,
                initial_timestamp,
            ),
        )
        previous_revision_id = None
        for revision_number in range(1, revisions_per_profile + 1):
            global_revision_number = (
                (number - 1) * revisions_per_profile + revision_number
            )
            revision_id = f"pvr_{global_revision_number:032x}"
            timestamp = (NOW + timedelta(seconds=revision_number - 1)).strftime(
                "%Y-%m-%dT%H:%M:%S+00:00"
            )
            manifest_sources = []
            for source_ordinal in range(1, sources_per_revision + 1):
                global_source_number = (
                    (global_revision_number - 1) * sources_per_revision
                    + source_ordinal
                )
                source_id = f"pfs_{global_source_number:032x}"
                source_content = (
                    f"Confirmed profile background {number}:{revision_number}:"
                    f"{source_ordinal}."
                )
                source_digest = sha256_text(source_content)
                connection.execute(
                    "INSERT INTO product_profile_sources "
                    "(source_id, revision_id, profile_id, principal_id, environment_namespace, "
                    "source_ordinal, source_type, source_format, source_content, "
                    "source_content_sha256, source_schema_version, parser_version, accepted_at) "
                    "VALUES (?, ?, ?, ?, 'test', ?, 'confirmed_about_you_text', 'text/plain', "
                    "?, ?, 'confirmed_about_you_text_v1', 'baseline_v1', ?)",
                    (
                        source_id,
                        revision_id,
                        profile_id,
                        principal_id,
                        source_ordinal,
                        source_content,
                        source_digest,
                        timestamp,
                    ),
                )
                manifest_sources.append(
                    {
                        "ordinal": source_ordinal,
                        "source_type": "confirmed_about_you_text",
                        "source_format": "text/plain",
                        "source_schema_version": "confirmed_about_you_text_v1",
                        "parser_version": "baseline_v1",
                        "confirmed_at": timestamp,
                        "byte_length": len(source_content.encode("utf-8")),
                        "source_content_hash": source_digest,
                    }
                )
            manifest = {
                "version": SOURCE_BUNDLE_HASH_VERSION,
                "sources": manifest_sources,
            }
            bundle_digest = hashlib.sha256(
                json.dumps(
                    manifest,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "INSERT INTO product_profile_revisions "
                "(revision_id, profile_id, principal_id, environment_namespace, revision_number, "
                "previous_revision_id, correction_of_revision_id, revision_kind, lifecycle_status, "
                "canonical_schema_version, structured_profile_json, structured_profile_sha256, "
                "source_count, source_bundle_sha256, normalizer_version, reviewer_version, "
                "actor_type, reason_code, idempotency_key, request_fingerprint, created_at) "
                "VALUES (?, ?, ?, 'test', ?, ?, NULL, ?, 'active', "
                "'canonical_profile_v2', ?, ?, ?, ?, 'baseline_v1', NULL, "
                "'development_service', ?, ?, ?, ?)",
                (
                    revision_id,
                    profile_id,
                    principal_id,
                    revision_number,
                    previous_revision_id,
                    "initial" if revision_number == 1 else "edit",
                    profile_json,
                    profile_digest,
                    sources_per_revision,
                    bundle_digest,
                    "profile.create" if revision_number == 1 else "profile.edit",
                    f"profile-r{revision_number}-{number:08d}",
                    hashlib.sha256(
                        f"request-{number}-{revision_number}".encode()
                    ).hexdigest(),
                    timestamp,
                ),
            )
            previous_revision_id = revision_id
    connection.commit()
