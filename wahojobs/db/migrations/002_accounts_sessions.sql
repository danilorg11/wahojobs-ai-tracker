CREATE TABLE users (
  user_id TEXT PRIMARY KEY,
  lifecycle_status TEXT NOT NULL,
  row_version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deletion_requested_at TEXT,
  deactivated_at TEXT,

  CHECK (lifecycle_status IN ('active', 'suspended', 'deletion_requested', 'deactivated_pending_purge')),
  CHECK (row_version >= 1),
  CHECK (length(user_id) >= 36),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(updated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at),
  CHECK (julianday(updated_at) >= julianday(created_at)),
  CHECK (deletion_requested_at IS NULL OR (length(deletion_requested_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', deletion_requested_at) IS deletion_requested_at AND julianday(deletion_requested_at) >= julianday(created_at))),
  CHECK (deactivated_at IS NULL OR (length(deactivated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', deactivated_at) IS deactivated_at AND julianday(deactivated_at) >= julianday(deletion_requested_at))),
  CHECK (
    (lifecycle_status IN ('active', 'suspended') AND deletion_requested_at IS NULL AND deactivated_at IS NULL)
    OR (lifecycle_status = 'deletion_requested' AND deletion_requested_at IS NOT NULL AND deactivated_at IS NULL)
    OR (lifecycle_status = 'deactivated_pending_purge' AND deletion_requested_at IS NOT NULL AND deactivated_at IS NOT NULL)
  )
);

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
  CHECK (provider IN ('google')),
  CHECK (email_verified IN (0, 1)),
  CHECK (email_verified = 0 OR verified_email IS NOT NULL),
  CHECK (length(provider_subject) BETWEEN 1 AND 1024),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(last_authenticated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', last_authenticated_at) IS last_authenticated_at),
  CHECK (disabled_at IS NULL OR (length(disabled_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', disabled_at) IS disabled_at AND julianday(disabled_at) >= julianday(created_at))),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE account_invitations (
  invitation_id TEXT PRIMARY KEY,
  invited_email_hmac TEXT NOT NULL,
  invitation_secret_hmac TEXT NOT NULL,
  hash_version TEXT NOT NULL,
  email_display_hint TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT,
  revoked_at TEXT,
  consumed_by_user_id TEXT,
  invitation_status TEXT NOT NULL,
  created_by TEXT NOT NULL,
  source_metadata_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,

  FOREIGN KEY (consumed_by_user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  CHECK (hash_version IN ('hmac_sha256_v1')),
  CHECK (invitation_status IN ('pending', 'consumed', 'revoked')),
  CHECK (length(invited_email_hmac) = 64 AND invited_email_hmac = lower(invited_email_hmac) AND invited_email_hmac NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(invitation_secret_hmac) = 64 AND invitation_secret_hmac = lower(invitation_secret_hmac) AND invitation_secret_hmac NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(expires_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', expires_at) IS expires_at AND julianday(expires_at) > julianday(created_at)),
  CHECK (consumed_at IS NULL OR (length(consumed_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', consumed_at) IS consumed_at AND julianday(consumed_at) >= julianday(created_at) AND julianday(consumed_at) < julianday(expires_at))),
  CHECK (revoked_at IS NULL OR (length(revoked_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', revoked_at) IS revoked_at AND julianday(revoked_at) >= julianday(created_at))),
  CHECK (length(source_metadata_json) <= 4096 AND json_valid(source_metadata_json)),
  CHECK (
    (invitation_status = 'pending' AND consumed_at IS NULL AND revoked_at IS NULL AND consumed_by_user_id IS NULL)
    OR (invitation_status = 'consumed' AND consumed_at IS NOT NULL AND revoked_at IS NULL AND consumed_by_user_id IS NOT NULL)
    OR (invitation_status = 'revoked' AND consumed_at IS NULL AND revoked_at IS NOT NULL AND consumed_by_user_id IS NULL)
  )
);

CREATE TABLE account_sessions (
  session_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  token_hash_version TEXT NOT NULL,
  csrf_secret_hash TEXT NOT NULL UNIQUE,
  csrf_hash_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  idle_expires_at TEXT NOT NULL,
  absolute_expires_at TEXT NOT NULL,
  rotated_at TEXT,
  revoked_at TEXT,
  revoke_reason TEXT,
  session_version INTEGER NOT NULL,
  creation_idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,

  UNIQUE (user_id, session_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  CHECK (token_hash_version IN ('sha256_v1')),
  CHECK (csrf_hash_version IN ('sha256_v1')),
  CHECK (length(token_hash) = 64 AND token_hash = lower(token_hash) AND token_hash NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(csrf_secret_hash) = 64 AND csrf_secret_hash = lower(csrf_secret_hash) AND csrf_secret_hash NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (session_version >= 1),
  CHECK (revoke_reason IS NULL OR revoke_reason IN ('account_deactivation_requested', 'account_suspended', 'explicit_revoke', 'security_reset', 'session_rotated', 'stale', 'user_logout')),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(last_seen_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', last_seen_at) IS last_seen_at AND julianday(last_seen_at) >= julianday(created_at)),
  CHECK (length(idle_expires_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', idle_expires_at) IS idle_expires_at AND julianday(idle_expires_at) > julianday(created_at)),
  CHECK (length(absolute_expires_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', absolute_expires_at) IS absolute_expires_at AND julianday(absolute_expires_at) > julianday(created_at)),
  CHECK (julianday(idle_expires_at) <= julianday(absolute_expires_at)),
  CHECK (julianday(last_seen_at) < julianday(idle_expires_at) AND julianday(last_seen_at) < julianday(absolute_expires_at)),
  CHECK (rotated_at IS NULL OR (length(rotated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', rotated_at) IS rotated_at AND julianday(rotated_at) >= julianday(created_at) AND julianday(rotated_at) < julianday(absolute_expires_at))),
  CHECK (revoked_at IS NULL OR (length(revoked_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', revoked_at) IS revoked_at AND julianday(revoked_at) >= julianday(created_at))),
  CHECK (rotated_at IS NULL OR (revoked_at IS NOT NULL AND revoke_reason = 'session_rotated' AND julianday(rotated_at) = julianday(revoked_at)))
);

CREATE TABLE account_session_rotations (
  rotation_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  predecessor_session_id TEXT NOT NULL UNIQUE,
  replacement_session_id TEXT NOT NULL UNIQUE,
  rotated_at TEXT NOT NULL,
  created_at TEXT NOT NULL,

  FOREIGN KEY (user_id, predecessor_session_id) REFERENCES account_sessions(user_id, session_id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, replacement_session_id) REFERENCES account_sessions(user_id, session_id) ON DELETE RESTRICT,
  CHECK (predecessor_session_id <> replacement_session_id),
  CHECK (length(rotated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', rotated_at) IS rotated_at),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (julianday(created_at) >= julianday(rotated_at))
);

CREATE TABLE consent_events (
  consent_event_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  purpose TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  action TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  source TEXT NOT NULL,
  consent_version_before INTEGER NOT NULL,
  consent_version_after INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,

  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  UNIQUE (user_id, purpose, consent_version_after),
  CHECK (purpose IN ('profile_storage', 'product_terms', 'privacy_policy')),
  CHECK (action IN ('granted', 'revoked')),
  CHECK (length(policy_version) BETWEEN 1 AND 128),
  CHECK (consent_version_before >= 0 AND consent_version_after = consent_version_before + 1),
  CHECK (length(occurred_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', occurred_at) IS occurred_at),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(metadata_json) <= 4096 AND json_valid(metadata_json))
);

CREATE TABLE account_lifecycle_events (
  lifecycle_event_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  source TEXT NOT NULL,
  account_version_before INTEGER NOT NULL,
  account_version_after INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,

  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  UNIQUE (user_id, account_version_after),
  CHECK (event_type IN ('account_created', 'account_suspended', 'account_reactivated', 'deletion_requested', 'deletion_cancelled', 'account_deactivated_pending_purge')),
  CHECK (account_version_before >= 0 AND account_version_after = account_version_before + 1),
  CHECK (length(occurred_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', occurred_at) IS occurred_at),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(metadata_json) <= 4096 AND json_valid(metadata_json))
);

CREATE TABLE account_deletion_requests (
  deletion_request_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  cooling_period_ends_at TEXT NOT NULL,
  purge_eligible_at TEXT NOT NULL,
  cancelled_at TEXT,
  deactivated_at TEXT,
  status TEXT NOT NULL,
  request_source TEXT NOT NULL,
  restore_lifecycle_status TEXT NOT NULL,
  deactivation_evidence_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,

  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  CHECK (status IN ('pending_cooling', 'cancelled', 'deactivated_pending_purge')),
  CHECK (restore_lifecycle_status IN ('active', 'suspended')),
  CHECK (length(requested_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', requested_at) IS requested_at),
  CHECK (length(cooling_period_ends_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', cooling_period_ends_at) IS cooling_period_ends_at AND julianday(cooling_period_ends_at) > julianday(requested_at)),
  CHECK (length(purge_eligible_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', purge_eligible_at) IS purge_eligible_at AND julianday(purge_eligible_at) >= julianday(cooling_period_ends_at)),
  CHECK (cancelled_at IS NULL OR (length(cancelled_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', cancelled_at) IS cancelled_at AND julianday(cancelled_at) >= julianday(requested_at))),
  CHECK (deactivated_at IS NULL OR (length(deactivated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', deactivated_at) IS deactivated_at AND julianday(deactivated_at) >= julianday(cooling_period_ends_at))),
  CHECK (length(deactivation_evidence_json) <= 4096 AND json_valid(deactivation_evidence_json)),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (
    (status = 'pending_cooling' AND cancelled_at IS NULL AND deactivated_at IS NULL)
    OR (status = 'cancelled' AND cancelled_at IS NOT NULL AND deactivated_at IS NULL)
    OR (status = 'deactivated_pending_purge' AND cancelled_at IS NULL AND deactivated_at IS NOT NULL)
  )
);

CREATE INDEX idx_auth_identities_user ON auth_identities(user_id);
CREATE INDEX idx_account_invitations_status ON account_invitations(invitation_status, expires_at);
CREATE INDEX idx_account_sessions_user_active ON account_sessions(user_id, revoked_at, absolute_expires_at);
CREATE INDEX idx_account_session_rotations_user_time ON account_session_rotations(user_id, rotated_at);
CREATE INDEX idx_consent_events_user_purpose ON consent_events(user_id, purpose, consent_version_after);
CREATE INDEX idx_account_lifecycle_events_user_version ON account_lifecycle_events(user_id, account_version_after);
CREATE INDEX idx_account_deletion_requests_user ON account_deletion_requests(user_id, requested_at);
CREATE UNIQUE INDEX idx_account_deletion_requests_one_open ON account_deletion_requests(user_id) WHERE status IN ('pending_cooling', 'deactivated_pending_purge');

CREATE TRIGGER trg_auth_identities_immutable_identity
BEFORE UPDATE OF user_id, provider, provider_subject ON auth_identities
BEGIN
  SELECT RAISE(ABORT, 'authentication identity is immutable');
END;

CREATE TRIGGER trg_users_created_at_immutable
BEFORE UPDATE OF user_id, created_at ON users
BEGIN
  SELECT RAISE(ABORT, 'user identity and creation time are immutable');
END;

CREATE TRIGGER trg_account_sessions_user_time_guard
BEFORE INSERT ON account_sessions
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM users owner
    WHERE owner.user_id = NEW.user_id
      AND julianday(NEW.created_at) >= julianday(owner.created_at)
  ) THEN RAISE(ABORT, 'invalid session temporal boundary') END;
END;

CREATE TRIGGER trg_account_sessions_core_immutable
BEFORE UPDATE OF session_id, user_id, created_at ON account_sessions
BEGIN
  SELECT RAISE(ABORT, 'session identity and creation time are immutable');
END;

CREATE TRIGGER trg_account_sessions_rotation_state_guard
BEFORE UPDATE OF rotated_at, revoked_at, revoke_reason ON account_sessions
BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM account_session_rotations edge
    WHERE edge.predecessor_session_id = OLD.session_id
      AND (NEW.rotated_at IS NOT edge.rotated_at
        OR NEW.revoked_at IS NOT edge.rotated_at
        OR NEW.revoke_reason IS NOT 'session_rotated')
  ) THEN RAISE(ABORT, 'session rotation state is immutable') END;
END;

CREATE TRIGGER trg_account_session_rotations_insert_guard
BEFORE INSERT ON account_session_rotations
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM account_sessions predecessor
    JOIN account_sessions replacement
      ON replacement.session_id = NEW.replacement_session_id
     AND replacement.user_id = NEW.user_id
    WHERE predecessor.session_id = NEW.predecessor_session_id
      AND predecessor.user_id = NEW.user_id
      AND predecessor.session_id <> replacement.session_id
      AND julianday(replacement.created_at) >= julianday(predecessor.created_at)
      AND julianday(NEW.rotated_at) >= julianday(predecessor.created_at)
      AND julianday(NEW.rotated_at) >= julianday(replacement.created_at)
      AND predecessor.rotated_at IS NEW.rotated_at
      AND predecessor.revoked_at IS NEW.rotated_at
      AND predecessor.revoke_reason = 'session_rotated'
      AND replacement.rotated_at IS NULL
      AND replacement.revoked_at IS NULL
  ) THEN RAISE(ABORT, 'invalid session rotation edge') END;
  SELECT CASE WHEN EXISTS (
    WITH RECURSIVE descendants(session_id) AS (
      SELECT NEW.replacement_session_id
      UNION
      SELECT edge.replacement_session_id
      FROM account_session_rotations edge
      JOIN descendants ON edge.predecessor_session_id = descendants.session_id
    )
    SELECT 1 FROM descendants WHERE session_id = NEW.predecessor_session_id
  ) THEN RAISE(ABORT, 'session rotation cycle') END;
END;

CREATE TRIGGER trg_account_session_rotations_no_update
BEFORE UPDATE ON account_session_rotations
BEGIN
  SELECT RAISE(ABORT, 'account_session_rotations is append-only');
END;

CREATE TRIGGER trg_account_session_rotations_no_delete
BEFORE DELETE ON account_session_rotations
BEGIN
  SELECT RAISE(ABORT, 'account_session_rotations is append-only');
END;

CREATE TRIGGER trg_account_invitations_consumption_time_guard
BEFORE UPDATE OF invitation_status, consumed_at, consumed_by_user_id ON account_invitations
WHEN NEW.invitation_status = 'consumed'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM users owner
    WHERE owner.user_id = NEW.consumed_by_user_id
      AND julianday(NEW.consumed_at) >= julianday(owner.created_at)
  ) THEN RAISE(ABORT, 'invitation consumption predates user') END;
END;

CREATE TRIGGER trg_consent_events_user_time_guard
BEFORE INSERT ON consent_events
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM users owner
    WHERE owner.user_id = NEW.user_id
      AND julianday(NEW.occurred_at) >= julianday(owner.created_at)
  ) THEN RAISE(ABORT, 'consent event predates user') END;
END;

CREATE TRIGGER trg_account_lifecycle_events_user_time_guard
BEFORE INSERT ON account_lifecycle_events
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM users owner
    WHERE owner.user_id = NEW.user_id
      AND julianday(NEW.occurred_at) >= julianday(owner.created_at)
  ) THEN RAISE(ABORT, 'lifecycle event predates user') END;
END;

CREATE TRIGGER trg_account_deletion_requests_user_time_guard
BEFORE INSERT ON account_deletion_requests
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM users owner
    WHERE owner.user_id = NEW.user_id
      AND julianday(NEW.requested_at) >= julianday(owner.created_at)
  ) THEN RAISE(ABORT, 'deletion request predates user') END;
END;

CREATE TRIGGER trg_consent_events_contiguous
BEFORE INSERT ON consent_events
BEGIN
  SELECT CASE WHEN
    (NOT EXISTS (SELECT 1 FROM consent_events WHERE user_id = NEW.user_id AND purpose = NEW.purpose)
      AND NOT (NEW.consent_version_before = 0 AND NEW.consent_version_after = 1))
    OR
    (EXISTS (SELECT 1 FROM consent_events WHERE user_id = NEW.user_id AND purpose = NEW.purpose)
      AND NEW.consent_version_before <> (SELECT MAX(consent_version_after) FROM consent_events WHERE user_id = NEW.user_id AND purpose = NEW.purpose))
    OR
    (EXISTS (SELECT 1 FROM consent_events WHERE user_id = NEW.user_id AND purpose = NEW.purpose)
      AND julianday(NEW.occurred_at) < (SELECT julianday(occurred_at) FROM consent_events WHERE user_id = NEW.user_id AND purpose = NEW.purpose ORDER BY consent_version_after DESC LIMIT 1))
  THEN RAISE(ABORT, 'consent history must be contiguous') END;
END;

CREATE TRIGGER trg_account_lifecycle_events_contiguous
BEFORE INSERT ON account_lifecycle_events
BEGIN
  SELECT CASE WHEN
    (NOT EXISTS (SELECT 1 FROM account_lifecycle_events WHERE user_id = NEW.user_id)
      AND NOT (NEW.event_type = 'account_created' AND NEW.account_version_before = 0 AND NEW.account_version_after = 1
        AND (SELECT row_version FROM users WHERE user_id = NEW.user_id) = 1))
    OR
    (EXISTS (SELECT 1 FROM account_lifecycle_events WHERE user_id = NEW.user_id)
      AND (NEW.account_version_before <> (SELECT MAX(account_version_after) FROM account_lifecycle_events WHERE user_id = NEW.user_id)
        OR NEW.account_version_before <> (SELECT row_version FROM users WHERE user_id = NEW.user_id)))
    OR
    (EXISTS (SELECT 1 FROM account_lifecycle_events WHERE user_id = NEW.user_id)
      AND julianday(NEW.occurred_at) < (SELECT julianday(occurred_at) FROM account_lifecycle_events WHERE user_id = NEW.user_id ORDER BY account_version_after DESC LIMIT 1))
  THEN RAISE(ABORT, 'account lifecycle must be contiguous') END;
END;

CREATE TRIGGER trg_consent_events_no_update BEFORE UPDATE ON consent_events
BEGIN
  SELECT RAISE(ABORT, 'consent_events is append-only');
END;

CREATE TRIGGER trg_consent_events_no_delete BEFORE DELETE ON consent_events
BEGIN
  SELECT RAISE(ABORT, 'consent_events is append-only');
END;

CREATE TRIGGER trg_account_lifecycle_events_no_update BEFORE UPDATE ON account_lifecycle_events
BEGIN
  SELECT RAISE(ABORT, 'account_lifecycle_events is append-only');
END;

CREATE TRIGGER trg_account_lifecycle_events_no_delete BEFORE DELETE ON account_lifecycle_events
BEGIN
  SELECT RAISE(ABORT, 'account_lifecycle_events is append-only');
END;
