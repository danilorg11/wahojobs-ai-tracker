import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.persistent_profile_canonical_v2_test_support import (
    install_canonical_v2_profiles,
)
from tests.persistent_profiles_test_support import (
    add_account_principal,
    add_development_principal,
)
from wahojobs.persistent_profiles import (
    AppendProfileRevisionCommand,
    ConfirmedAboutYouTextSourceDraft,
    CreatePersistentProfileCommand,
    LifecycleActionSourceDraft,
    PurgePersistentProfileCommand,
    TrustedPersistentProfileReference,
    TrustedPrincipalContext,
    TrustedPrivacyAdminContext,
    UserConfirmedCorrectionSourceDraft,
)
from wahojobs.profiles.canonical import complete_trusted_fixture_provenance
from wahojobs.profiles.canonical_v2 import convert_v1_to_v2


NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
SUITE_PATH = Path(__file__).resolve().parent / "fixtures" / "profile_normalization_v1.json"


def _ordinal_resolver(_path, _source_kind, _explicit):
    return [1]


def canonical_fixture(profile_id="prf_0123456789abcdef0123456789abcdef"):
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    v1 = complete_trusted_fixture_provenance(
        suite["cases"][0]["expected_canonical_profile"]
    )
    return convert_v1_to_v2(
        v1,
        persistent_profile_id=profile_id,
        source_ordinal_resolver=_ordinal_resolver,
    )


def install_repository_database(path):
    return install_canonical_v2_profiles(path)


def connect_repository_database(path, *, timeout=3.0):
    connection = sqlite3.connect(path, timeout=timeout)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def development_context(connection, suffix="41"):
    principal_id = add_development_principal(connection, suffix)
    return TrustedPrincipalContext(
        principal_id=principal_id,
        environment_namespace="test",
        principal_type="development",
        lifecycle_status="active",
        claim_policy="nonclaimable",
        exclusive_account_binding=False,
        eligibility_mode="development_test",
        active_owner_binding=None,
    )


def account_context(connection, suffix="42"):
    principal_id = add_account_principal(
        connection, suffix, environment="private_beta"
    )
    return TrustedPrincipalContext(
        principal_id=principal_id,
        environment_namespace="private_beta",
        principal_type="account_native",
        lifecycle_status="active",
        claim_policy="account_native",
        exclusive_account_binding=True,
        eligibility_mode="account_native",
        active_owner_binding=True,
    )


def create_command(
    principal,
    *,
    idempotency_key="profile-create-0001",
    reason_code="profile.create",
    accepted_at=NOW,
    source_text="Confirmed profile background.",
):
    return CreatePersistentProfileCommand.prepare(
        principal=principal,
        canonical_profile_v2=canonical_fixture(),
        sources=(ConfirmedAboutYouTextSourceDraft(source_text, accepted_at),),
        normalizer_version="baseline_v1",
        reviewer_version=None,
        actor_type=(
            "authenticated_user"
            if principal.eligibility_mode == "account_native"
            else "development_service"
        ),
        reason_code=reason_code,
        idempotency_key=idempotency_key,
        accepted_at=accepted_at,
    )


def reference(created, principal):
    return TrustedPersistentProfileReference(
        profile_id=created.profile_id,
        principal_id=principal.principal_id,
        environment_namespace=principal.environment_namespace,
    )


def append_command(
    principal,
    profile_reference,
    canonical_profile,
    *,
    expected_revision=1,
    revision_kind="edit",
    idempotency_key="profile-append-0001",
    accepted_at=None,
    correction_of_revision_id=None,
):
    accepted_at = accepted_at or NOW + timedelta(seconds=expected_revision)
    if revision_kind in {"archive", "reactivate", "deletion_request"}:
        sources = (
            LifecycleActionSourceDraft.for_action(
                revision_kind, confirmed_at=accepted_at
            ),
        )
    elif revision_kind == "correction":
        sources = (
            UserConfirmedCorrectionSourceDraft(
                '{"field":"languages"}', accepted_at
            ),
        )
    else:
        sources = (ConfirmedAboutYouTextSourceDraft("Confirmed edit.", accepted_at),)
    return AppendProfileRevisionCommand.prepare(
        principal=principal,
        profile=profile_reference,
        expected_current_revision_number=expected_revision,
        revision_kind=revision_kind,
        canonical_profile_v2=canonical_profile,
        sources=sources,
        correction_of_revision_id=correction_of_revision_id,
        normalizer_version="baseline_v1",
        reviewer_version=None,
        actor_type=(
            "authenticated_user"
            if principal.eligibility_mode == "account_native"
            else "development_service"
        ),
        reason_code=f"profile.{revision_kind}",
        idempotency_key=idempotency_key,
        accepted_at=accepted_at,
    )


def purge_command(profile_reference, *, accepted_at=NOW + timedelta(minutes=1)):
    return PurgePersistentProfileCommand.prepare(
        privacy_admin=TrustedPrivacyAdminContext(
            "purge", profile_reference.environment_namespace
        ),
        profile=profile_reference,
        operation_key="profile-purge-0001",
        accepted_at=accepted_at,
    )


def profile_counts(connection):
    return tuple(
        connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in (
            "product_profiles",
            "product_profile_revisions",
            "product_profile_sources",
            "current_product_profiles",
        )
    )
