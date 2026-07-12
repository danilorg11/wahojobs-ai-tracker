CREATE UNIQUE INDEX idx_user_pipeline_items_pipeline_profile
ON user_pipeline_items(pipeline_item_id, profile_id);

CREATE TABLE user_pipeline_state (
  pipeline_item_id TEXT PRIMARY KEY,
  workflow_status TEXT,
  workflow_status_provenance TEXT NOT NULL,
  visibility TEXT NOT NULL,
  reminder_at TEXT,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (pipeline_item_id) REFERENCES user_pipeline_items(pipeline_item_id) ON DELETE RESTRICT,
  CHECK (
    workflow_status IS NULL OR workflow_status IN (
      'recommended',
      'saved',
      'applied',
      'waiting',
      'assessment_invited',
      'assessment_started',
      'assessment_completed',
      'accepted',
      'active_worker',
      'paid_task_received',
      'rejected',
      'expired'
    )
  ),
  CHECK (workflow_status_provenance IN ('known', 'inferred_legacy', 'unknown_legacy')),
  CHECK (visibility IN ('visible', 'hidden')),
  CHECK (version >= 1)
);

CREATE TABLE user_pipeline_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transition_id TEXT NOT NULL UNIQUE,
  pipeline_item_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  affected_dimension TEXT NOT NULL,
  action_name TEXT NOT NULL,
  before_state_json TEXT NOT NULL,
  after_state_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  actor_source TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  state_version_before INTEGER NOT NULL,
  state_version_after INTEGER NOT NULL,
  undo_of_transition_id TEXT,
  correction_of_transition_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (pipeline_item_id, profile_id)
    REFERENCES user_pipeline_items(pipeline_item_id, profile_id) ON DELETE RESTRICT,
  FOREIGN KEY (undo_of_transition_id) REFERENCES user_pipeline_transitions(transition_id) ON DELETE RESTRICT,
  FOREIGN KEY (correction_of_transition_id) REFERENCES user_pipeline_transitions(transition_id) ON DELETE RESTRICT,
  CHECK (
    affected_dimension IN (
      'baseline',
      'workflow',
      'visibility',
      'reminder',
      'undo',
      'correction'
    )
  ),
  CHECK (state_version_before >= 0),
  CHECK (state_version_after = state_version_before + 1),
  CHECK (undo_of_transition_id IS NULL OR correction_of_transition_id IS NULL)
);

CREATE INDEX idx_user_pipeline_transitions_pipeline_occurred
ON user_pipeline_transitions(pipeline_item_id, occurred_at, id);

CREATE INDEX idx_user_pipeline_transitions_profile_occurred
ON user_pipeline_transitions(profile_id, occurred_at, id);

CREATE INDEX idx_user_pipeline_transitions_undo
ON user_pipeline_transitions(undo_of_transition_id);

CREATE INDEX idx_user_pipeline_transitions_correction
ON user_pipeline_transitions(correction_of_transition_id);

CREATE INDEX idx_user_pipeline_transitions_occurred
ON user_pipeline_transitions(occurred_at, id);

CREATE TABLE wahojobs_schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trg_user_pipeline_transitions_no_update
BEFORE UPDATE ON user_pipeline_transitions
BEGIN
  SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only');
END;

CREATE TRIGGER trg_user_pipeline_transitions_no_delete
BEFORE DELETE ON user_pipeline_transitions
BEGIN
  SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only');
END;
