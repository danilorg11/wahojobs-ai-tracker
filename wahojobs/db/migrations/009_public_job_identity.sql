CREATE TABLE public_job_identities (
  public_job_id TEXT PRIMARY KEY NOT NULL,
  disposition TEXT NOT NULL,
  redirect_target_public_job_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,

  FOREIGN KEY (redirect_target_public_job_id)
    REFERENCES public_job_identities(public_job_id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (length(public_job_id) = 33),
  CHECK (substr(public_job_id, 1, 1) = 'j'),
  CHECK (substr(public_job_id, 2) NOT GLOB '*[^0-9a-f]*'),
  CHECK (disposition IN ('serving', 'redirect', 'gone')),
  CHECK (
    (disposition = 'redirect'
      AND redirect_target_public_job_id IS NOT NULL
      AND redirect_target_public_job_id <> public_job_id)
    OR (disposition IN ('serving', 'gone')
      AND redirect_target_public_job_id IS NULL)
  ),
  CHECK (
    length(created_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at
  ),
  CHECK (
    length(updated_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at
  ),
  CHECK (julianday(updated_at) >= julianday(created_at))
);

CREATE TABLE public_job_paths (
  path TEXT PRIMARY KEY NOT NULL,
  normalized_path TEXT NOT NULL UNIQUE,
  public_job_id TEXT NOT NULL,
  path_role TEXT NOT NULL,
  created_at TEXT NOT NULL,

  FOREIGN KEY (public_job_id)
    REFERENCES public_job_identities(public_job_id) ON DELETE RESTRICT,
  CHECK (length(CAST(path AS BLOB)) BETWEEN 6 AND 2048),
  CHECK (lower(substr(path, 1, 5)) = '/job/'),
  CHECK (path = trim(path)),
  CHECK (path NOT GLOB '*[^ -~]*'),
  CHECK (instr(path, ' ') = 0),
  CHECK (instr(path, '%') = 0),
  CHECK (instr(path, char(92)) = 0),
  CHECK (instr(path, '?') = 0),
  CHECK (instr(path, '#') = 0),
  CHECK (instr(path, '//') = 0),
  CHECK (instr(path, '/./') = 0),
  CHECK (instr(path, '/../') = 0),
  CHECK (substr(path, -2) <> '/.'),
  CHECK (substr(path, -3) <> '/..'),
  CHECK (normalized_path = lower(path)),
  CHECK (path_role IN ('primary', 'alias')),
  CHECK (
    length(created_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at) IS created_at
  )
);

CREATE TABLE public_job_bindings (
  public_job_id TEXT PRIMARY KEY NOT NULL,
  canonical_opportunity_id INTEGER NOT NULL UNIQUE,
  binding_version INTEGER NOT NULL,
  bound_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,

  FOREIGN KEY (public_job_id)
    REFERENCES public_job_identities(public_job_id) ON DELETE RESTRICT,
  FOREIGN KEY (canonical_opportunity_id)
    REFERENCES canonical_opportunities(id) ON DELETE RESTRICT,
  CHECK (binding_version >= 1),
  CHECK (
    length(bound_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', bound_at) IS bound_at
  ),
  CHECK (
    length(updated_at) = 25
    AND strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at) IS updated_at
  ),
  CHECK (julianday(updated_at) >= julianday(bound_at))
);

CREATE UNIQUE INDEX idx_public_job_paths_one_primary
ON public_job_paths(public_job_id)
WHERE path_role = 'primary';

CREATE INDEX idx_public_job_paths_owner_role
ON public_job_paths(public_job_id, path_role);

CREATE INDEX idx_public_job_identities_redirect_target
ON public_job_identities(redirect_target_public_job_id)
WHERE redirect_target_public_job_id IS NOT NULL;

CREATE TRIGGER trg_public_job_identities_immutable_identity
BEFORE UPDATE OF public_job_id, created_at ON public_job_identities
BEGIN
  SELECT RAISE(ABORT, 'public job identity is immutable');
END;

CREATE TRIGGER trg_public_job_identities_update_guard
BEFORE UPDATE ON public_job_identities
BEGIN
  SELECT CASE
    WHEN julianday(NEW.updated_at) < julianday(OLD.updated_at)
    THEN RAISE(ABORT, 'public job identity time cannot move backward')
  END;
  SELECT CASE
    WHEN OLD.disposition = 'redirect'
      AND NEW.disposition <> 'redirect'
    THEN RAISE(ABORT, 'redirected public job identity is terminal')
  END;
END;

CREATE TRIGGER trg_public_job_identities_no_replace
BEFORE INSERT ON public_job_identities
WHEN EXISTS (
  SELECT 1
  FROM public_job_identities issued
  WHERE issued.public_job_id = NEW.public_job_id
)
BEGIN
  SELECT RAISE(ABORT, 'issued public job identity cannot be replaced');
END;

CREATE TRIGGER trg_public_job_identities_redirect_insert_guard
BEFORE INSERT ON public_job_identities
WHEN NEW.disposition = 'redirect'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM public_job_identities target
    WHERE target.public_job_id = NEW.redirect_target_public_job_id
      AND target.disposition = 'serving'
  ) THEN RAISE(ABORT, 'redirect target must be a serving identity') END;
END;

CREATE TRIGGER trg_public_job_identities_redirect_update_guard
BEFORE UPDATE OF disposition, redirect_target_public_job_id
ON public_job_identities
WHEN NEW.disposition = 'redirect'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM public_job_identities target
    WHERE target.public_job_id = NEW.redirect_target_public_job_id
      AND target.disposition = 'serving'
  ) THEN RAISE(ABORT, 'redirect target must be a serving identity') END;
END;

CREATE TRIGGER trg_public_job_identities_incoming_redirect_guard
BEFORE UPDATE OF disposition ON public_job_identities
WHEN OLD.disposition = 'serving' AND NEW.disposition <> 'serving'
BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM public_job_identities source
    WHERE source.redirect_target_public_job_id = OLD.public_job_id
  ) THEN RAISE(ABORT, 'incoming redirects must be reconciled first') END;
