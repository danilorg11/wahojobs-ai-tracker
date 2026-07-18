CREATE TABLE product_principals (
  principal_id TEXT PRIMARY KEY,
  environment_namespace TEXT NOT NULL,
  principal_type TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL,
  claim_policy TEXT NOT NULL,
  exclusive_account_binding INTEGER NOT NULL,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  provenance_json TEXT NOT NULL DEFAULT '{}',

  CHECK (length(principal_id) = 36),
  CHECK (substr(principal_id, 1, 4) = 'prn_'),
  CHECK (substr(principal_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(principal_id, 5) <> replace(printf('%32s', ''), ' ', substr(principal_id, 5, 1))),
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (principal_type IN ('legacy_profile', 'account_native', 'development', 'sample', 'system')),
  CHECK (lifecycle_status IN ('dormant', 'active', 'suspended', 'retired')),
  CHECK (claim_policy IN ('nonclaimable', 'manual_approval', 'account_native')),
  CHECK (
    (principal_type = 'legacy_profile' AND claim_policy IN ('nonclaimable', 'manual_approval'))
    OR (principal_type = 'account_native' AND claim_policy = 'account_native')
    OR (principal_type IN ('development', 'sample', 'system') AND claim_policy = 'nonclaimable')
  ),
  CHECK (exclusive_account_binding IN (0, 1)),
  CHECK (version >= 1),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(updated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at),
  CHECK (julianday(updated_at) >= julianday(created_at)),
  CHECK (length(CAST(provenance_json AS BLOB)) <= 4096),
  CHECK (json_valid(provenance_json) AND json_type(provenance_json) = 'object')
);

CREATE TABLE legacy_owner_aliases (
  alias_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  environment_namespace TEXT NOT NULL,
  alias_kind TEXT NOT NULL,
  alias_family TEXT GENERATED ALWAYS AS (
    CASE
      WHEN alias_kind IN ('profile_id', 'pipeline_owner', 'applicant_user_id', 'legacy_user_id')
        THEN 'owner_resource'
      WHEN alias_kind = 'anonymous_user_key' THEN 'anonymous'
    END
  ) STORED,
  alias_value TEXT NOT NULL,
  claimability TEXT NOT NULL,
  discovered_from TEXT NOT NULL,
  created_at TEXT NOT NULL,
  provenance_json TEXT NOT NULL DEFAULT '{}',

  FOREIGN KEY (principal_id) REFERENCES product_principals(principal_id) ON DELETE RESTRICT,
  UNIQUE (environment_namespace, alias_kind, alias_value),
  CHECK (length(alias_id) = 36),
  CHECK (substr(alias_id, 1, 4) = 'loa_'),
  CHECK (substr(alias_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(alias_id, 5) <> replace(printf('%32s', ''), ' ', substr(alias_id, 5, 1))),
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (alias_kind IN ('profile_id', 'pipeline_owner', 'applicant_user_id', 'anonymous_user_key', 'legacy_user_id')),
  CHECK (alias_family IN ('owner_resource', 'anonymous')),
  CHECK (length(alias_value) BETWEEN 1 AND 512),
  CHECK (alias_value = trim(alias_value)),
  CHECK (instr(alias_value, char(0)) = 0),
  CHECK (claimability IN ('nonclaimable', 'manual_approval', 'account_native')),
  CHECK (discovered_from IN ('user_profiles', 'user_pipeline_items', 'user_pipeline_transitions', 'applicant_status_updates', 'manual_review', 'account_creation', 'system')),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(CAST(provenance_json AS BLOB)) <= 4096),
  CHECK (json_valid(provenance_json) AND json_type(provenance_json) = 'object')
);

CREATE TABLE principal_account_bindings (
  binding_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  environment_namespace TEXT NOT NULL,
  binding_role TEXT NOT NULL,
  binding_status TEXT NOT NULL,
  version INTEGER NOT NULL,
  latest_event_version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  suspended_at TEXT,
  provenance_json TEXT NOT NULL DEFAULT '{}',

  FOREIGN KEY (principal_id) REFERENCES product_principals(principal_id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  UNIQUE (binding_id, principal_id, user_id),
  CHECK (length(binding_id) = 36),
  CHECK (substr(binding_id, 1, 4) = 'pab_'),
  CHECK (substr(binding_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(binding_id, 5) <> replace(printf('%32s', ''), ' ', substr(binding_id, 5, 1))),
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (binding_role IN ('owner', 'delegated', 'support')),
  CHECK (binding_status IN ('active', 'suspended', 'released')),
  CHECK (version >= 1 AND latest_event_version = version),
  CHECK (length(created_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at),
  CHECK (length(updated_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at),
  CHECK (julianday(updated_at) >= julianday(created_at)),
  CHECK (suspended_at IS NULL OR (length(suspended_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', suspended_at) IS suspended_at AND julianday(suspended_at) >= julianday(created_at))),
  CHECK ((binding_status = 'suspended' AND suspended_at IS NOT NULL) OR (binding_status <> 'suspended' AND suspended_at IS NULL)),
  CHECK (length(CAST(provenance_json AS BLOB)) <= 4096),
  CHECK (json_valid(provenance_json) AND json_type(provenance_json) = 'object')
);

CREATE TABLE ownership_binding_events (
  event_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  environment_namespace TEXT NOT NULL,
  event_version INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  prior_status TEXT,
  resulting_status TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  approval_reference TEXT,
  idempotency_key TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',

  FOREIGN KEY (principal_id) REFERENCES product_principals(principal_id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
  FOREIGN KEY (binding_id, principal_id, user_id)
    REFERENCES principal_account_bindings(binding_id, principal_id, user_id) ON DELETE RESTRICT,
  UNIQUE (binding_id, event_version),
  UNIQUE (principal_id, idempotency_key),
  CHECK (length(event_id) = 36),
  CHECK (substr(event_id, 1, 4) = 'obe_'),
  CHECK (substr(event_id, 5) NOT GLOB '*[^0-9a-f]*'),
  CHECK (substr(event_id, 5) <> replace(printf('%32s', ''), ' ', substr(event_id, 5, 1))),
  CHECK (length(environment_namespace) BETWEEN 1 AND 64),
  CHECK (environment_namespace = trim(environment_namespace)),
  CHECK (environment_namespace = lower(environment_namespace)),
  CHECK (environment_namespace NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (event_version >= 1),
  CHECK (event_type IN ('binding_activated', 'binding_suspended', 'binding_reactivated', 'binding_released', 'administrative_correction')),
  CHECK (prior_status IS NULL OR prior_status IN ('active', 'suspended', 'released')),
  CHECK (resulting_status IN ('active', 'suspended', 'released')),
  CHECK (
    (event_type = 'binding_activated' AND prior_status IS NULL AND resulting_status = 'active')
    OR (event_type = 'binding_suspended' AND prior_status = 'active' AND resulting_status = 'suspended')
    OR (event_type = 'binding_reactivated' AND prior_status = 'suspended' AND resulting_status = 'active')
    OR (event_type = 'binding_released' AND prior_status IN ('active', 'suspended') AND resulting_status = 'released')
    OR (event_type = 'administrative_correction' AND prior_status IS NOT NULL)
  ),
  CHECK (actor_type IN ('authenticated_user', 'administrator', 'system', 'migration')),
  CHECK (length(reason_code) BETWEEN 1 AND 128 AND reason_code = trim(reason_code) AND reason_code = lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  CHECK (approval_reference IS NULL OR (length(approval_reference) BETWEEN 1 AND 128 AND approval_reference = trim(approval_reference) AND approval_reference NOT GLOB '*[^0-9A-Za-z_.:-]*')),
  CHECK (length(idempotency_key) BETWEEN 16 AND 256 AND idempotency_key = trim(idempotency_key)),
  CHECK (length(request_fingerprint) = 64 AND request_fingerprint = lower(request_fingerprint) AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(occurred_at) = 25 AND strftime('%Y-%m-%dT%H:%M:%S+00:00', occurred_at) IS occurred_at),
  CHECK (length(CAST(metadata_json AS BLOB)) <= 4096),
  CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object')
);

CREATE INDEX idx_product_principals_environment_type
ON product_principals(environment_namespace, principal_type, lifecycle_status);

CREATE INDEX idx_legacy_owner_aliases_principal
ON legacy_owner_aliases(principal_id, alias_kind);

CREATE INDEX idx_legacy_owner_aliases_family_coherence
ON legacy_owner_aliases(environment_namespace, alias_family, alias_value, principal_id);

CREATE INDEX idx_principal_account_bindings_user_status
ON principal_account_bindings(user_id, binding_status, binding_role);

CREATE UNIQUE INDEX idx_principal_account_bindings_active_identity
ON principal_account_bindings(principal_id, user_id, binding_role)
WHERE binding_status = 'active';

CREATE INDEX idx_ownership_binding_events_principal_version
ON ownership_binding_events(principal_id, event_version);

CREATE INDEX idx_ownership_binding_events_binding_time
ON ownership_binding_events(binding_id, occurred_at, event_version);

CREATE TRIGGER trg_product_principals_insert_guard
BEFORE INSERT ON product_principals
BEGIN
  SELECT CASE WHEN NEW.version <> 1
    THEN RAISE(ABORT, 'product principal version must begin at one') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.provenance_json) node
    WHERE (node.key IS NOT NULL AND typeof(node.key) = 'text' AND (
      length(node.key) NOT BETWEEN 1 AND 128
      OR node.key GLOB '*[^ -~]*'
      OR replace(replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', ''), char(9), '')
        IN ('authorization','authorizationheader','authenticationheader','bearer','cookie','password','secret','token','tokenhash','tokenmaterial','tokensecret','tokenvalue','sessiontoken','csrf','csrfmaterial','invitationhmac','providersubject','resume','resumecontent','rawclaim','rawclaims','rawhtml','rawapplicationcontent','applicationcontent','databasepath','email','oauth','oauthclaim','oauthclaims','sql','sqlquery','credential')
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') LIKE '%token'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authorization*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authenticationheader*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'email*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'oauth*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'csrf*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'invitationhmac*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'providersubject*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'resume*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawclaim*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawapplicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'applicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'sessiontoken*'
    ))
    OR (node.type = 'text' AND (length(node.atom) > 1024 OR lower(node.atom) GLOB '*bearer [0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-]*'))
    OR node.type NOT IN ('object','array','text','integer','real','true','false','null')
  ) THEN RAISE(ABORT, 'ownership metadata violates privacy policy') END;
  SELECT CASE WHEN (SELECT COUNT(*) FROM json_tree(NEW.provenance_json)) > 512
    THEN RAISE(ABORT, 'ownership metadata contains too many values') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.provenance_json)
    WHERE (length(path) - length(replace(path, '.', '')))
      + (length(path) - length(replace(path, '[', ''))) > 8
  ) THEN RAISE(ABORT, 'ownership metadata exceeds allowed depth') END;
  SELECT CASE WHEN EXISTS (
    SELECT parent FROM json_tree(NEW.provenance_json)
    WHERE parent IS NOT NULL GROUP BY parent HAVING COUNT(*) > 64
  ) THEN RAISE(ABORT, 'ownership metadata collection is too large') END;
END;

CREATE TRIGGER trg_product_principals_identity_immutable
BEFORE UPDATE OF principal_id, environment_namespace, principal_type, created_at
ON product_principals
BEGIN
  SELECT RAISE(ABORT, 'product principal identity is immutable');
END;

CREATE TRIGGER trg_product_principals_update_guard
BEFORE UPDATE ON product_principals
BEGIN
  SELECT CASE WHEN NEW.version <> OLD.version + 1
    THEN RAISE(ABORT, 'product principal version must advance once') END;
  SELECT CASE WHEN julianday(NEW.updated_at) < julianday(OLD.updated_at)
    THEN RAISE(ABORT, 'product principal time cannot move backward') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM legacy_owner_aliases alias
    WHERE alias.principal_id = OLD.principal_id
      AND alias.claimability <> NEW.claim_policy
  ) THEN RAISE(ABORT, 'product principal claim policy conflicts with aliases') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.provenance_json) node
    WHERE (node.key IS NOT NULL AND typeof(node.key) = 'text' AND (
      length(node.key) NOT BETWEEN 1 AND 128 OR node.key GLOB '*[^ -~]*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') IN ('authorization','authorizationheader','authenticationheader','bearer','cookie','password','secret','token','tokenhash','tokenmaterial','tokensecret','tokenvalue','sessiontoken','csrf','csrfmaterial','invitationhmac','providersubject','resume','resumecontent','rawclaim','rawclaims','rawhtml','rawapplicationcontent','applicationcontent','databasepath','email','oauth','oauthclaim','oauthclaims','sql','sqlquery','credential')
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') LIKE '%token'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authorization*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authenticationheader*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'email*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'oauth*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'csrf*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'invitationhmac*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'providersubject*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'resume*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawclaim*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawapplicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'applicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'sessiontoken*'
    )) OR (node.type = 'text' AND (length(node.atom) > 1024 OR lower(node.atom) GLOB '*bearer [0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-]*'))
  ) THEN RAISE(ABORT, 'ownership metadata violates privacy policy') END;
  SELECT CASE WHEN (SELECT COUNT(*) FROM json_tree(NEW.provenance_json)) > 512
    THEN RAISE(ABORT, 'ownership metadata contains too many values') END;
  SELECT CASE WHEN EXISTS (SELECT 1 FROM json_tree(NEW.provenance_json) WHERE (length(path)-length(replace(path,'.',''))) + (length(path)-length(replace(path,'[',''))) > 8)
    THEN RAISE(ABORT, 'ownership metadata exceeds allowed depth') END;
  SELECT CASE WHEN EXISTS (SELECT parent FROM json_tree(NEW.provenance_json) WHERE parent IS NOT NULL GROUP BY parent HAVING COUNT(*) > 64)
    THEN RAISE(ABORT, 'ownership metadata collection is too large') END;
END;

CREATE TRIGGER trg_product_principals_no_delete
BEFORE DELETE ON product_principals
BEGIN
  SELECT RAISE(ABORT, 'product principals cannot be deleted');
END;

CREATE TRIGGER trg_legacy_owner_aliases_insert_guard
BEFORE INSERT ON legacy_owner_aliases
BEGIN
  SELECT CASE WHEN EXISTS (
    WITH RECURSIVE positions(position) AS (
      SELECT 1 UNION ALL SELECT position + 1 FROM positions WHERE position < length(NEW.alias_value)
    )
    SELECT 1 FROM positions
    WHERE unicode(substr(NEW.alias_value, position, 1)) BETWEEN 0 AND 31
       OR unicode(substr(NEW.alias_value, position, 1)) BETWEEN 127 AND 159
  ) THEN RAISE(ABORT, 'legacy alias contains control characters') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM product_principals principal
    WHERE principal.principal_id = NEW.principal_id
      AND principal.environment_namespace = NEW.environment_namespace
      AND principal.claim_policy = NEW.claimability
      AND julianday(NEW.created_at) >= julianday(principal.created_at)
      AND (principal.principal_type NOT IN ('development', 'sample') OR NEW.claimability = 'nonclaimable')
      AND (NEW.alias_value <> 'local_user' OR (
        NEW.alias_family = 'owner_resource'
        AND principal.principal_type = 'development'
        AND NEW.claimability = 'nonclaimable'
      ))
  ) THEN RAISE(ABORT, 'legacy alias does not match its principal') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM legacy_owner_aliases existing
    WHERE existing.environment_namespace = NEW.environment_namespace
      AND existing.alias_family = NEW.alias_family
      AND existing.alias_value = NEW.alias_value
      AND existing.principal_id <> NEW.principal_id
  ) THEN RAISE(ABORT, 'legacy alias family is already owned') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.provenance_json) node
    WHERE (node.key IS NOT NULL AND typeof(node.key) = 'text' AND (
      length(node.key) NOT BETWEEN 1 AND 128 OR node.key GLOB '*[^ -~]*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') IN ('authorization','authorizationheader','authenticationheader','bearer','cookie','password','secret','token','tokenhash','tokenmaterial','tokensecret','tokenvalue','sessiontoken','csrf','csrfmaterial','invitationhmac','providersubject','resume','resumecontent','rawclaim','rawclaims','rawhtml','rawapplicationcontent','applicationcontent','databasepath','email','oauth','oauthclaim','oauthclaims','sql','sqlquery','credential')
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') LIKE '%token'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authorization*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authenticationheader*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'email*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'oauth*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'csrf*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'invitationhmac*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'providersubject*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'resume*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawclaim*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawapplicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'applicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'sessiontoken*'
    )) OR (node.type = 'text' AND (length(node.atom) > 1024 OR lower(node.atom) GLOB '*bearer [0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-]*'))
  ) THEN RAISE(ABORT, 'ownership metadata violates privacy policy') END;
  SELECT CASE WHEN (SELECT COUNT(*) FROM json_tree(NEW.provenance_json)) > 512 THEN RAISE(ABORT, 'ownership metadata contains too many values') END;
  SELECT CASE WHEN EXISTS (SELECT 1 FROM json_tree(NEW.provenance_json) WHERE (length(path)-length(replace(path,'.',''))) + (length(path)-length(replace(path,'[',''))) > 8) THEN RAISE(ABORT, 'ownership metadata exceeds allowed depth') END;
  SELECT CASE WHEN EXISTS (SELECT parent FROM json_tree(NEW.provenance_json) WHERE parent IS NOT NULL GROUP BY parent HAVING COUNT(*) > 64) THEN RAISE(ABORT, 'ownership metadata collection is too large') END;
