import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';

import detailHandler from '../deploy/vercel-production-gateway/api/job.mjs';
import jobsHandler from '../deploy/vercel-production-gateway/api/jobs.mjs';
import {
  publishedPathSet,
  release,
} from '../deploy/vercel-production-gateway/lib/release.mjs';

const TOKEN = 'P'.repeat(43);
const CANARY =
  '/job/handshake-ai-evaluation-specialist-j125e8ced56da8007c92ab964f58f9f0f';
const KARL = '/job/oneforma-karl-llm-1';
const PRESERVATION_PATHS = [
  '/',
  KARL,
  '/job/freecash-multi-task-contributor-1',
  '/job/oneforma-atlas-creator-1',
  '/remote-companies',
  '/company/oneforma',
  '/online-jobs/ai-training',
  '/blog',
  '/blog/ai-training',
  '/editorial-guidelines',
  '/robots.txt',
  '/sitemap.xml',
  '/sitemaps/static.xml',
  '/sitemaps/jobs.xml',
  '/sitemaps/companies.xml',
];

function enableProduction() {
  process.env.VERCEL_ENV = 'production';
  process.env.WAHOJOBS_PRODUCTION_PUBLIC_ROUTES_ENABLED = '1';
  process.env.WAHOJOBS_NEW_ORIGIN_URL = 'https://catalog-origin.example.test';
  process.env.WAHOJOBS_ORIGIN_AUTH_TOKEN = TOKEN;
}

function originResponse(body = '<title>Jobs</title>', status = 200) {
  return new Response(body, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'x-wahojobs-origin': 'public-catalog-production',
      'x-wahojobs-release-id': release.releaseId,
    },
  });
}

function responseHarness() {
  return {
    headers: new Map(),
    statusCode: 200,
    body: Buffer.alloc(0),
    setHeader(name, value) {
      this.headers.set(name.toLowerCase(), String(value));
    },
    status(value) {
      this.statusCode = value;
      return this;
    },
    send(value) {
      this.body = Buffer.from(value);
    },
    end(value) {
      this.body = value ? Buffer.from(value) : Buffer.alloc(0);
    },
  };
}

async function quietly(callback) {
  const original = console.log;
  console.log = () => {};
  try {
    return await callback();
  } finally {
    console.log = original;
  }
}

test('generated gateway and activation own exactly jobs and the canary', async () => {
  const gateway = JSON.parse(
    await readFile(
      new URL('../deploy/vercel-production-gateway/vercel.json', import.meta.url),
      'utf8',
    ),
  );
  const activation = JSON.parse(
    await readFile(
      new URL(
        '../deploy/production-exact-route-v1/route-publication-activation.json',
        import.meta.url,
      ),
      'utf8',
    ),
  );
  const rollback = JSON.parse(
    await readFile(
      new URL(
        '../deploy/production-exact-route-v1/route-publication-rollback.json',
        import.meta.url,
      ),
      'utf8',
    ),
  );
  assert.deepEqual([...publishedPathSet], [CANARY]);
  assert.deepEqual(gateway.rewrites, [
    { source: '/jobs', destination: '/api/jobs' },
    { source: CANARY, destination: '/api/job' },
  ]);
  assert.deepEqual(
    activation.routes.map((item) => item.source),
    ['/jobs', CANARY],
  );
  assert.deepEqual(rollback.routes, []);
  assert.equal(activation.release_id, release.releaseId);
  assert.equal(rollback.release_id, release.releaseId);
  assert.equal(activation.cache_policy, 'no-store');
  assert.equal(
    gateway.rewrites.some((item) => /[:*]/.test(item.source)),
    false,
  );
  assert.deepEqual(
    (
      await readdir(
        new URL('../deploy/vercel-production-gateway/api/', import.meta.url),
      )
    ).sort(),
    ['job.mjs', 'jobs.mjs'],
  );
});

test('owned production routes send one release and no browser credentials', async () => {
  enableProduction();
  for (const [handler, url, target] of [
    [jobsHandler, '/jobs?q=AI%20Evaluation%20Specialist', '/jobs?q=AI+Evaluation+Specialist'],
    [detailHandler, CANARY, CANARY],
  ]) {
    let captured;
    globalThis.fetch = async (upstream, options) => {
      captured = { upstream: String(upstream), options };
      return originResponse();
    };
    const response = responseHarness();
    await quietly(() =>
      handler(
        {
          method: 'GET',
          url,
          headers: {
            accept: 'text/html',
            cookie: 'private=1',
            authorization: 'Bearer private',
            origin: 'https://evil.example',
          },
        },
        response,
      ),
    );
    assert.equal(captured.upstream, `https://catalog-origin.example.test${target}`);
    assert.equal(captured.options.headers['x-wahojobs-origin-auth'], TOKEN);
    assert.equal(captured.options.headers['x-wahojobs-release-id'], release.releaseId);
    assert.equal(captured.options.headers.cookie, undefined);
    assert.equal(captured.options.headers.authorization, undefined);
    assert.equal(captured.options.headers.origin, undefined);
    assert.equal(response.statusCode, 200);
    assert.equal(response.headers.get('x-wahojobs-production-owner'), 'new-origin');
    assert.equal(response.headers.get('x-wahojobs-release-id'), release.releaseId);
    assert.equal(response.headers.get('cache-control'), 'private, no-store, max-age=0');
    assert.equal(response.headers.get('cdn-cache-control'), 'no-store');
    assert.equal(response.headers.get('vercel-cdn-cache-control'), 'no-store');
  }
});

