CREATE TABLE companies_m007_backup AS
SELECT
  id,
  name,
  slug,
  careers_url,
  source_tier,
  inventory_model,
  market_count_policy,
  created_at,
  updated_at
FROM companies;

CREATE TABLE jobs_m007_backup AS
SELECT
  id,
  company_id,
  canonical_opportunity_id,
  external_id,
  title,
  location,
  department,
  expertise,
  commitment,
  url,
  source_hash,
  opportunity_kind,
  availability_basis,
  include_in_live_market_estimate,
  first_seen_at,
  last_seen_at,
  is_active,
  removed_at,
  created_at,
  updated_at
FROM jobs;

DROP TABLE jobs;

DROP TABLE companies;

CREATE TABLE companies (
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

INSERT INTO companies (
  id,
  name,
  slug,
  careers_url,
  source_tier,
  inventory_model,
  market_count_policy,
  created_at,
  updated_at
)
SELECT
  id,
  name,
  slug,
  careers_url,
  source_tier,
  inventory_model,
  market_count_policy,
  created_at,
  updated_at
FROM companies_m007_backup;

CREATE TABLE jobs (
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

INSERT INTO jobs (
  id,
  company_id,
  canonical_opportunity_id,
  external_id,
  title,
  location,
  department,
  expertise,
  commitment,
  url,
  source_hash,
  opportunity_kind,
  availability_basis,
  include_in_live_market_estimate,
  first_seen_at,
  last_seen_at,
  is_active,
  removed_at,
  created_at,
  updated_at
)
SELECT
  id,
  company_id,
  canonical_opportunity_id,
  external_id,
  title,
  location,
  department,
  expertise,
  commitment,
  url,
  source_hash,
  opportunity_kind,
  availability_basis,
  include_in_live_market_estimate,
  first_seen_at,
  last_seen_at,
  is_active,
  removed_at,
  created_at,
  updated_at
FROM jobs_m007_backup;

CREATE INDEX idx_jobs_company_active
ON jobs(company_id, is_active);

CREATE INDEX idx_jobs_first_seen_at
ON jobs(first_seen_at);

CREATE INDEX idx_jobs_last_seen_at
ON jobs(last_seen_at);

CREATE INDEX idx_jobs_live_market
        ON jobs(include_in_live_market_estimate, is_active)
        ;

CREATE INDEX idx_jobs_canonical_opportunity
        ON jobs(canonical_opportunity_id)
        ;

DROP TABLE jobs_m007_backup;

DROP TABLE companies_m007_backup;
