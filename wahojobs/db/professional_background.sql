-- Explicit companion database only; never installed in the product database.
CREATE TABLE preparation_meta (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  store_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 0)
);
CREATE TABLE preparation_results (
  request_id TEXT PRIMARY KEY CHECK (length(request_id) = 64),
  owner_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('attempted', 'validated', 'revoked')),
  record_json TEXT,
  record_sha256 TEXT,
  CHECK ((state = 'validated' AND record_json IS NOT NULL AND record_sha256 IS NOT NULL)
      OR (state <> 'validated' AND record_json IS NULL AND record_sha256 IS NULL)),
  CHECK (length(CAST(record_json AS BLOB)) <= 65536)
);
CREATE INDEX preparation_results_owner ON preparation_results(owner_json);