test('preservation matrix and Karl remain unowned with zero upstream calls', async () => {
  enableProduction();
  for (const path of [...PRESERVATION_PATHS, '/jobs/', `${CANARY}/`, CANARY.toUpperCase()]) {
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      throw new Error('unexpected_upstream_request');
    };
    const response = responseHarness();
    await quietly(() =>
      detailHandler({ method: 'GET', url: path, headers: {} }, response),
    );
    assert.equal(calls, 0, path);
    assert.equal(response.statusCode, 404, path);
    assert.equal(response.headers.get('x-wahojobs-production-owner'), 'unowned', path);
  }
});

test('raw normalization attacks fail closed before ownership', async () => {
  enableProduction();
  const leaf = CANARY.slice('/job/'.length);
  const attacks = [
    `/job/decoy/../${leaf}`,
    `/job/decoy/%2e%2e/${leaf}`,
    `/api/../job/${leaf}`,
    `/api/%2e%2e/job/${leaf}`,
    `/job/decoy/./${leaf}`,
    `/job/decoy/%2E/${leaf}`,
    `/job/decoy/.%2e/${leaf}`,
    `/job/decoy/%2e./${leaf}`,
    `/api/%2E%2e/job/${leaf}?from=attack`,
  ];
  for (const path of attacks) {
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
    };
    const response = responseHarness();
    await quietly(() =>
      detailHandler({ method: 'GET', url: path, headers: {} }, response),
    );
    assert.equal(calls, 0, path);
    assert.equal(response.statusCode, 404, path);
    assert.equal(response.headers.get('x-wahojobs-production-owner'), 'rejected', path);
  }
});

test('direct functions, disabled state, methods, and origin mismatch fail closed', async () => {
  enableProduction();
  for (const [handler, path] of [
    [jobsHandler, '/api/jobs'],
    [detailHandler, '/api/job'],
  ]) {
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
    };
    const response = responseHarness();
    await quietly(() => handler({ method: 'GET', url: path, headers: {} }, response));
    assert.equal(calls, 0);
    assert.equal(response.statusCode, 404);
    assert.equal(response.headers.get('x-wahojobs-production-owner'), 'rejected');
  }

  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
  };
  let response = responseHarness();
  await quietly(() =>
    detailHandler({ method: 'POST', url: CANARY, headers: {} }, response),
  );
  assert.equal(calls, 0);
  assert.equal(response.statusCode, 405);
  assert.equal(response.headers.get('allow'), 'GET, HEAD');

  process.env.WAHOJOBS_PRODUCTION_PUBLIC_ROUTES_ENABLED = '0';
  response = responseHarness();
  await quietly(() =>
    jobsHandler({ method: 'GET', url: '/jobs', headers: {} }, response),
  );
  assert.equal(response.statusCode, 503);
  assert.equal(response.headers.get('x-wahojobs-production-owner'), 'disabled');
  assert.equal(calls, 0);

  enableProduction();
  process.env.WAHOJOBS_NEW_ORIGIN_URL = 'https://www.wahojobs.com';
  response = responseHarness();
  await quietly(() =>
    jobsHandler({ method: 'GET', url: '/jobs', headers: {} }, response),
  );
  assert.equal(response.statusCode, 503);
  assert.equal(calls, 0);
});

test('missing release echo and origin failure are visible uncached 503s', async () => {
  enableProduction();
  for (const upstreamRelease of [null, '0'.repeat(64)]) {
    globalThis.fetch = async () =>
      new Response('wrong release', {
        status: 200,
        headers: upstreamRelease
          ? { 'x-wahojobs-release-id': upstreamRelease }
          : {},
      });
    const response = responseHarness();
    await quietly(() =>
      detailHandler({ method: 'GET', url: CANARY, headers: {} }, response),
    );
    assert.equal(response.statusCode, 503);
    assert.equal(response.headers.get('cache-control'), 'private, no-store, max-age=0');
  }
});

test('rollback document is one zero-route replacement preserving all legacy paths', async () => {
  const activation = JSON.parse(
    await readFile(
      new URL(
        '../deploy/production-exact-route-v1/route-publication-activation.json',
        import.meta.url,
      ),
      'utf8',
    ),
  );
  const rollback = JSON.parse(
    await readFile(
      new URL(
        '../deploy/production-exact-route-v1/route-publication-rollback.json',
        import.meta.url,
      ),
      'utf8',
    ),
  );
  assert.equal(activation.state, 'enabled');
  assert.equal(rollback.state, 'disabled');
  assert.equal(rollback.routes.length, 0);
  assert.deepEqual(rollback.preservation_paths, activation.preservation_paths);
  assert.deepEqual(rollback.preservation_paths, PRESERVATION_PATHS);
});
