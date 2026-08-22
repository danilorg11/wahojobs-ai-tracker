import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

const raw = readFileSync(new URL('../release-manifest.json', import.meta.url), 'utf8');
const document = JSON.parse(raw);
const sha256 = /^[0-9a-f]{64}$/;
const canary = Object.freeze({
  path: '/job/handshake-ai-evaluation-specialist-j125e8ced56da8007c92ab964f58f9f0f',
  public_job_id: 'j125e8ced56da8007c92ab964f58f9f0f',
  canonical_key:
    'raw::fb45713051f6db962b98c9ba2ef14ad27c795a231530eb4ba3e03c91e41e6109',
});

function exactKeys(value, keys) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort())
  );
}

if (
  !exactKeys(document, [
    'format',
    'release_id',
    'database_sha256',
    'registry_sha256',
    'published_details',
  ]) ||
  document.format !== 'wahojobs-public-job-production-release-v1' ||
  !sha256.test(document.release_id) ||
  !sha256.test(document.database_sha256) ||
  !sha256.test(document.registry_sha256) ||
  !Array.isArray(document.published_details) ||
  document.published_details.length !== 1
) {
  throw new Error('invalid_production_release');
}

const detail = document.published_details[0];
if (
  !exactKeys(detail, ['path', 'public_job_id', 'canonical_key']) ||
  detail.path !== canary.path ||
  detail.public_job_id !== canary.public_job_id ||
  detail.canonical_key !== canary.canonical_key
) {
  throw new Error('invalid_production_release_scope');
}

const payload = {
  database_sha256: document.database_sha256,
  format: document.format,
  published_details: [
    {
      canonical_key: detail.canonical_key,
      path: detail.path,
      public_job_id: detail.public_job_id,
    },
  ],
  registry_sha256: document.registry_sha256,
};
const expectedReleaseId = createHash('sha256')
  .update(JSON.stringify(payload) + '\n', 'ascii')
  .digest('hex');
if (expectedReleaseId !== document.release_id) {
  throw new Error('production_release_digest_mismatch');
}

export const release = Object.freeze({
  format: document.format,
  releaseId: document.release_id,
  databaseSha256: document.database_sha256,
  registrySha256: document.registry_sha256,
  publishedDetails: Object.freeze([Object.freeze({ ...detail })]),
});
export const publishedPathSet = new Set([detail.path]);
