from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile

import scripts.google_oidc_authorization_transactions_migration as migration_006
from tests.google_oidc_gateway_test_support import (
    CLIENT_SECRET,
    ManualClock,
    NOW,
    authorization_parameters,
    close_secret_vault,
    completion_policy,
    make_real_gateway,
    request_secret_vault,
    seed_existing_google_identity,
    sockets_blocked,
    vault_entry_count,
)
from tests.persistent_profile_canonical_v2_test_support import (
    install_canonical_v2_profiles,
)


LOOKUP_KEY_MATERIAL = {
    1: bytes.fromhex(
        "1057e22bead62f3477530331d038a4a2"
        "ec2f43f59a0f7f644f2ea567bf8d4471"
    ),
    2: bytes.fromhex(
        "72d03d8f75c53d64efebedc90df0d3a1"
        "d074577a7e95b927aca5b59666623c0f"
    ),
    3: bytes.fromhex(
        "f14d88d674f4ea9a9e53b6d774b739b6"
        "5ab85f830de4486a78d74a47a72e5ec8"
    ),
}
PROTECTION_KEY_MATERIAL = {
    11: bytes.fromhex(
        "f00ca456d59d5ee2a8cbc89d3d15675f"
        "b35bc26a3c75986a7b80f40e4c8d3921"
    ),
    12: bytes.fromhex(
        "09234d17ccb577896bb9dac0045b53fd"
        "63753e642292cb0e14a2c449946c6103"
    ),
    13: bytes.fromhex(
        "9fac4f14d23ad9d583ae42cdf837a674"
        "e101379da00db08614942084c792c431"
    ),
}


@dataclass(frozen=True, slots=True)
class DurableTransactionDatabase:
    path: Path
    connection: sqlite3.Connection
    account_id: str
    identity_id: str
    subject: str


def key_authority(
    *,
    lookup_versions=(1,),
    protection_versions=(11,),
    active_lookup_version=None,
    active_protection_version=None,
):
    from wahojobs.google_oidc_transaction_protection import (
        GoogleOidcTransactionKeyAuthority,
    )

    lookup_versions = tuple(lookup_versions)
    protection_versions = tuple(protection_versions)
    if active_lookup_version is None:
        active_lookup_version = lookup_versions[-1]
    if active_protection_version is None:
        active_protection_version = protection_versions[-1]
    lookup = {
        version: bytearray(LOOKUP_KEY_MATERIAL[version])
        for version in lookup_versions
    }
    protection = {
        version: bytearray(PROTECTION_KEY_MATERIAL[version])
        for version in protection_versions
    }
    authority = GoogleOidcTransactionKeyAuthority.from_mutable_keys(
        lookup_keys=lookup,
        protection_keys=protection,
        active_lookup_version=active_lookup_version,
        active_protection_version=active_protection_version,
    )
    if any(buffer for buffer in (*lookup.values(), *protection.values())):
        authority.close()
        raise AssertionError("key_authority_did_not_consume_test_buffers")
    return authority


def open_connection(path, *, timeout=2.0):
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextlib.contextmanager
def durable_transaction_database(*, suffix="durable-oidc"):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{suffix}.sqlite"
        connection = install_canonical_v2_profiles(path)
        migration_006.apply_google_oidc_authorization_transactions_migration(
            connection,
            requested_path=path,
            expected_identity=migration_006.database_file_identity(path),
        )
        connection.row_factory = sqlite3.Row
        connection.text_factory = str
        created = seed_existing_google_identity(
            connection,
            suffix=suffix,
        )
        try:
            yield DurableTransactionDatabase(
                path=path,
                connection=connection,
                account_id=created.user.user_id,
                identity_id=created.identity.auth_identity_id,
                subject=f"google-subject-{suffix}",
            )
        finally:
            connection.close()


def transaction_rows(connection):
    return tuple(
        dict(row)
        for row in connection.execute(
            "SELECT transaction_id, record_version, provider, "
            "environment_namespace, configuration_fingerprint, "
            "state_digest_version, lookup_key_version, state_lookup_digest, "
            "created_at, expires_at, lifecycle, claimed_at, terminal_at, "
            "row_version, protection_envelope_version, "
            "protection_key_version, protection_nonce, protected_material "
            "FROM google_oidc_authorization_transactions "
            "ORDER BY transaction_id"
        ).fetchall()
    )


def reconstructed_gateway(*, clock=None, subject=None):
    return make_real_gateway(
        clock=clock or ManualClock(NOW),
        client_secret=bytearray(CLIENT_SECRET),
        subject=subject or "google-subject-durable-oidc",
    )


__all__ = (
    "DurableTransactionDatabase",
    "LOOKUP_KEY_MATERIAL",
    "ManualClock",
    "NOW",
    "PROTECTION_KEY_MATERIAL",
    "authorization_parameters",
    "close_secret_vault",
    "completion_policy",
    "durable_transaction_database",
    "key_authority",
    "make_real_gateway",
    "open_connection",
    "reconstructed_gateway",
    "request_secret_vault",
    "sockets_blocked",
    "transaction_rows",
    "vault_entry_count",
)