END;

CREATE TRIGGER trg_legacy_owner_aliases_no_update
BEFORE UPDATE ON legacy_owner_aliases
BEGIN
  SELECT RAISE(ABORT, 'legacy owner aliases are append-only');
END;

CREATE TRIGGER trg_legacy_owner_aliases_no_delete
BEFORE DELETE ON legacy_owner_aliases
BEGIN
  SELECT RAISE(ABORT, 'legacy owner aliases are append-only');
END;

CREATE TRIGGER trg_principal_account_bindings_insert_guard
BEFORE INSERT ON principal_account_bindings
BEGIN
  SELECT CASE WHEN NEW.version <> 1 OR NEW.latest_event_version <> 1 THEN RAISE(ABORT, 'binding version must begin at one') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM product_principals principal JOIN users account ON account.user_id = NEW.user_id
    WHERE principal.principal_id = NEW.principal_id
      AND principal.environment_namespace = NEW.environment_namespace
      AND julianday(NEW.created_at) >= julianday(principal.created_at)
      AND julianday(NEW.created_at) >= julianday(account.created_at)
      AND (NEW.binding_status <> 'active' OR (
        principal.lifecycle_status = 'active' AND account.lifecycle_status = 'active'
        AND principal.claim_policy <> 'nonclaimable'
      ))
  ) THEN RAISE(ABORT, 'binding does not match an available principal and account') END;
  SELECT CASE WHEN NEW.binding_role = 'owner' AND NEW.binding_status = 'active' AND EXISTS (
    SELECT 1 FROM product_principals principal
    JOIN principal_account_bindings existing ON existing.principal_id = principal.principal_id
    WHERE principal.principal_id = NEW.principal_id
      AND principal.exclusive_account_binding = 1
      AND existing.binding_role = 'owner' AND existing.binding_status = 'active'
  ) THEN RAISE(ABORT, 'exclusive principal already has an active owner') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.provenance_json) node
    WHERE (node.key IS NOT NULL AND typeof(node.key) = 'text' AND (
      length(node.key) NOT BETWEEN 1 AND 128 OR node.key GLOB '*[^ -~]*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') IN ('authorization','authorizationheader','authenticationheader','bearer','cookie','password','secret','token','tokenhash','tokenmaterial','tokensecret','tokenvalue','sessiontoken','csrf','csrfmaterial','invitationhmac','providersubject','resume','resumecontent','rawclaim','rawclaims','rawhtml','rawapplicationcontent','applicationcontent','databasepath','email','oauth','oauthclaim','oauthclaims','sql','sqlquery','credential')
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') LIKE '%token'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authorization*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authenticationheader*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'email*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'oauth*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'csrf*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'invitationhmac*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'providersubject*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'resume*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawclaim*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawapplicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'applicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'sessiontoken*'
    )) OR (node.type = 'text' AND (length(node.atom) > 1024 OR lower(node.atom) GLOB '*bearer [0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-]*'))
  ) THEN RAISE(ABORT, 'ownership metadata violates privacy policy') END;
  SELECT CASE WHEN (SELECT COUNT(*) FROM json_tree(NEW.provenance_json)) > 512 THEN RAISE(ABORT, 'ownership metadata contains too many values') END;
  SELECT CASE WHEN EXISTS (SELECT 1 FROM json_tree(NEW.provenance_json) WHERE (length(path)-length(replace(path,'.',''))) + (length(path)-length(replace(path,'[',''))) > 8) THEN RAISE(ABORT, 'ownership metadata exceeds allowed depth') END;
  SELECT CASE WHEN EXISTS (SELECT parent FROM json_tree(NEW.provenance_json) WHERE parent IS NOT NULL GROUP BY parent HAVING COUNT(*) > 64) THEN RAISE(ABORT, 'ownership metadata collection is too large') END;
