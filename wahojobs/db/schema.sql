PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  careers_url TEXT NOT NULL,
  source_tier TEXT NOT NULL DEFAULT 'core',
  inventory_model TEXT NOT NULL DEFAULT 'live_feed',
  market_count_policy TEXT NOT NULL DEFAULT 'count_live',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL,
  canonical_opportunity_id INTEGER,
  external_id TEXT,
  title TEXT NOT NULL,
  location TEXT,
  department TEXT,
  expertise TEXT,
  commitment TEXT,
  url TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  opportunity_kind TEXT NOT NULL DEFAULT 'live_posting',
  availability_basis TEXT NOT NULL DEFAULT 'api_feed',
  include_in_live_market_estimate INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  removed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (company_id) REFERENCES companies(id),
  FOREIGN KEY (canonical_opportunity_id) REFERENCES canonical_opportunities(id),
  UNIQUE (company_id, source_hash)
);

CREATE TABLE IF NOT EXISTS canonical_opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL,
  canonical_key TEXT NOT NULL,
  canonical_title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  source_category TEXT NOT NULL,
  language TEXT,
  language_locale TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  variant_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (company_id) REFERENCES companies(id),
  UNIQUE (company_id, canonical_key)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  jobs_found_count INTEGER NOT NULL DEFAULT 0,
  jobs_new_count INTEGER NOT NULL DEFAULT 0,
  jobs_reactivated_count INTEGER NOT NULL DEFAULT 0,
  jobs_updated_count INTEGER NOT NULL DEFAULT 0,
  jobs_removed_count INTEGER NOT NULL DEFAULT 0,
  used_sample_data INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (company_id) REFERENCES companies(id)
);

CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  crawl_run_id INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('discovered', 'removed', 'reactivated')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (job_id) REFERENCES jobs(id),
  FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id)
);

CREATE TABLE IF NOT EXISTS user_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  profile_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  education_level TEXT,
  degrees_or_domains_json TEXT NOT NULL DEFAULT '[]',
  languages_json TEXT NOT NULL DEFAULT '[]',
  skills_json TEXT NOT NULL DEFAULT '[]',
  work_preferences_json TEXT NOT NULL DEFAULT '[]',
  constraints_json TEXT NOT NULL DEFAULT '[]',
  target_opportunity_types_json TEXT NOT NULL DEFAULT '[]',
  notes TEXT,
  is_sample INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_pipeline_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pipeline_item_id TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  source TEXT NOT NULL,
  opportunity_title TEXT NOT NULL,
  opportunity_url TEXT,
  opportunity_external_id TEXT,
  canonical_id INTEGER,
  status TEXT NOT NULL,
  status_date TEXT,
  user_priority TEXT,
  reminder_date TEXT,
  notes TEXT,
  last_user_action TEXT,
  is_sample INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS applicant_status_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  update_id TEXT NOT NULL UNIQUE,
  user_id TEXT,
  anonymous_user_key TEXT,
  profile_id TEXT NOT NULL,
  source TEXT NOT NULL,
  opportunity_title TEXT NOT NULL,
  opportunity_url TEXT,
  opportunity_external_id TEXT,
  canonical_id INTEGER,
  status TEXT NOT NULL,
  previous_status TEXT,
  status_date TEXT NOT NULL,
  reported_at TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  confidence_level TEXT NOT NULL,
  notes TEXT,
  is_sample INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_company_active
ON jobs(company_id, is_active);

CREATE INDEX IF NOT EXISTS idx_canonical_opportunities_company_active
ON canonical_opportunities(company_id, is_active);

CREATE INDEX IF NOT EXISTS idx_jobs_first_seen_at
ON jobs(first_seen_at);

CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at
ON jobs(last_seen_at);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_company_started
ON crawl_runs(company_id, started_at);

CREATE INDEX IF NOT EXISTS idx_job_events_job
ON job_events(job_id);

CREATE INDEX IF NOT EXISTS idx_job_events_crawl_run
ON job_events(crawl_run_id);

CREATE INDEX IF NOT EXISTS idx_job_events_type_created
ON job_events(event_type, created_at);

CREATE INDEX IF NOT EXISTS idx_user_profiles_user
ON user_profiles(user_id);

CREATE INDEX IF NOT EXISTS idx_user_pipeline_items_profile_status
ON user_pipeline_items(profile_id, status);

CREATE INDEX IF NOT EXISTS idx_user_pipeline_items_source
ON user_pipeline_items(source);

CREATE INDEX IF NOT EXISTS idx_applicant_status_updates_profile
ON applicant_status_updates(profile_id);

CREATE INDEX IF NOT EXISTS idx_applicant_status_updates_source
ON applicant_status_updates(source);

CREATE INDEX IF NOT EXISTS idx_applicant_status_updates_reported
ON applicant_status_updates(reported_at);
