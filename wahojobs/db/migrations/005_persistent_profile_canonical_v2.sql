DROP VIEW current_product_profiles;

DROP TRIGGER trg_product_profiles_insert_guard;
DROP TRIGGER trg_product_profiles_no_update;
DROP TRIGGER trg_product_profiles_delete_guard;
DROP TRIGGER trg_product_profile_sources_insert_guard;
DROP TRIGGER trg_product_profile_sources_no_update;
DROP TRIGGER trg_product_profile_sources_delete_guard;
DROP TRIGGER trg_product_profile_revisions_insert_guard;
DROP TRIGGER trg_product_profile_revisions_no_update;
DROP TRIGGER trg_product_profile_revisions_delete_guard;

DROP INDEX idx_product_profiles_environment;
DROP INDEX idx_product_profile_revisions_profile_history;
DROP INDEX idx_product_profile_revisions_principal_history;
DROP INDEX idx_product_profile_revisions_lifecycle;
DROP INDEX idx_product_profile_sources_revision;
DROP INDEX idx_product_profile_sources_profile;

ALTER TABLE product_profile_sources RENAME TO product_profile_sources_m005_backup;
ALTER TABLE product_profile_revisions RENAME TO product_profile_revisions_m005_backup;
ALTER TABLE product_profiles RENAME TO product_profiles_m005_backup;