END;

CREATE TRIGGER trg_principal_account_bindings_update_guard
BEFORE UPDATE ON principal_account_bindings
BEGIN
  SELECT CASE WHEN NEW.binding_id <> OLD.binding_id OR NEW.principal_id <> OLD.principal_id
    OR NEW.user_id <> OLD.user_id OR NEW.environment_namespace <> OLD.environment_namespace
    OR NEW.binding_role <> OLD.binding_role OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'binding identity is immutable') END;
  SELECT CASE WHEN NEW.version <> OLD.version + 1 OR NEW.latest_event_version <> OLD.latest_event_version + 1
    THEN RAISE(ABORT, 'binding version must advance once') END;
  SELECT CASE WHEN OLD.binding_status = 'released' AND NEW.binding_status <> 'released'
    THEN RAISE(ABORT, 'released binding is terminal') END;
  SELECT CASE WHEN julianday(NEW.updated_at) < julianday(OLD.updated_at)
    THEN RAISE(ABORT, 'binding time cannot move backward') END;
  SELECT CASE WHEN NEW.binding_status = 'active' AND NOT EXISTS (
    SELECT 1 FROM product_principals principal JOIN users account ON account.user_id = NEW.user_id
    WHERE principal.principal_id = NEW.principal_id
      AND principal.lifecycle_status = 'active' AND principal.claim_policy <> 'nonclaimable'
      AND account.lifecycle_status = 'active'
  ) THEN RAISE(ABORT, 'active binding requires available principal and account') END;
  SELECT CASE WHEN NEW.binding_role = 'owner' AND NEW.binding_status = 'active' AND EXISTS (
    SELECT 1 FROM product_principals principal
    JOIN principal_account_bindings existing ON existing.principal_id = principal.principal_id
    WHERE principal.principal_id = NEW.principal_id AND principal.exclusive_account_binding = 1
      AND existing.binding_id <> OLD.binding_id
      AND existing.binding_role = 'owner' AND existing.binding_status = 'active'
  ) THEN RAISE(ABORT, 'exclusive principal already has an active owner') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.provenance_json) node
    WHERE (node.key IS NOT NULL AND typeof(node.key) = 'text' AND (
      length(node.key) NOT BETWEEN 1 AND 128 OR node.key GLOB '*[^ -~]*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') IN ('authorization','authorizationheader','authenticationheader','bearer','cookie','password','secret','token','tokenhash','tokenmaterial','tokensecret','tokenvalue','sessiontoken','csrf','csrfmaterial','invitationhmac','providersubject','resume','resumecontent','rawclaim','rawclaims','rawhtml','rawapplicationcontent','applicationcontent','databasepath','email','oauth','oauthclaim','oauthclaims','sql','sqlquery','credential')
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') LIKE '%token'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authorization*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authenticationheader*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'email*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'oauth*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'csrf*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'invitationhmac*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'providersubject*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'resume*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawclaim*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawapplicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'applicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'sessiontoken*'
    )) OR (node.type = 'text' AND (length(node.atom) > 1024 OR lower(node.atom) GLOB '*bearer [0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-]*'))
  ) THEN RAISE(ABORT, 'ownership metadata violates privacy policy') END;
  SELECT CASE WHEN (SELECT COUNT(*) FROM json_tree(NEW.provenance_json)) > 512 THEN RAISE(ABORT, 'ownership metadata contains too many values') END;
  SELECT CASE WHEN EXISTS (SELECT 1 FROM json_tree(NEW.provenance_json) WHERE (length(path)-length(replace(path,'.',''))) + (length(path)-length(replace(path,'[',''))) > 8) THEN RAISE(ABORT, 'ownership metadata exceeds allowed depth') END;
  SELECT CASE WHEN EXISTS (SELECT parent FROM json_tree(NEW.provenance_json) WHERE parent IS NOT NULL GROUP BY parent HAVING COUNT(*) > 64) THEN RAISE(ABORT, 'ownership metadata collection is too large') END;
