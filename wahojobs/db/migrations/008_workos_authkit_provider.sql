DROP TRIGGER trg_auth_identities_immutable_identity;

DROP INDEX idx_auth_identities_user;

ALTER TABLE auth_identities RENAME TO auth_identities_m008_backup;

CREATE TABLE auth_identities (
  auth_identity_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_subject TEXT NOT NULL,
  verified_email TEXT,
  email_verified INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  last_authenticated_at TEXT NOT NULL,
  disabled_at TEXT,
  link_idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,

  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  UNIQUE (provider, provider_subject),
  UNIQUE (user_id, provider),
  CHECK (provider IN ('google', 'workos_authkit')),
  CHECK (email_verified IN (0, 1)),
  CHECK (email_verified = 0 OR verified_email IS NOT NULL),
  CHECK (length(provider_subject) BETWEEN 1 AND 1024),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(last_authenticated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', last_authenticated_at) IS last_authenticated_at),
  CHECK (disabled_at IS NULL OR (length(disabled_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', disabled_at) IS disabled_at AND julianday(disabled_at) >= julianday(created_at))),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*')
);

INSERT INTO auth_identities (
  auth_identity_id,
  user_id,
  provider,
  provider_subject,
  verified_email,
  email_verified,
  created_at,
  last_authenticated_at,
  disabled_at,
  link_idempotency_key,
  request_fingerprint
)
SELECT
  auth_identity_id,
  user_id,
  provider,
  provider_subject,
  verified_email,
  email_verified,
  created_at,
  last_authenticated_at,
  disabled_at,
  link_idempotency_key,
  request_fingerprint
FROM auth_identities_m008_backup;

DROP TABLE auth_identities_m008_backup;

CREATE INDEX idx_auth_identities_user ON auth_identities(user_id);

CREATE TRIGGER trg_auth_identities_immutable_identity
BEFORE UPDATE OF user_id, provider, provider_subject ON auth_identities
BEGIN
  SELECT RAISE(ABORT, 'authentication identity is immutable');
END;