CREATE TABLE product_profiles (
  profile_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL UNIQUE,
  environment_namespace TEXT NOT NULL,
  initial_revision_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,

  UNIQUE (profile_id, principal_id, environment_namespace),
  FOREIGN KEY (principal_id, environment_namespace)
    REFERENCES product_principals(principal_id, environment_namespace) ON DELETE RESTRICT,
  FOREIGN KEY (initial_revision_id, profile_id)
    REFERENCES product_profile_revisions(revision_id, profile_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (length(profile_id) = 36),
  CHECK (substr(profile_id, 1, 4) = 'prf_'),
  CHECK (substr(profile_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(profile_id, 5) <> replace(printf('%32s', ''), ' ', substr(profile_id, 5, 1))),
  CHECK (length(initial_revision_id) = 36),
  CHECK (substr(initial_revision_id, 1, 4) = 'pvr_'),
  CHECK (substr(initial_revision_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(initial_revision_id, 5) <> replace(printf('%32s', ''), ' ', substr(initial_revision_id, 5, 1))),
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at)
);

CREATE TABLE product_profile_revisions (
  revision_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  environment_namespace TEXT NOT NULL,
  revision_number INTEGER NOT NULL,
  previous_revision_id TEXT,
  correction_of_revision_id TEXT,
  revision_kind TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL,
  canonical_schema_version TEXT NOT NULL,
  structured_profile_json TEXT NOT NULL,
  structured_profile_sha256 TEXT NOT NULL,
  source_count INTEGER NOT NULL,
  source_bundle_sha256 TEXT NOT NULL,
  normalizer_version TEXT,
  reviewer_version TEXT,
  actor_type TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,

  UNIQUE (profile_id, revision_number),
  UNIQUE (revision_id, profile_id),
  UNIQUE (revision_id, profile_id, principal_id, environment_namespace),
  UNIQUE (principal_id, idempotency_key),
  FOREIGN KEY (profile_id, principal_id, environment_namespace)
    REFERENCES product_profiles(profile_id, principal_id, environment_namespace) ON DELETE CASCADE,
  FOREIGN KEY (previous_revision_id, profile_id)
    REFERENCES product_profile_revisions(revision_id, profile_id) ON DELETE CASCADE
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (correction_of_revision_id, profile_id)
    REFERENCES product_profile_revisions(revision_id, profile_id) ON DELETE CASCADE
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (length(revision_id) = 36),
  CHECK (substr(revision_id, 1, 4) = 'pvr_'),
  CHECK (substr(revision_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(revision_id, 5) <> replace(printf('%32s', ''), ' ', substr(revision_id, 5, 1))),
  CHECK (revision_number >= 1),
  CHECK (revision_kind IN ('initial', 'edit', 'correction', 'archive', 'reactivate', 'deletion_request')),
  CHECK (lifecycle_status IN ('active', 'archived', 'deletion_requested')),
  CHECK (canonical_schema_version = 'canonical_profile_v2'),
  CHECK (length(CAST(structured_profile_json AS BLOB)) BETWEEN 2 AND 131072),
  CHECK (json_valid(structured_profile_json) AND json_type(structured_profile_json) IS 'object'),
  CHECK (
    json_type(structured_profile_json, '$.schema_version') IS 'text'
    AND json_extract(structured_profile_json, '$.schema_version') IS canonical_schema_version
  ),
  CHECK (json_type(structured_profile_json, '$.identity') IS 'object'),
  CHECK (
    json_type(structured_profile_json, '$.identity.profile_id') IS 'text'
    AND json_extract(structured_profile_json, '$.identity.profile_id') IS profile_id
  ),
  CHECK (json_type(structured_profile_json, '$.provenance.original_text') IS NULL),
  CHECK (
    json_type(structured_profile_json, '$.provenance.evidence_snippets') IS NULL
    OR (
      json_type(structured_profile_json, '$.provenance.evidence_snippets') = 'array'
      AND json_array_length(structured_profile_json, '$.provenance.evidence_snippets') = 0
    )
  ),
  CHECK (length(structured_profile_sha256) = 64),
  CHECK (structured_profile_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (structured_profile_sha256 <> replace(printf('%64s', ''), ' ', substr(structured_profile_sha256, 1, 1))),
  CHECK (source_count BETWEEN 1 AND 16),
  CHECK (length(source_bundle_sha256) = 64),
  CHECK (source_bundle_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (source_bundle_sha256 <> replace(printf('%64s', ''), ' ', substr(source_bundle_sha256, 1, 1))),
  CHECK (normalizer_version IS NULL OR (
    length(normalizer_version) BETWEEN 1 AND 64
    AND normalizer_version = lower(normalizer_version)
    AND normalizer_version NOT GLOB '*[^a-z0-9_.-]*'
  )),
  CHECK (reviewer_version IS NULL OR (
    length(reviewer_version) BETWEEN 1 AND 64
    AND reviewer_version = lower(reviewer_version)
    AND reviewer_version NOT GLOB '*[^a-z0-9_.-]*'
  )),
  CHECK (actor_type IN ('authenticated_user', 'development_service', 'system')),
  CHECK (length(reason_code) BETWEEN 1 AND 128),
  CHECK (reason_code = lower(reason_code)),
  CHECK (reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (length(idempotency_key) BETWEEN 16 AND 256),
  CHECK (idempotency_key = trim(idempotency_key)),
  CHECK (instr(idempotency_key, char(0)) = 0),
  CHECK (length(request_fingerprint) = 64),
  CHECK (request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (request_fingerprint <> replace(printf('%64s', ''), ' ', substr(request_fingerprint, 1, 1))),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at)
);

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
  CHECK (source_type IN ('confirmed_about_you_text', 'user_confirmed_correction', 'confirmed_lifecycle_action')),
  CHECK (
    (source_type = 'confirmed_about_you_text' AND source_format = 'text/plain')
    OR (source_type = 'user_confirmed_correction' AND source_format = 'application/json')
    OR (source_type = 'confirmed_lifecycle_action' AND source_format = 'application/json')
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

DROP TABLE product_profile_sources_m005_backup;
DROP TABLE product_profile_revisions_m005_backup;
DROP TABLE product_profiles_m005_backup;

CREATE INDEX idx_product_profiles_environment
ON product_profiles(environment_namespace, created_at);

CREATE INDEX idx_product_profile_revisions_profile_history
ON product_profile_revisions(profile_id, revision_number DESC);

CREATE INDEX idx_product_profile_revisions_principal_history
ON product_profile_revisions(principal_id, created_at DESC);

CREATE INDEX idx_product_profile_revisions_lifecycle
ON product_profile_revisions(environment_namespace, lifecycle_status, created_at DESC);

CREATE INDEX idx_product_profile_sources_revision
ON product_profile_sources(revision_id, source_ordinal);

CREATE INDEX idx_product_profile_sources_profile
ON product_profile_sources(profile_id, accepted_at);

CREATE TRIGGER trg_product_profiles_insert_guard
BEFORE INSERT ON product_profiles
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM product_principals principal
    WHERE principal.principal_id = NEW.principal_id
      AND principal.environment_namespace = NEW.environment_namespace
      AND principal.lifecycle_status = 'active'
      AND (
        (
          principal.principal_type = 'development'
          AND principal.claim_policy = 'nonclaimable'
          AND principal.environment_namespace IN ('development', 'test')
        )
        OR (
          principal.principal_type = 'account_native'
          AND principal.claim_policy = 'account_native'
          AND principal.exclusive_account_binding = 1
          AND EXISTS (
            SELECT 1 FROM principal_account_bindings binding
            JOIN users account ON account.user_id = binding.user_id
            WHERE binding.principal_id = principal.principal_id
              AND binding.environment_namespace = principal.environment_namespace
              AND binding.binding_role = 'owner'
              AND binding.binding_status = 'active'
              AND account.lifecycle_status = 'active'
          )
        )
      )
  ) THEN RAISE(ABORT, 'profile principal is not eligible') END;
END;

CREATE TRIGGER trg_product_profiles_no_update
BEFORE UPDATE ON product_profiles
BEGIN
  SELECT RAISE(ABORT, 'product profile identity is immutable');
END;

CREATE TRIGGER trg_product_profiles_delete_guard
BEFORE DELETE ON product_profiles
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM product_profile_revisions revision
    WHERE revision.profile_id = OLD.profile_id
    ORDER BY revision.revision_number DESC
    LIMIT 1
  ) THEN RAISE(ABORT, 'profile purge requires revision history') END;
  SELECT CASE WHEN (
    SELECT revision.lifecycle_status FROM product_profile_revisions revision
    WHERE revision.profile_id = OLD.profile_id
    ORDER BY revision.revision_number DESC
    LIMIT 1
  ) <> 'deletion_requested' THEN RAISE(ABORT, 'profile purge requires deletion_requested lifecycle') END;
END;

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

CREATE TRIGGER trg_product_profile_revisions_insert_guard
BEFORE INSERT ON product_profile_revisions
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM product_profiles profile
    WHERE profile.profile_id = NEW.profile_id
      AND profile.principal_id = NEW.principal_id
      AND profile.environment_namespace = NEW.environment_namespace
      AND julianday(NEW.created_at) >= julianday(profile.created_at)
      AND (NEW.revision_number <> 1 OR profile.initial_revision_id = NEW.revision_id)
  ) THEN RAISE(ABORT, 'profile revision identity or time is invalid') END;
  SELECT CASE WHEN NEW.revision_number = 1 AND NOT (
    NEW.revision_kind = 'initial'
    AND NEW.lifecycle_status = 'active'
    AND NEW.previous_revision_id IS NULL
    AND NEW.correction_of_revision_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM product_profile_revisions WHERE profile_id = NEW.profile_id)
  ) THEN RAISE(ABORT, 'initial profile revision is invalid') END;
  SELECT CASE WHEN NEW.revision_number > 1 AND (
    NEW.revision_kind = 'initial'
    OR NEW.previous_revision_id IS NULL
    OR NEW.revision_number <> COALESCE((
      SELECT MAX(revision_number) + 1 FROM product_profile_revisions WHERE profile_id = NEW.profile_id
    ), 1)
    OR NEW.previous_revision_id <> (
      SELECT revision_id FROM product_profile_revisions
      WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1
    )
  ) THEN RAISE(ABORT, 'profile revision history must be contiguous') END;
  SELECT CASE WHEN NEW.revision_number > 1 AND julianday(NEW.created_at) < julianday((
    SELECT created_at FROM product_profile_revisions
    WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1
  )) THEN RAISE(ABORT, 'profile revision time cannot move backward') END;
  SELECT CASE WHEN NEW.revision_number > 1 AND (
    (SELECT lifecycle_status FROM product_profile_revisions
      WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1) = 'deletion_requested'
    OR (NEW.revision_kind IN ('edit', 'correction') AND NEW.lifecycle_status <> (
      SELECT lifecycle_status FROM product_profile_revisions
      WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1
    ))
    OR (NEW.revision_kind = 'archive' AND NOT (
      (SELECT lifecycle_status FROM product_profile_revisions
        WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1) = 'active'
      AND NEW.lifecycle_status = 'archived'
    ))
    OR (NEW.revision_kind = 'reactivate' AND NOT (
      (SELECT lifecycle_status FROM product_profile_revisions
        WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1) = 'archived'
      AND NEW.lifecycle_status = 'active'
    ))
    OR (NEW.revision_kind = 'deletion_request' AND NOT (
      (SELECT lifecycle_status FROM product_profile_revisions
        WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1) IN ('active', 'archived')
      AND NEW.lifecycle_status = 'deletion_requested'
    ))
  ) THEN RAISE(ABORT, 'profile lifecycle transition is invalid') END;
  SELECT CASE WHEN (
    NEW.canonical_schema_version IS NOT 'canonical_profile_v2'
    OR json_type(NEW.structured_profile_json, '$.schema_version') IS NOT 'text'
    OR json_extract(NEW.structured_profile_json, '$.schema_version') IS NOT NEW.canonical_schema_version
    OR json_type(NEW.structured_profile_json, '$.identity') IS NOT 'object'
    OR json_type(NEW.structured_profile_json, '$.identity.profile_id') IS NOT 'text'
    OR json_extract(NEW.structured_profile_json, '$.identity.profile_id') IS NOT NEW.profile_id
  ) THEN RAISE(ABORT, 'canonical profile v2 envelope is invalid') END;
  SELECT CASE WHEN NEW.revision_kind IN ('archive', 'reactivate', 'deletion_request') AND (
    NEW.revision_number <= 1
    OR NEW.correction_of_revision_id IS NOT NULL
    OR NEW.source_count <> 1
    OR NEW.canonical_schema_version IS NOT (
      SELECT canonical_schema_version FROM product_profile_revisions
      WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1
    )
    OR NEW.structured_profile_json IS NOT (
      SELECT structured_profile_json FROM product_profile_revisions
      WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1
    )
    OR NEW.structured_profile_sha256 IS NOT (
      SELECT structured_profile_sha256 FROM product_profile_revisions
      WHERE profile_id = NEW.profile_id ORDER BY revision_number DESC LIMIT 1
    )
    OR NOT EXISTS (
      SELECT 1 FROM product_profile_sources source
      WHERE source.revision_id = NEW.revision_id
        AND source.source_ordinal = 1
        AND source.source_type = 'confirmed_lifecycle_action'
        AND source.source_format = 'application/json'
        AND source.source_schema_version = 'confirmed_lifecycle_action_v1'
        AND source.source_content = (
          '{"action":"' || NEW.revision_kind || '","schema_version":"confirmed_lifecycle_action_v1"}'
        )
    )
  ) THEN RAISE(ABORT, 'profile lifecycle revision source or content is invalid') END;
  SELECT CASE WHEN NEW.revision_kind IN ('initial', 'edit', 'correction') AND EXISTS (
    SELECT 1 FROM product_profile_sources source
    WHERE source.revision_id = NEW.revision_id
      AND source.source_type = 'confirmed_lifecycle_action'
  ) THEN RAISE(ABORT, 'lifecycle source cannot support an ordinary revision') END;
  SELECT CASE WHEN NEW.revision_kind IN ('archive', 'reactivate', 'deletion_request') AND EXISTS (
    SELECT 1 FROM product_profile_sources source
    WHERE source.revision_id = NEW.revision_id
      AND source.source_type <> 'confirmed_lifecycle_action'
  ) THEN RAISE(ABORT, 'ordinary source cannot support a lifecycle revision') END;
  SELECT CASE WHEN (
    (NEW.revision_kind = 'correction' AND NEW.correction_of_revision_id IS NULL)
    OR (NEW.revision_kind <> 'correction' AND NEW.correction_of_revision_id IS NOT NULL)
    OR (NEW.correction_of_revision_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM product_profile_revisions target
      WHERE target.revision_id = NEW.correction_of_revision_id
        AND target.profile_id = NEW.profile_id
        AND target.revision_number < NEW.revision_number
    ))
  ) THEN RAISE(ABORT, 'profile correction target is invalid') END;
  SELECT CASE WHEN NEW.source_count <> (
    SELECT COUNT(*) FROM product_profile_sources source WHERE source.revision_id = NEW.revision_id
  ) OR NEW.source_count <> (
    SELECT COALESCE(MAX(source_ordinal), 0) FROM product_profile_sources source
    WHERE source.revision_id = NEW.revision_id
  ) OR EXISTS (
    SELECT 1 FROM product_profile_sources source
    WHERE source.revision_id = NEW.revision_id
      AND (
        source.profile_id <> NEW.profile_id
        OR source.principal_id <> NEW.principal_id
        OR source.environment_namespace <> NEW.environment_namespace
        OR julianday(source.accepted_at) > julianday(NEW.created_at)
      )
  ) THEN RAISE(ABORT, 'profile revision sources are incomplete or inconsistent') END;
  SELECT CASE WHEN (SELECT COUNT(*) FROM json_tree(NEW.structured_profile_json)) > 4096
    THEN RAISE(ABORT, 'structured profile contains too many values') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.structured_profile_json)
    WHERE (length(path) - length(replace(path, '.', '')))
      + (length(path) - length(replace(path, '[', ''))) > 12
  ) THEN RAISE(ABORT, 'structured profile exceeds allowed depth') END;
  SELECT CASE WHEN EXISTS (
    SELECT parent FROM json_tree(NEW.structured_profile_json)
    WHERE parent IS NOT NULL GROUP BY parent HAVING COUNT(*) > 256
  ) THEN RAISE(ABORT, 'structured profile collection is too large') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.structured_profile_json)
    WHERE (type = 'text' AND length(atom) > 4096)
      OR (key IS NOT NULL AND length(CAST(key AS TEXT)) > 128)
  ) THEN RAISE(ABORT, 'structured profile scalar or key is too large') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM json_tree(NEW.structured_profile_json) node
    JOIN json_tree(NEW.structured_profile_json) container
      ON container.id = node.parent
    WHERE container.type = 'object'
      AND (
        typeof(node.key) <> 'text'
        OR length(node.key) NOT BETWEEN 1 AND 128
        OR node.key <> lower(node.key)
        OR node.key GLOB '*[^a-z0-9_]*'
      )
  ) THEN RAISE(ABORT, 'structured profile object key is invalid') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM json_tree(NEW.structured_profile_json) node
    JOIN json_tree(NEW.structured_profile_json) container
      ON container.id = node.parent
    WHERE container.type = 'object'
    GROUP BY node.parent, node.key
    HAVING COUNT(*) > 1
  ) THEN RAISE(ABORT, 'structured profile contains duplicate object keys') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM json_tree(NEW.structured_profile_json) node
    JOIN json_tree(NEW.structured_profile_json) container
      ON container.id = node.parent
    WHERE container.type = 'object'
      AND replace(node.key, '_', '') IN (
        'originaltext', 'rawtext', 'rawinput', 'rawcontent', 'aboutyou',
        'aboutyoutext', 'sourcetext', 'sourcecontent', 'evidence',
        'evidencesnippet', 'evidencesnippets', 'resume', 'resumecontent',
        'cv', 'cvcontent', 'applicationcontent', 'rawapplicationcontent',
        'accountid', 'userid', 'principalid', 'providerid', 'providersubject',
        'sessionid', 'sessiontoken', 'token', 'cookie', 'authorization',
        'authorizationheader', 'authenticationheader', 'password', 'secret',
        'credential', 'bearer', 'csrf', 'csrfmaterial', 'invitationhmac',
        'rawclaims', 'email', 'oauthsubject'
      )
  ) THEN RAISE(ABORT, 'structured profile contains prohibited raw or identity metadata') END;
