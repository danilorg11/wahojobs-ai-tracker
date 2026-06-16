PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  careers_url TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL,
  external_id TEXT,
  title TEXT NOT NULL,
  location TEXT,
  department TEXT,
  expertise TEXT,
  commitment TEXT,
  url TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  removed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (company_id) REFERENCES companies(id),
  UNIQUE (company_id, source_hash)
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

CREATE INDEX IF NOT EXISTS idx_jobs_company_active
ON jobs(company_id, is_active);

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
