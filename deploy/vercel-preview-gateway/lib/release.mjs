import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

const raw = readFileSync(new URL('../release-manifest.json', import.meta.url), 'utf8');
const document = JSON.parse(raw);
const sha256 = /^[0-9a-f]{64}$/;
const publicJobId = /^j[0-9a-f]{32}$/;
const newPath = /^\/job\/[a-z0-9]+(?:-[a-z0-9]+)*-j[0-9a-f]{32}$/;
const karlPath = '/job/oneforma-karl-llm-1';
const karlId = 'j7b8550e11700c9b26ac68deb753e1f82';
const karlCanonicalKey = 'oneforma::177080';

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
  document.format !== 'wahojobs-public-job-preview-release-v1' ||
  !sha256.test(document.release_id) ||
  !sha256.test(document.database_sha256) ||
  !sha256.test(document.registry_sha256) ||
  !Array.isArray(document.published_details) ||
  document.published_details.length !== 2
) {
  throw new Error('invalid_preview_release');
}

const details = document.published_details
  .map((item) => {
    if (
      !exactKeys(item, ['path', 'public_job_id', 'canonical_key']) ||
      typeof item.path !== 'string' ||
      !publicJobId.test(item.public_job_id) ||
      typeof item.canonical_key !== 'string' ||
      !item.canonical_key
    ) {
      throw new Error('invalid_preview_release');
    }
    return Object.freeze({ ...item });
  })
  .sort((left, right) => left.path.localeCompare(right.path));

const paths = details.map((item) => item.path);
if (
  new Set(paths).size !== 2 ||
  new Set(details.map((item) => item.public_job_id)).size !== 2 ||
  paths.filter((path) => path === karlPath).length !== 1 ||
  paths.filter(
    (path) => {
      const item = details.find((detail) => detail.path === path);
      return (
        path !== karlPath &&
        newPath.test(path) &&
        Buffer.byteLength(path) <= 119 &&
        path.endsWith(`-${item.public_job_id}`)
      );
    },
  ).length !== 1
) {
  throw new Error('invalid_preview_release_scope');
}
const karl = details.find((item) => item.path === karlPath);
if (
  karl.public_job_id !== karlId ||
  karl.canonical_key !== karlCanonicalKey
) {
  throw new Error('invalid_preview_release_scope');
}

const payload = {
  database_sha256: document.database_sha256,
  format: document.format,
  published_details: details.map((item) => ({
    canonical_key: item.canonical_key,
    path: item.path,
    public_job_id: item.public_job_id,
  })),
  registry_sha256: document.registry_sha256,
};
const expectedReleaseId = createHash('sha256')
  .update(JSON.stringify(payload) + '\n', 'ascii')
  .digest('hex');
if (expectedReleaseId !== document.release_id) {
  throw new Error('preview_release_digest_mismatch');
}

export const release = Object.freeze({
  format: document.format,
  releaseId: document.release_id,
  databaseSha256: document.database_sha256,
  registrySha256: document.registry_sha256,
  publishedDetails: Object.freeze(details),
});
export const publishedPathSet = new Set(paths);
