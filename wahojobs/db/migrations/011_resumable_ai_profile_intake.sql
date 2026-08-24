DROP TRIGGER trg_ai_profile_import_entitlements_update_guard;
DROP TRIGGER trg_ai_profile_import_attempts_update_guard;

CREATE TABLE ai_profile_intake_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  environment_namespace TEXT NOT NULL,
  account_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  checkpoint_schema_version TEXT NOT NULL,
  review_schema_version TEXT NOT NULL,
  state TEXT NOT NULL,
  row_version INTEGER NOT NULL,
  review_payload_json TEXT NOT NULL,
  review_payload_sha256 TEXT NOT NULL,
  source_metadata_json TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL,
  latest_event_version INTEGER NOT NULL,
  latest_event_id TEXT NOT NULL,
  lineage_sha256 TEXT NOT NULL,
  reservation_generation INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  review_saved_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,

  UNIQUE (environment_namespace, account_id),
  FOREIGN KEY (account_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  FOREIGN KEY (principal_id, environment_namespace)
    REFERENCES product_principals(principal_id, environment_namespace) ON DELETE RESTRICT,
  FOREIGN KEY (binding_id, principal_id, account_id)
    REFERENCES principal_account_bindings(binding_id, principal_id, user_id) ON DELETE RESTRICT,
  CHECK (length(checkpoint_id) = 36),
  CHECK (substr(checkpoint_id, 1, 4) = 'aic_'),
  CHECK (substr(checkpoint_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(checkpoint_id, 5) <> replace(printf('%32s', ''), ' ', substr(checkpoint_id, 5, 1))),
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (checkpoint_schema_version = 'profile_intake_checkpoint_v1'),
  CHECK (review_schema_version = 'ai_profile_review_draft_v2'),
  CHECK (state = 'active'),
  CHECK (row_version >= 1),
  CHECK (length(CAST(review_payload_json AS BLOB)) BETWEEN 2 AND 65536),
  CHECK (json_valid(review_payload_json) AND json_type(review_payload_json) = 'object'),
  CHECK (length(review_payload_sha256) = 64),
  CHECK (review_payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(CAST(source_metadata_json AS BLOB)) BETWEEN 2 AND 4096),
  CHECK (json_valid(source_metadata_json) AND json_type(source_metadata_json) = 'object'),
  CHECK (length(binding_id) = 36 AND substr(binding_id, 1, 4) = 'pab_' AND substr(binding_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (binding_version >= 1 AND latest_event_version = binding_version),
  CHECK (length(latest_event_id) = 36 AND substr(latest_event_id, 1, 4) = 'obe_' AND substr(latest_event_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(lineage_sha256) = 64 AND lineage_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (reservation_generation >= 1),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(review_saved_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', review_saved_at) IS review_saved_at),
  CHECK (length(updated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at),
  CHECK (length(expires_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', expires_at) IS expires_at),
  CHECK (julianday(review_saved_at) >= julianday(created_at)),
  CHECK (julianday(updated_at) >= julianday(review_saved_at)),
  CHECK (abs(julianday(expires_at) - julianday(review_saved_at) - 7.0) < 0.000001)
);

CREATE INDEX idx_ai_profile_intake_checkpoints_expiry
ON ai_profile_intake_checkpoints(state, expires_at);

CREATE INDEX idx_ai_profile_intake_checkpoints_principal
ON ai_profile_intake_checkpoints(principal_id, environment_namespace);

CREATE TRIGGER trg_ai_profile_intake_checkpoints_insert_guard
BEFORE INSERT ON ai_profile_intake_checkpoints
BEGIN
  SELECT CASE WHEN NEW.row_version <> 1
    OR NEW.created_at <> NEW.review_saved_at
    OR NEW.review_saved_at <> NEW.updated_at
    THEN RAISE(ABORT, 'AI intake checkpoint initial version is invalid') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM principal_account_bindings binding
    WHERE binding.binding_id = NEW.binding_id
      AND binding.principal_id = NEW.principal_id
      AND binding.user_id = NEW.account_id
      AND binding.environment_namespace = NEW.environment_namespace
      AND binding.binding_status = 'active'
      AND binding.binding_role = 'owner'
      AND binding.version = NEW.binding_version
      AND binding.latest_event_version = NEW.latest_event_version
  ) THEN RAISE(ABORT, 'AI intake checkpoint ownership is invalid') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM product_profiles profile
    WHERE profile.principal_id = NEW.principal_id
      AND profile.environment_namespace = NEW.environment_namespace
  ) THEN RAISE(ABORT, 'AI intake checkpoint cannot outlive profile creation') END;
END;

CREATE TRIGGER trg_ai_profile_intake_checkpoints_update_guard
BEFORE UPDATE ON ai_profile_intake_checkpoints
BEGIN
  SELECT CASE WHEN NEW.checkpoint_id <> OLD.checkpoint_id
    OR NEW.environment_namespace <> OLD.environment_namespace
    OR NEW.account_id <> OLD.account_id
    OR NEW.principal_id <> OLD.principal_id
    OR NEW.checkpoint_schema_version <> OLD.checkpoint_schema_version
    OR NEW.review_schema_version <> OLD.review_schema_version
    OR NEW.state <> OLD.state
    OR NEW.source_metadata_json <> OLD.source_metadata_json
    OR NEW.binding_id <> OLD.binding_id
    OR NEW.binding_version <> OLD.binding_version
    OR NEW.latest_event_version <> OLD.latest_event_version
    OR NEW.latest_event_id <> OLD.latest_event_id
    OR NEW.lineage_sha256 <> OLD.lineage_sha256
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'AI intake checkpoint identity is immutable') END;
  SELECT CASE WHEN NEW.row_version <> OLD.row_version + 1
    OR julianday(NEW.updated_at) < julianday(OLD.updated_at)
    OR NEW.reservation_generation < OLD.reservation_generation
    THEN RAISE(ABORT, 'AI intake checkpoint version transition is invalid') END;
  SELECT CASE WHEN NEW.review_payload_json = OLD.review_payload_json AND (
      NEW.review_payload_sha256 <> OLD.review_payload_sha256
      OR NEW.review_saved_at <> OLD.review_saved_at
      OR NEW.expires_at <> OLD.expires_at
    ) THEN RAISE(ABORT, 'AI intake checkpoint unchanged payload is invalid') END;
  SELECT CASE WHEN NEW.review_payload_json <> OLD.review_payload_json AND (
      NEW.review_payload_sha256 = OLD.review_payload_sha256
      OR julianday(NEW.review_saved_at) < julianday(OLD.review_saved_at)
      OR abs(julianday(NEW.expires_at) - julianday(NEW.review_saved_at) - 7.0) >= 0.000001
    ) THEN RAISE(ABORT, 'AI intake checkpoint retention transition is invalid') END;
END;

CREATE TRIGGER trg_ai_profile_import_entitlements_update_guard
BEFORE UPDATE ON ai_profile_import_entitlements
BEGIN
  SELECT CASE WHEN NEW.environment_namespace <> OLD.environment_namespace
    OR NEW.account_id <> OLD.account_id
    OR NEW.entitlement_code <> OLD.entitlement_code
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'AI import entitlement identity is immutable') END;
  SELECT CASE WHEN NOT (
    (OLD.state = 'available' AND NEW.state = 'reserved')
    OR (OLD.state = 'reserved' AND NEW.state = 'reserved')
    OR (OLD.state = 'reserved' AND NEW.state IN ('available', 'consumed'))
  ) THEN RAISE(ABORT, 'AI import entitlement transition is invalid') END;
  SELECT CASE WHEN OLD.state = 'reserved' AND NEW.state = 'reserved' AND (
    NEW.reservation_id <> OLD.reservation_id
    OR NEW.attempt_id <> OLD.attempt_id
    OR NEW.lease_expires_at <= OLD.lease_expires_at
    OR julianday(NEW.updated_at) <= julianday(OLD.updated_at)
    OR NEW.consumed_at IS NOT OLD.consumed_at
  ) THEN RAISE(ABORT, 'AI import reservation renewal is invalid') END;
  SELECT CASE WHEN NEW.state = 'reserved' AND NOT EXISTS (
    SELECT 1 FROM ai_profile_import_attempts attempt
    WHERE attempt.attempt_id = NEW.attempt_id
      AND attempt.reservation_id = NEW.reservation_id
      AND attempt.environment_namespace = NEW.environment_namespace
      AND attempt.account_id = NEW.account_id
      AND attempt.entitlement_code = NEW.entitlement_code
      AND attempt.state = 'reserved'
      AND attempt.lease_expires_at = NEW.lease_expires_at
  ) THEN RAISE(ABORT, 'AI import reservation relationship is invalid') END;
  SELECT CASE WHEN NEW.state = 'available' AND NOT EXISTS (
    SELECT 1 FROM ai_profile_import_attempts attempt
    WHERE attempt.attempt_id = OLD.attempt_id
      AND attempt.reservation_id = OLD.reservation_id
      AND attempt.environment_namespace = NEW.environment_namespace
      AND attempt.account_id = NEW.account_id
      AND attempt.entitlement_code = NEW.entitlement_code
      AND attempt.state IN ('released', 'expired', 'failed')
  ) THEN RAISE(ABORT, 'AI import release relationship is invalid') END;
  SELECT CASE WHEN NEW.state = 'consumed' AND NOT EXISTS (
    SELECT 1 FROM ai_profile_import_attempts attempt
    WHERE attempt.attempt_id = NEW.attempt_id
      AND attempt.reservation_id = NEW.reservation_id
      AND attempt.environment_namespace = NEW.environment_namespace
      AND attempt.account_id = NEW.account_id
      AND attempt.entitlement_code = NEW.entitlement_code
      AND attempt.state = 'succeeded'
      AND attempt.result_code = 'success'
  ) THEN RAISE(ABORT, 'AI import consumption relationship is invalid') END;
END;

CREATE TRIGGER trg_ai_profile_import_attempts_update_guard
BEFORE UPDATE ON ai_profile_import_attempts
BEGIN
  SELECT CASE WHEN NEW.attempt_id <> OLD.attempt_id
    OR NEW.reservation_id <> OLD.reservation_id
    OR NEW.environment_namespace <> OLD.environment_namespace
    OR NEW.account_id <> OLD.account_id
    OR NEW.principal_id <> OLD.principal_id
    OR NEW.entitlement_code <> OLD.entitlement_code
    OR NEW.idempotency_key_sha256 <> OLD.idempotency_key_sha256
    OR NEW.request_fingerprint <> OLD.request_fingerprint
    OR NEW.source_metadata_json <> OLD.source_metadata_json
    OR NEW.binding_id <> OLD.binding_id
    OR NEW.binding_version <> OLD.binding_version
    OR NEW.latest_event_version <> OLD.latest_event_version
    OR NEW.latest_event_id <> OLD.latest_event_id
    OR NEW.lineage_sha256 <> OLD.lineage_sha256
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'AI import attempt identity is immutable') END;
  SELECT CASE WHEN NOT (
    (OLD.state = 'reserved' AND NEW.state = 'reserved'
      AND NEW.lease_expires_at > OLD.lease_expires_at
      AND julianday(NEW.updated_at) > julianday(OLD.updated_at)
      AND NEW.confirmation_fingerprint IS OLD.confirmation_fingerprint
      AND NEW.result_code IS OLD.result_code
      AND NEW.result_profile_id IS OLD.result_profile_id
      AND NEW.result_revision_id IS OLD.result_revision_id
      AND NEW.completed_at IS OLD.completed_at)
    OR (OLD.state = 'reserved' AND NEW.state IN ('released', 'expired', 'failed', 'succeeded')
      AND NEW.lease_expires_at = OLD.lease_expires_at)
  ) THEN RAISE(ABORT, 'AI import attempt transition is invalid') END;
  SELECT CASE WHEN julianday(NEW.lease_expires_at) > julianday(NEW.created_at) + (2.0 / 24.0) + 0.000001
    THEN RAISE(ABORT, 'AI import reservation generation exceeded') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM ai_profile_import_entitlements entitlement
    WHERE entitlement.environment_namespace = OLD.environment_namespace
      AND entitlement.account_id = OLD.account_id
      AND entitlement.entitlement_code = OLD.entitlement_code
      AND entitlement.state = 'reserved'
      AND entitlement.attempt_id = OLD.attempt_id
      AND entitlement.reservation_id = OLD.reservation_id
  ) THEN RAISE(ABORT, 'AI import attempt entitlement relationship is invalid') END;
END;

CREATE TRIGGER trg_ai_profile_import_attempts_generation_limit
BEFORE INSERT ON ai_profile_import_attempts
WHEN julianday(NEW.lease_expires_at) > julianday(NEW.created_at) + (2.0 / 24.0) + 0.000001
BEGIN
  SELECT RAISE(ABORT, 'AI import reservation generation exceeded');
END;