END;

CREATE TRIGGER trg_product_profile_revisions_no_update
BEFORE UPDATE ON product_profile_revisions
BEGIN
  SELECT RAISE(ABORT, 'product profile revisions are immutable');
END;

CREATE TRIGGER trg_product_profile_revisions_delete_guard
BEFORE DELETE ON product_profile_revisions
WHEN EXISTS (SELECT 1 FROM product_profiles WHERE profile_id = OLD.profile_id)
BEGIN
  SELECT RAISE(ABORT, 'product profile revisions cannot be deleted individually');
END;

CREATE VIEW current_product_profiles AS
SELECT
  profile.profile_id,
  profile.principal_id,
  profile.environment_namespace,
  profile.initial_revision_id,
  profile.created_at AS profile_created_at,
  revision.revision_id AS current_revision_id,
  revision.revision_number AS current_revision_number,
  revision.revision_kind AS current_revision_kind,
  revision.lifecycle_status,
  revision.canonical_schema_version,
  revision.structured_profile_json,
  revision.structured_profile_sha256,
  revision.created_at AS revised_at
FROM product_profiles profile
JOIN product_profile_revisions revision
  ON revision.profile_id = profile.profile_id
WHERE revision.revision_number = (
  SELECT MAX(candidate.revision_number)
  FROM product_profile_revisions candidate
  WHERE candidate.profile_id = profile.profile_id
);