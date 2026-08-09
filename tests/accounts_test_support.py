import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import accounts_migration  # noqa: E402
import pipeline_state_migration  # noqa: E402
from wahojobs import accounts  # noqa: E402
from wahojobs.db.repository import install_base_schema  # noqa: E402


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
INVITATION_KEY = b"test-only-invitation-key-32-bytes-minimum"
IDENTITY_VERIFIER = accounts.TrustedIdentityVerifier()
ACCOUNT_SERVICE = accounts.AccountService(IDENTITY_VERIFIER)


def connect(path):
    conn = sqlite3.connect(path, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def install_base(path):
    conn = connect(path)
    install_base_schema(conn)
    conn.commit()
    return conn


def install_migration_001(conn):
    pipeline_state_migration.apply_pipeline_state_migration(conn)


def install_accounts(path):
    conn = install_base(path)
    install_migration_001(conn)
    accounts_migration.apply_accounts_migration(conn)
    return conn


def trusted_identity(subject, email, *, authenticated_at=NOW):
    return IDENTITY_VERIFIER.from_validated_google_claims(
        provider_subject=subject,
        verified_email=email,
        email_verified=True,
        authenticated_at=authenticated_at,
        metadata_version="google_oidc_v1",
    )


def create_user(conn, suffix="one", *, now=NOW):
    email = f"person-{suffix}@example.test"
    invitation_creation = accounts.create_invitation(
        conn,
        email=email,
        lookup_key=INVITATION_KEY,
        expires_at=now + timedelta(days=7),
        created_by="test_admin",
        idempotency_key=f"invite-create-{suffix}",
        now=now,
    )
    created = ACCOUNT_SERVICE.create_invited_user(
        conn,
        identity=trusted_identity(f"google-subject-{suffix}", email, authenticated_at=now),
        invitation_token=invitation_creation.invitation_token,
        invitation_lookup_key=INVITATION_KEY,
        idempotency_key=f"user-create-{suffix}",
        now=now,
    )
    return invitation_creation.invitation, created