END;

CREATE TRIGGER trg_principal_account_bindings_no_delete
BEFORE DELETE ON principal_account_bindings
BEGIN
  SELECT RAISE(ABORT, 'principal account bindings cannot be deleted');
END;

CREATE TRIGGER trg_ownership_binding_events_insert_guard
BEFORE INSERT ON ownership_binding_events
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM principal_account_bindings binding
    JOIN product_principals principal ON principal.principal_id = binding.principal_id
    JOIN users account ON account.user_id = binding.user_id
    WHERE binding.binding_id = NEW.binding_id AND binding.principal_id = NEW.principal_id
      AND binding.user_id = NEW.user_id AND binding.environment_namespace = NEW.environment_namespace
      AND principal.environment_namespace = NEW.environment_namespace
      AND julianday(NEW.occurred_at) >= julianday(binding.created_at)
      AND julianday(NEW.occurred_at) >= julianday(principal.created_at)
      AND julianday(NEW.occurred_at) >= julianday(account.created_at)
  ) THEN RAISE(ABORT, 'binding event relation or time is invalid') END;
  SELECT CASE WHEN
    (NOT EXISTS (SELECT 1 FROM ownership_binding_events WHERE binding_id = NEW.binding_id)
      AND NOT (NEW.event_version = 1 AND NEW.event_type = 'binding_activated'
        AND NEW.prior_status IS NULL AND NEW.resulting_status = 'active'))
    OR (EXISTS (SELECT 1 FROM ownership_binding_events WHERE binding_id = NEW.binding_id) AND (
      NEW.event_version <> (SELECT MAX(event_version) + 1 FROM ownership_binding_events WHERE binding_id = NEW.binding_id)
      OR NEW.prior_status <> (SELECT resulting_status FROM ownership_binding_events WHERE binding_id = NEW.binding_id ORDER BY event_version DESC LIMIT 1)
      OR julianday(NEW.occurred_at) < julianday((SELECT occurred_at FROM ownership_binding_events WHERE binding_id = NEW.binding_id ORDER BY event_version DESC LIMIT 1))
    )) THEN RAISE(ABORT, 'binding event history must be contiguous') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_tree(NEW.metadata_json) node
    WHERE (node.key IS NOT NULL AND typeof(node.key) = 'text' AND (
      length(node.key) NOT BETWEEN 1 AND 128 OR node.key GLOB '*[^ -~]*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') IN ('authorization','authorizationheader','authenticationheader','bearer','cookie','password','secret','token','tokenhash','tokenmaterial','tokensecret','tokenvalue','sessiontoken','csrf','csrfmaterial','invitationhmac','providersubject','resume','resumecontent','rawclaim','rawclaims','rawhtml','rawapplicationcontent','applicationcontent','databasepath','email','oauth','oauthclaim','oauthclaims','sql','sqlquery','credential')
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') LIKE '%token'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authorization*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'authenticationheader*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'email*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'oauth*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'csrf*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'invitationhmac*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'providersubject*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'resume*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawclaim*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'rawapplicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'applicationcontent*'
      OR replace(replace(replace(replace(replace(replace(lower(node.key), '-', ''), '_', ''), '.', ''), ' ', ''), '/', ''), ':', '') GLOB 'sessiontoken*'
    )) OR (node.type = 'text' AND (length(node.atom) > 1024 OR lower(node.atom) GLOB '*bearer [0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-][0-9a-z._~+/-]*'))
  ) THEN RAISE(ABORT, 'ownership metadata violates privacy policy') END;
  SELECT CASE WHEN (SELECT COUNT(*) FROM json_tree(NEW.metadata_json)) > 512 THEN RAISE(ABORT, 'ownership metadata contains too many values') END;
  SELECT CASE WHEN EXISTS (SELECT 1 FROM json_tree(NEW.metadata_json) WHERE (length(path)-length(replace(path,'.',''))) + (length(path)-length(replace(path,'[',''))) > 8) THEN RAISE(ABORT, 'ownership metadata exceeds allowed depth') END;
  SELECT CASE WHEN EXISTS (SELECT parent FROM json_tree(NEW.metadata_json) WHERE parent IS NOT NULL GROUP BY parent HAVING COUNT(*) > 64) THEN RAISE(ABORT, 'ownership metadata collection is too large') END;
END;

CREATE TRIGGER trg_ownership_binding_events_no_update
BEFORE UPDATE ON ownership_binding_events
BEGIN
  SELECT RAISE(ABORT, 'ownership binding events are append-only');
END;

CREATE TRIGGER trg_ownership_binding_events_no_delete
BEFORE DELETE ON ownership_binding_events
BEGIN
  SELECT RAISE(ABORT, 'ownership binding events are append-only');
END;
