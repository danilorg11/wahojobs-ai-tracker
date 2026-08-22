DROP TRIGGER trg_product_profile_sources_insert_guard;
DROP TRIGGER trg_product_profile_sources_no_update;
DROP TRIGGER trg_product_profile_sources_delete_guard;
DROP INDEX idx_product_profile_sources_revision;
DROP INDEX idx_product_profile_sources_profile;

ALTER TABLE product_profile_sources RENAME TO product_profile_sources_m010_backup;

CREATE TABLE product_profile_sources (
  source_id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  environment_namespace TEXT NOT NULL,
  source_ordinal INTEGER NOT NULL,
  source_type TEXT NOT NULL,
  source_format TEXT NOT NULL,
  source_content TEXT NOT NULL,
  source_content_sha256 TEXT NOT NULL,
  source_schema_version TEXT NOT NULL,
  parser_version TEXT,
  accepted_at TEXT NOT NULL,

  UNIQUE (revision_id, source_ordinal),
  UNIQUE (source_id, revision_id),
  FOREIGN KEY (revision_id, profile_id, principal_id, environment_namespace)
    REFERENCES product_profile_revisions(revision_id, profile_id, principal_id, environment_namespace)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
  CHECK (length(source_id) = 36),
  CHECK (substr(source_id, 1, 4) = 'pfs_'),
  CHECK (substr(source_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(source_id, 5) <> replace(printf('%32s', ''), ' ', substr(source_id, 5, 1))),
  CHECK (source_ordinal BETWEEN 1 AND 16),
  CHECK (source_type IN (
    'confirmed_about_you_text',
    'user_confirmed_correction',
    'confirmed_lifecycle_action',
    'user_confirmed_ai_import'
  )),
  CHECK (
    (source_type = 'confirmed_about_you_text' AND source_format = 'text/plain')
    OR (source_type = 'user_confirmed_correction' AND source_format = 'application/json')
    OR (source_type = 'confirmed_lifecycle_action' AND source_format = 'application/json')
    OR (source_type = 'user_confirmed_ai_import' AND source_format = 'application/json')
  ),
  CHECK (
    source_type <> 'confirmed_lifecycle_action'
    OR (
      source_schema_version = 'confirmed_lifecycle_action_v1'
      AND source_content IN (
        '{"action":"archive","schema_version":"confirmed_lifecycle_action_v1"}',
        '{"action":"reactivate","schema_version":"confirmed_lifecycle_action_v1"}',
        '{"action":"deletion_request","schema_version":"confirmed_lifecycle_action_v1"}'
      )
    )
  ),
  CHECK (
    source_type <> 'user_confirmed_ai_import'
    OR source_schema_version = 'user_confirmed_ai_import_v1'
  ),
  CHECK (length(CAST(source_content AS BLOB)) BETWEEN 1 AND 32768),
  CHECK (instr(source_content, char(0)) = 0),
  CHECK (source_format <> 'application/json' OR json_valid(source_content)),
  CHECK (source_format <> 'application/json' OR json_type(source_content) = 'object'),
  CHECK (length(source_content_sha256) = 64),
  CHECK (source_content_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (source_content_sha256 <> replace(printf('%64s', ''), ' ', substr(source_content_sha256, 1, 1))),
  CHECK (length(source_schema_version) BETWEEN 1 AND 64),
  CHECK (source_schema_version = lower(source_schema_version)),
  CHECK (source_schema_version NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (parser_version IS NULL OR (
    length(parser_version) BETWEEN 1 AND 64
    AND parser_version = lower(parser_version)
    AND parser_version NOT GLOB '*[^a-z0-9_.-]*'
  )),
  CHECK (length(accepted_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', accepted_at) IS accepted_at)
);

INSERT INTO product_profile_sources (
  source_id, revision_id, profile_id, principal_id, environment_namespace,
  source_ordinal, source_type, source_format, source_content,
  source_content_sha256, source_schema_version, parser_version, accepted_at
)
SELECT
  source_id, revision_id, profile_id, principal_id, environment_namespace,
  source_ordinal, source_type, source_format, source_content,
  source_content_sha256, source_schema_version, parser_version, accepted_at
FROM product_profile_sources_m010_backup;

DROP TABLE product_profile_sources_m010_backup;

CREATE INDEX idx_product_profile_sources_revision
ON product_profile_sources(revision_id, source_ordinal);

CREATE INDEX idx_product_profile_sources_profile
ON product_profile_sources(profile_id, accepted_at);

CREATE TRIGGER trg_product_profile_sources_insert_guard
BEFORE INSERT ON product_profile_sources
BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM product_profile_revisions revision
    WHERE revision.revision_id = NEW.revision_id
  ) THEN RAISE(ABORT, 'profile revision source bundle is sealed') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM product_profiles profile
    WHERE profile.profile_id = NEW.profile_id
      AND profile.principal_id = NEW.principal_id
      AND profile.environment_namespace = NEW.environment_namespace
      AND julianday(NEW.accepted_at) >= julianday(profile.created_at)
  ) THEN RAISE(ABORT, 'profile source identity or time is invalid') END;
  SELECT CASE WHEN EXISTS (
    WITH RECURSIVE character_positions(position) AS (
      SELECT 1
      UNION ALL
      SELECT position + 1 FROM character_positions
      WHERE position < length(NEW.source_content)
    )
    SELECT 1 FROM character_positions
    WHERE (
      unicode(substr(NEW.source_content, position, 1)) BETWEEN 0 AND 31
      AND unicode(substr(NEW.source_content, position, 1)) NOT IN (9, 10, 13)
    ) OR unicode(substr(NEW.source_content, position, 1)) BETWEEN 127 AND 159
    LIMIT 1
  ) THEN RAISE(ABORT, 'profile source contains prohibited control characters') END;
  SELECT CASE WHEN (
    SELECT COUNT(*) FROM product_profile_sources
    WHERE revision_id = NEW.revision_id
  ) >= 16 THEN RAISE(ABORT, 'profile revision source limit exceeded') END;
  SELECT CASE WHEN NEW.source_type = 'user_confirmed_ai_import' AND (
    NEW.source_ordinal <> 1
    OR NOT EXISTS (
      SELECT 1 FROM product_profiles profile
      WHERE profile.profile_id = NEW.profile_id
        AND profile.initial_revision_id = NEW.revision_id
        AND profile.principal_id = NEW.principal_id
        AND profile.environment_namespace = NEW.environment_namespace
    )
    OR json_type(NEW.source_content) <> 'object'
    OR (SELECT COUNT(*) FROM json_each(NEW.source_content)) <> 8
    OR (SELECT COUNT(DISTINCT key) FROM json_each(NEW.source_content)) <> 8
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_content)
      WHERE key NOT IN (
        'schema_version', 'bundle_origins', 'document_count', 'parser_versions',
        'model', 'prompt_version', 'extraction_schema_version', 'review_schema_version'
      )
    )
    OR json_extract(NEW.source_content, '$.schema_version') <> 'user_confirmed_ai_import_v1'
    OR json_type(NEW.source_content, '$.bundle_origins') <> 'array'
    OR json_array_length(NEW.source_content, '$.bundle_origins') NOT BETWEEN 1 AND 2
    OR json_extract(NEW.source_content, '$.bundle_origins') NOT IN (
      '["resume"]', '["linkedin_profile_export"]', '["resume","linkedin_profile_export"]'
    )
    OR json_type(NEW.source_content, '$.document_count') <> 'integer'
    OR json_extract(NEW.source_content, '$.document_count') <> json_array_length(NEW.source_content, '$.bundle_origins')
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_content, '$.bundle_origins')
      WHERE type <> 'text' OR value NOT IN ('resume', 'linkedin_profile_export')
    )
    OR (SELECT COUNT(DISTINCT value) FROM json_each(NEW.source_content, '$.bundle_origins'))
       <> json_array_length(NEW.source_content, '$.bundle_origins')
    OR json_type(NEW.source_content, '$.parser_versions') <> 'array'
    OR json_array_length(NEW.source_content, '$.parser_versions') <> json_extract(NEW.source_content, '$.document_count')
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_content, '$.parser_versions')
      WHERE type <> 'text' OR length(value) NOT BETWEEN 1 AND 128 OR value GLOB '*[^0-9A-Za-z._:/-]*'
    )
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_content)
      WHERE key IN ('model', 'prompt_version', 'extraction_schema_version', 'review_schema_version')
        AND (type <> 'text' OR length(value) NOT BETWEEN 1 AND 128 OR value GLOB '*[^0-9A-Za-z._:/-]*')
    )
  ) THEN RAISE(ABORT, 'AI profile source metadata is invalid') END;
