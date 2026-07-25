CREATE TABLE google_oidc_authorization_transactions (
  transaction_id TEXT PRIMARY KEY NOT NULL,
  record_version INTEGER NOT NULL,
  provider TEXT NOT NULL,
  environment_namespace TEXT NOT NULL,
  configuration_fingerprint BLOB NOT NULL,
  state_digest_version INTEGER NOT NULL,
  lookup_key_version INTEGER NOT NULL,
  state_lookup_digest BLOB NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  claimed_at TEXT,
  terminal_at TEXT,
  row_version INTEGER NOT NULL,
  protection_envelope_version INTEGER NOT NULL,
  protection_key_version INTEGER NOT NULL,
  protection_nonce BLOB NOT NULL,
  protected_material BLOB NOT NULL,

  CHECK (typeof(transaction_id) = 'text'),
  CHECK (length(transaction_id) = 39),
  CHECK (substr(transaction_id, 1, 7) = 'oidctx_'),
  CHECK (substr(transaction_id, 8) NOT GLOB '*[^0-9a-f]*'),
  CHECK (typeof(record_version) = 'integer' AND record_version = 1),
  CHECK (typeof(provider) = 'text' AND provider = 'google'),
  CHECK (typeof(environment_namespace) = 'text'),
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (substr(environment_namespace, 1, 1) GLOB '[a-z0-9]'),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (
    typeof(configuration_fingerprint) = 'blob'
    AND length(configuration_fingerprint) = 32
  ),
  CHECK (
    typeof(state_digest_version) = 'integer'
    AND state_digest_version = 1
  ),
  CHECK (
    typeof(lookup_key_version) = 'integer'
    AND lookup_key_version BETWEEN 1 AND 2147483647
  ),
  CHECK (
    typeof(state_lookup_digest) = 'blob'
    AND length(state_lookup_digest) = 32
  ),
  CHECK (
    typeof(created_at) = 'text'
    AND length(created_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at
  ),
  CHECK (
    typeof(expires_at) = 'text'
    AND length(expires_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', expires_at) IS expires_at
  ),
  CHECK (
    CAST(strftime('%s', expires_at) AS INTEGER)
      = CAST(strftime('%s', created_at) AS INTEGER) + 600
  ),
  CHECK (
    typeof(lifecycle) = 'text'
    AND lifecycle IN ('prepared', 'consumed', 'expired', 'invalidated')
  ),
  CHECK (
    claimed_at IS NULL
    OR (
      typeof(claimed_at) = 'text'
      AND length(claimed_at) = 25
      AND strftime('%Y-%m-%dT%H:%M:%S+00:00', claimed_at) IS claimed_at
    )
  ),
  CHECK (
    terminal_at IS NULL
    OR (
      typeof(terminal_at) = 'text'
      AND length(terminal_at) = 25
      AND strftime('%Y-%m-%dT%H:%M:%S+00:00', terminal_at) IS terminal_at
    )
  ),
  CHECK (typeof(row_version) = 'integer' AND row_version IN (1, 2)),
  CHECK (
    (
      lifecycle = 'prepared'
      AND row_version = 1
      AND claimed_at IS NULL
      AND terminal_at IS NULL
    )
    OR (
      lifecycle = 'consumed'
      AND row_version = 2
      AND claimed_at IS NOT NULL
      AND terminal_at IS claimed_at
      AND claimed_at >= created_at
      AND claimed_at < expires_at
    )
    OR (
      lifecycle = 'expired'
      AND row_version = 2
      AND claimed_at IS NULL
      AND terminal_at IS NOT NULL
      AND terminal_at >= expires_at
    )
    OR (
      lifecycle = 'invalidated'
      AND row_version = 2
      AND claimed_at IS NULL
      AND terminal_at IS NOT NULL
      AND terminal_at >= created_at
    )
  ),
  CHECK (
    typeof(protection_envelope_version) = 'integer'
    AND protection_envelope_version = 1
  ),
  CHECK (
    typeof(protection_key_version) = 'integer'
    AND protection_key_version BETWEEN 1 AND 2147483647
  ),
  CHECK (
    typeof(protection_nonce) = 'blob'
    AND length(protection_nonce) = 12
  ),
  CHECK (
    typeof(protected_material) = 'blob'
    AND length(protected_material) BETWEEN 17 AND 528
  )
);

CREATE UNIQUE INDEX uq_google_oidc_authorization_transactions_state_lookup
ON google_oidc_authorization_transactions(
  lookup_key_version,
  state_lookup_digest
);

CREATE UNIQUE INDEX uq_google_oidc_authorization_transactions_protection_nonce
ON google_oidc_authorization_transactions(
  protection_key_version,
  protection_nonce
);

CREATE INDEX idx_google_oidc_authorization_transactions_prepared_expiry
ON google_oidc_authorization_transactions(expires_at, transaction_id)
WHERE lifecycle = 'prepared' AND row_version = 1;

CREATE INDEX idx_google_oidc_authorization_transactions_terminal_cleanup
ON google_oidc_authorization_transactions(terminal_at, transaction_id)
WHERE row_version = 2
  AND lifecycle IN ('consumed', 'expired', 'invalidated');

CREATE TRIGGER trg_google_oidc_authorization_transactions_insert_guard
BEFORE INSERT ON google_oidc_authorization_transactions
BEGIN
  SELECT CASE WHEN NOT (
    NEW.lifecycle = 'prepared'
    AND NEW.row_version = 1
    AND NEW.claimed_at IS NULL
    AND NEW.terminal_at IS NULL
  ) THEN RAISE(ABORT, 'google oidc authorization transaction must be inserted prepared') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM google_oidc_authorization_transactions
    WHERE transaction_id = NEW.transaction_id
      OR (
        lookup_key_version = NEW.lookup_key_version
        AND state_lookup_digest = NEW.state_lookup_digest
      )
      OR (
        protection_key_version = NEW.protection_key_version
        AND protection_nonce = NEW.protection_nonce
      )
  ) THEN RAISE(ABORT, 'google oidc authorization transaction replacement is forbidden') END;
END;

CREATE TRIGGER trg_google_oidc_authorization_transactions_update_guard
BEFORE UPDATE ON google_oidc_authorization_transactions
BEGIN
  SELECT CASE WHEN (
    NEW.transaction_id IS NOT OLD.transaction_id
    OR NEW.record_version IS NOT OLD.record_version
    OR NEW.provider IS NOT OLD.provider
    OR NEW.environment_namespace IS NOT OLD.environment_namespace
    OR NEW.configuration_fingerprint IS NOT OLD.configuration_fingerprint
    OR NEW.state_digest_version IS NOT OLD.state_digest_version
    OR NEW.lookup_key_version IS NOT OLD.lookup_key_version
    OR NEW.state_lookup_digest IS NOT OLD.state_lookup_digest
    OR NEW.created_at IS NOT OLD.created_at
    OR NEW.expires_at IS NOT OLD.expires_at
    OR NEW.protection_envelope_version IS NOT OLD.protection_envelope_version
    OR NEW.protection_key_version IS NOT OLD.protection_key_version
    OR NEW.protection_nonce IS NOT OLD.protection_nonce
    OR NEW.protected_material IS NOT OLD.protected_material
  ) THEN RAISE(ABORT, 'google oidc authorization transaction identity is immutable') END;
  SELECT CASE WHEN NOT (
    OLD.lifecycle = 'prepared'
    AND OLD.row_version = 1
    AND OLD.claimed_at IS NULL
    AND OLD.terminal_at IS NULL
    AND NEW.lifecycle IN ('consumed', 'expired', 'invalidated')
    AND NEW.row_version = 2
    AND NEW.terminal_at IS NOT NULL
    AND (
      (
        NEW.lifecycle = 'consumed'
        AND NEW.claimed_at IS NOT NULL
        AND NEW.terminal_at IS NEW.claimed_at
      )
      OR (
        NEW.lifecycle IN ('expired', 'invalidated')
        AND NEW.claimed_at IS NULL
      )
    )
  ) THEN RAISE(ABORT, 'google oidc authorization transaction transition is invalid') END;
END;

CREATE TRIGGER trg_google_oidc_authorization_transactions_delete_guard
BEFORE DELETE ON google_oidc_authorization_transactions
WHEN NOT (
  OLD.lifecycle IN ('consumed', 'expired', 'invalidated')
  AND OLD.row_version = 2
  AND OLD.terminal_at IS NOT NULL
)
BEGIN
  SELECT RAISE(ABORT, 'prepared google oidc authorization transaction cannot be deleted');
END;