END;

CREATE TRIGGER trg_public_job_identities_retire_binding
AFTER UPDATE OF disposition ON public_job_identities
WHEN OLD.disposition <> 'redirect' AND NEW.disposition = 'redirect'
BEGIN
  DELETE FROM public_job_bindings
  WHERE public_job_id = NEW.public_job_id;
END;

CREATE TRIGGER trg_public_job_identities_no_delete
BEFORE DELETE ON public_job_identities
BEGIN
  SELECT RAISE(ABORT, 'public job identity cannot be deleted');
END;

CREATE TRIGGER trg_public_job_paths_no_update
BEFORE UPDATE ON public_job_paths
BEGIN
  SELECT RAISE(ABORT, 'public job path is immutable');
END;

CREATE TRIGGER trg_public_job_paths_no_replace
BEFORE INSERT ON public_job_paths
WHEN EXISTS (
  SELECT 1
  FROM public_job_paths issued
  WHERE issued.path = NEW.path
    OR issued.normalized_path = NEW.normalized_path
)
BEGIN
  SELECT RAISE(ABORT, 'issued public job path cannot be replaced');
END;

CREATE TRIGGER trg_public_job_paths_no_delete
BEFORE DELETE ON public_job_paths
BEGIN
  SELECT RAISE(ABORT, 'public job path cannot be deleted');
END;

CREATE TRIGGER trg_public_job_bindings_insert_guard
BEFORE INSERT ON public_job_bindings
BEGIN
  SELECT CASE
    WHEN NEW.binding_version <> 1
    THEN RAISE(ABORT, 'public job binding version must begin at one')
  END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM public_job_identities identity
    WHERE identity.public_job_id = NEW.public_job_id
      AND identity.disposition IN ('serving', 'gone')
  ) THEN RAISE(ABORT, 'public job binding owner is not bindable') END;
END;

CREATE TRIGGER trg_public_job_bindings_no_replace
BEFORE INSERT ON public_job_bindings
WHEN EXISTS (
  SELECT 1
  FROM public_job_bindings issued
  WHERE issued.public_job_id = NEW.public_job_id
    OR issued.canonical_opportunity_id = NEW.canonical_opportunity_id
)
BEGIN
  SELECT RAISE(ABORT, 'issued public job binding cannot be replaced');
END;

CREATE TRIGGER trg_public_job_bindings_update_guard
BEFORE UPDATE ON public_job_bindings
BEGIN
  SELECT CASE
    WHEN NEW.public_job_id <> OLD.public_job_id
      OR NEW.bound_at <> OLD.bound_at
    THEN RAISE(ABORT, 'public job binding identity is immutable')
  END;
  SELECT CASE
    WHEN NEW.binding_version <> OLD.binding_version + 1
    THEN RAISE(ABORT, 'public job binding version must advance once')
  END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM public_job_bindings owner
    WHERE owner.canonical_opportunity_id = NEW.canonical_opportunity_id
      AND owner.public_job_id <> OLD.public_job_id
  ) THEN RAISE(ABORT, 'canonical opportunity binding is already owned') END;
  SELECT CASE
    WHEN julianday(NEW.updated_at) < julianday(OLD.updated_at)
    THEN RAISE(ABORT, 'public job binding time cannot move backward')
  END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM public_job_identities identity
    WHERE identity.public_job_id = NEW.public_job_id
      AND identity.disposition IN ('serving', 'gone')
  ) THEN RAISE(ABORT, 'public job binding owner is not bindable') END;
END;

CREATE TRIGGER trg_public_job_bindings_no_delete
BEFORE DELETE ON public_job_bindings
WHEN NOT EXISTS (
  SELECT 1
  FROM public_job_identities identity
  WHERE identity.public_job_id = OLD.public_job_id
    AND identity.disposition = 'redirect'
)
BEGIN
  SELECT RAISE(ABORT, 'public job binding can retire only with its identity');
END;