END;

CREATE TRIGGER trg_product_profile_sources_no_update
BEFORE UPDATE ON product_profile_sources
BEGIN
  SELECT RAISE(ABORT, 'product profile sources are immutable');
END;

CREATE TRIGGER trg_product_profile_sources_delete_guard
BEFORE DELETE ON product_profile_sources
WHEN EXISTS (SELECT 1 FROM product_profiles WHERE profile_id = OLD.profile_id)
BEGIN
  SELECT RAISE(ABORT, 'product profile sources cannot be deleted individually');
END;

CREATE TABLE ai_profile_import_entitlements (
  environment_namespace TEXT NOT NULL,
  account_id TEXT NOT NULL,
  entitlement_code TEXT NOT NULL,
  state TEXT NOT NULL,
  reservation_id TEXT,
  attempt_id TEXT,
  lease_expires_at TEXT,
  consumed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,

  PRIMARY KEY (environment_namespace, account_id, entitlement_code),
  UNIQUE (reservation_id),
  FOREIGN KEY (account_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (entitlement_code = 'ai_profile_import_v1'),
  CHECK (state IN ('available', 'reserved', 'consumed')),
  CHECK (reservation_id IS NULL OR (
    length(reservation_id) = 36 AND substr(reservation_id, 1, 4) = 'air_'
    AND substr(reservation_id, 5) NOT GLOB '*[^0-9a-f]*'
    AND substr(reservation_id, 5) <> replace(printf('%32s', ''), ' ', substr(reservation_id, 5, 1))
  )),
  CHECK (attempt_id IS NULL OR (
    length(attempt_id) = 36 AND substr(attempt_id, 1, 4) = 'aip_'
    AND substr(attempt_id, 5) NOT GLOB '*[^0-9a-f]*'
    AND substr(attempt_id, 5) <> replace(printf('%32s', ''), ' ', substr(attempt_id, 5, 1))
  )),
  CHECK (lease_expires_at IS NULL OR (
    length(lease_expires_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', lease_expires_at) IS lease_expires_at
  )),
  CHECK (consumed_at IS NULL OR (
    length(consumed_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', consumed_at) IS consumed_at
  )),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(updated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at),
  CHECK (julianday(updated_at) >= julianday(created_at)),
  CHECK (
    (state = 'available' AND reservation_id IS NULL AND attempt_id IS NULL AND lease_expires_at IS NULL AND consumed_at IS NULL)
    OR (state = 'reserved' AND reservation_id IS NOT NULL AND attempt_id IS NOT NULL AND lease_expires_at IS NOT NULL AND consumed_at IS NULL)
    OR (state = 'consumed' AND reservation_id IS NOT NULL AND attempt_id IS NOT NULL AND lease_expires_at IS NULL AND consumed_at IS NOT NULL)
  )
);

CREATE TABLE ai_profile_import_attempts (
  attempt_id TEXT PRIMARY KEY,
  reservation_id TEXT NOT NULL UNIQUE,
  environment_namespace TEXT NOT NULL,
  account_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  entitlement_code TEXT NOT NULL,
  state TEXT NOT NULL,
  idempotency_key_sha256 TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  confirmation_fingerprint TEXT,
  source_metadata_json TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL,
  latest_event_version INTEGER NOT NULL,
  latest_event_id TEXT NOT NULL,
  lineage_sha256 TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  result_code TEXT,
  result_profile_id TEXT,
  result_revision_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,

  UNIQUE (environment_namespace, account_id, entitlement_code, idempotency_key_sha256),
  FOREIGN KEY (environment_namespace, account_id, entitlement_code)
    REFERENCES ai_profile_import_entitlements(environment_namespace, account_id, entitlement_code)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (account_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  FOREIGN KEY (principal_id, environment_namespace)
    REFERENCES product_principals(principal_id, environment_namespace) ON DELETE RESTRICT,
  CHECK (length(attempt_id) = 36 AND substr(attempt_id, 1, 4) = 'aip_' AND substr(attempt_id, 5) NOT GLOB '*[^0-9a-f]*' AND substr(attempt_id, 5) <> replace(printf('%32s', ''), ' ', substr(attempt_id, 5, 1))),
  CHECK (length(reservation_id) = 36 AND substr(reservation_id, 1, 4) = 'air_' AND substr(reservation_id, 5) NOT GLOB '*[^0-9a-f]*' AND substr(reservation_id, 5) <> replace(printf('%32s', ''), ' ', substr(reservation_id, 5, 1))),
  CHECK (entitlement_code = 'ai_profile_import_v1'),
  CHECK (state IN ('reserved', 'released', 'expired', 'failed', 'succeeded')),
  CHECK (length(idempotency_key_sha256) = 64 AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (confirmation_fingerprint IS NULL OR (length(confirmation_fingerprint) = 64 AND confirmation_fingerprint NOT GLOB '*[^0-9a-f]*')),
  CHECK (result_profile_id IS NULL OR (
    length(result_profile_id) = 36 AND substr(result_profile_id, 1, 4) = 'prf_'
    AND substr(result_profile_id, 5) NOT GLOB '*[^0-9a-f]*'
    AND substr(result_profile_id, 5) <> replace(printf('%32s', ''), ' ', substr(result_profile_id, 5, 1))
  )),
  CHECK (result_revision_id IS NULL OR (
    length(result_revision_id) = 36 AND substr(result_revision_id, 1, 4) = 'pvr_'
    AND substr(result_revision_id, 5) NOT GLOB '*[^0-9a-f]*'
    AND substr(result_revision_id, 5) <> replace(printf('%32s', ''), ' ', substr(result_revision_id, 5, 1))
  )),
  CHECK (length(CAST(source_metadata_json AS BLOB)) BETWEEN 2 AND 4096),
  CHECK (json_valid(source_metadata_json) AND json_type(source_metadata_json) = 'object'),
  CHECK (length(binding_id) = 36 AND substr(binding_id, 1, 4) = 'pab_' AND substr(binding_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (binding_version >= 1 AND latest_event_version = binding_version),
  CHECK (length(latest_event_id) = 36 AND substr(latest_event_id, 1, 4) = 'obe_' AND substr(latest_event_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(lineage_sha256) = 64 AND lineage_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(lease_expires_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', lease_expires_at) IS lease_expires_at),
  CHECK (result_code IS NULL OR (length(result_code) BETWEEN 1 AND 64 AND result_code = lower(result_code) AND result_code NOT GLOB '*[^a-z0-9_.-]*')),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(updated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at),
  CHECK (completed_at IS NULL OR (length(completed_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', completed_at) IS completed_at)),
  CHECK (julianday(updated_at) >= julianday(created_at)),
  CHECK (julianday(lease_expires_at) > julianday(created_at)),
  CHECK (
    (state = 'reserved' AND result_code IS NULL AND confirmation_fingerprint IS NULL AND result_profile_id IS NULL AND result_revision_id IS NULL AND completed_at IS NULL)
    OR (state IN ('released', 'expired', 'failed') AND result_code IS NOT NULL AND confirmation_fingerprint IS NULL AND result_profile_id IS NULL AND result_revision_id IS NULL AND completed_at IS NOT NULL)
    OR (state = 'succeeded' AND result_code = 'success' AND confirmation_fingerprint IS NOT NULL AND result_profile_id IS NOT NULL AND result_revision_id IS NOT NULL AND completed_at IS NOT NULL)
  )
);

CREATE INDEX idx_ai_profile_import_attempts_entitlement_state
ON ai_profile_import_attempts(environment_namespace, account_id, entitlement_code, state);

CREATE INDEX idx_ai_profile_import_attempts_principal_time
ON ai_profile_import_attempts(principal_id, created_at);

CREATE TRIGGER trg_ai_profile_import_entitlements_no_delete
BEFORE DELETE ON ai_profile_import_entitlements
BEGIN
  SELECT RAISE(ABORT, 'AI import entitlement cannot be deleted');
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
    OR (OLD.state = 'reserved' AND NEW.state IN ('available', 'consumed'))
  ) THEN RAISE(ABORT, 'AI import entitlement transition is invalid') END;
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

CREATE TRIGGER trg_ai_profile_import_attempts_insert_guard
BEFORE INSERT ON ai_profile_import_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM ai_profile_import_entitlements entitlement
    WHERE entitlement.environment_namespace = NEW.environment_namespace
      AND entitlement.account_id = NEW.account_id
      AND entitlement.entitlement_code = NEW.entitlement_code
      AND entitlement.state = 'available'
  ) THEN RAISE(ABORT, 'AI import attempt requires an available entitlement') END;
  SELECT CASE WHEN json_type(NEW.source_metadata_json) <> 'object'
    OR (SELECT COUNT(*) FROM json_each(NEW.source_metadata_json)) <> 8
    OR (SELECT COUNT(DISTINCT key) FROM json_each(NEW.source_metadata_json)) <> 8
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_metadata_json)
      WHERE key NOT IN (
        'schema_version', 'bundle_origins', 'document_count', 'parser_versions',
        'model', 'prompt_version', 'extraction_schema_version', 'review_schema_version'
      )
    )
    OR json_extract(NEW.source_metadata_json, '$.schema_version') <> 'user_confirmed_ai_import_v1'
    OR json_type(NEW.source_metadata_json, '$.bundle_origins') <> 'array'
    OR json_array_length(NEW.source_metadata_json, '$.bundle_origins') NOT BETWEEN 1 AND 2
    OR json_extract(NEW.source_metadata_json, '$.bundle_origins') NOT IN (
      '["resume"]', '["linkedin_profile_export"]', '["resume","linkedin_profile_export"]'
    )
    OR json_type(NEW.source_metadata_json, '$.document_count') <> 'integer'
    OR json_extract(NEW.source_metadata_json, '$.document_count') <> json_array_length(NEW.source_metadata_json, '$.bundle_origins')
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_metadata_json, '$.bundle_origins')
      WHERE type <> 'text' OR value NOT IN ('resume', 'linkedin_profile_export')
    )
    OR (SELECT COUNT(DISTINCT value) FROM json_each(NEW.source_metadata_json, '$.bundle_origins'))
       <> json_array_length(NEW.source_metadata_json, '$.bundle_origins')
    OR json_type(NEW.source_metadata_json, '$.parser_versions') <> 'array'
    OR json_array_length(NEW.source_metadata_json, '$.parser_versions') <> json_extract(NEW.source_metadata_json, '$.document_count')
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_metadata_json, '$.parser_versions')
      WHERE type <> 'text' OR length(value) NOT BETWEEN 1 AND 128 OR value GLOB '*[^0-9A-Za-z._:/-]*'
    )
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.source_metadata_json)
      WHERE key IN ('model', 'prompt_version', 'extraction_schema_version', 'review_schema_version')
        AND (type <> 'text' OR length(value) NOT BETWEEN 1 AND 128 OR value GLOB '*[^0-9A-Za-z._:/-]*')
    )
    THEN RAISE(ABORT, 'AI import attempt metadata is invalid') END;
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
    OR NEW.lease_expires_at <> OLD.lease_expires_at
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'AI import attempt identity is immutable') END;
  SELECT CASE WHEN OLD.state <> 'reserved' OR NEW.state NOT IN ('released', 'expired', 'failed', 'succeeded')
    THEN RAISE(ABORT, 'AI import attempt transition is invalid') END;
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

CREATE TRIGGER trg_ai_profile_import_attempts_no_delete
BEFORE DELETE ON ai_profile_import_attempts
BEGIN
  SELECT RAISE(ABORT, 'AI import attempt cannot be deleted');
END;
