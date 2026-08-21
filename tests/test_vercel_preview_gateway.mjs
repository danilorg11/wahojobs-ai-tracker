import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';

import detailHandler from '../deploy/vercel-preview-gateway/api/job.mjs';
import jobsHandler from '../deploy/vercel-preview-gateway/api/jobs.mjs';
import {
  publishedPathSet,
  release,
} from '../deploy/vercel-preview-gateway/lib/release.mjs';

const TOKEN = 'T'.repeat(43);
const KARL = '/job/oneforma-karl-llm-1';
const NEW =
  '/job/handshake-ai-evaluation-specialist-j125e8ced56da8007c92ab964f58f9f0f';

function enablePreview() {
  process.env.VERCEL_ENV = 'preview';
  process.env.WAHOJOBS_PREVIEW_PUBLIC_ROUTES_ENABLED = '1';
  process.env.WAHOJOBS_NEW_ORIGIN_URL = 'https://origin.example.test';
  process.env.WAHOJOBS_ORIGIN_AUTH_TOKEN = TOKEN;
}

function originResponse(body = '<title>Jobs</title>', status = 200) {
  return new Response(body, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'x-wahojobs-origin': 'public-catalog-preview',
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

test('boundary owns only /jobs and the exact two attested manifest paths', async () => {
  const configuration = JSON.parse(
    await readFile(
      new URL('../deploy/vercel-preview-gateway/vercel.json', import.meta.url),
      'utf8',
    ),
  );
  assert.deepEqual([...publishedPathSet].sort(), [NEW, KARL].sort());
  assert.deepEqual(configuration.rewrites, [
    { source: '/jobs', destination: '/api/jobs' },
    { source: NEW, destination: '/api/job' },
    { source: KARL, destination: '/api/job' },
    { source: '/', destination: 'https://www.wahojobs.com/' },
    {
      source: '/:path*',
      destination: 'https://www.wahojobs.com/:path*',
    },
  ]);
  assert.deepEqual(
    configuration.headers.map((item) => item.source),
    ['/jobs', NEW, KARL],
  );
  assert.equal(
    configuration.rewrites.some(
      (item) =>
        item.destination === '/api/job' &&
        (item.source.includes(':') || item.source.includes('*')),
    ),
    false,
  );
  assert.deepEqual(
    (
      await readdir(
        new URL('../deploy/vercel-preview-gateway/api/', import.meta.url),
      )
    ).sort(),
    ['job.mjs', 'jobs.mjs'],
  );
});

test('all three exact Preview routes send one release identity and no browser credentials', async () => {
  enablePreview();
  const cases = [
    [jobsHandler, '/jobs?q=python%20engineer', '/jobs?q=python+engineer'],
    [detailHandler, `${NEW}?from=catalog`, `${NEW}?from=catalog`],
    [detailHandler, KARL, KARL],
  ];
  for (const [handler, url, originTarget] of cases) {
    let captured;
    globalThis.fetch = async (target, options) => {
      captured = { target: String(target), options };
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
            authorization: 'secret',
          },
        },
        response,
      ),
    );
    assert.equal(captured.target, `https://origin.example.test${originTarget}`);
    assert.equal(captured.options.headers['x-wahojobs-origin-auth'], TOKEN);
    assert.equal(
      captured.options.headers['x-wahojobs-release-id'],
      release.releaseId,
    );
    assert.equal(captured.options.headers.cookie, undefined);
    assert.equal(captured.options.headers.authorization, undefined);
    assert.equal(response.statusCode, 200);
    assert.equal(response.headers.get('x-wahojobs-preview-owner'), 'new-origin');
    assert.equal(
      response.headers.get('x-wahojobs-release-id'),
      release.releaseId,
    );
    assert.equal(
      response.headers.get('cache-control'),
      'private, no-store, max-age=0',
    );
    assert.equal(response.headers.get('cdn-cache-control'), 'no-store');
    assert.equal(response.headers.get('vercel-cdn-cache-control'), 'no-store');
  }
});

test('unowned, malformed, encoded, uppercase, and trailing detail paths stay legacy', async () => {
  enablePreview();
  const paths = [
    '/job/unknown-j00000000000000000000000000000000',
    NEW.toUpperCase(),
    `${NEW}/`,
    NEW.replace('/job/', '/job/%68'),
    '/job/not-a-public-id',
    '/job/legacy-family-member',
  ];
  for (const path of paths) {
    const targets = [];
    globalThis.fetch = async (target) => {
      targets.push(String(target));
      return new Response('legacy', { status: 404 });
    };
    const response = responseHarness();
    await quietly(() =>
      detailHandler({ method: 'GET', url: path, headers: {} }, response),
    );
    assert.deepEqual(targets, [`https://www.wahojobs.com${path}`]);
    assert.equal(response.headers.get('x-wahojobs-preview-owner'), 'legacy-fallback');
    assert.equal(response.body.toString(), 'legacy');
  }
});

test('raw literal and encoded dot segments fail closed before manifest ownership', async () => {
  enablePreview();
  const leaf = NEW.slice('/job/'.length);
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
    assert.notEqual(
      new URL(path, 'https://preview.invalid').pathname,
      path.split('?')[0],
      `control: URL parsing should normalize ${path}`,
    );
    let fetchCalls = 0;
    globalThis.fetch = async () => {
      fetchCalls += 1;
      throw new Error('unexpected_upstream_request');
    };
    const response = responseHarness();
    await quietly(() =>
      detailHandler({ method: 'GET', url: path, headers: {} }, response),
    );
    assert.equal(fetchCalls, 0, path);
    assert.equal(response.statusCode, 404, path);
    assert.equal(response.body.toString('utf8'), 'Not found\n', path);
    assert.equal(
      response.headers.get('x-wahojobs-preview-owner'),
      'rejected',
      path,
    );
  }
});

test('non-dot legacy lookalikes do not become owned or get over-rejected', async () => {
  enablePreview();
  const leaf = NEW.slice('/job/'.length);
  for (const path of [
    `/job/decoy/.../${leaf}`,
    `/job/decoy/%252e%252e/${leaf}`,
  ]) {
    const targets = [];
    globalThis.fetch = async (target) => {
      targets.push(String(target));
      return new Response('legacy', { status: 404 });
    };
    const response = responseHarness();
    await quietly(() =>
      detailHandler({ method: 'GET', url: path, headers: {} }, response),
    );
    assert.deepEqual(targets, [`https://www.wahojobs.com${path}`]);
    assert.equal(
      response.headers.get('x-wahojobs-preview-owner'),
      'legacy-fallback',
    );
  }
});

test('direct API function paths fail closed without any upstream traffic', async () => {
  enablePreview();
  for (const [handler, path] of [
    [jobsHandler, '/api/jobs'],
    [detailHandler, '/api/job'],
  ]) {
    let fetchCalls = 0;
    globalThis.fetch = async () => {
      fetchCalls += 1;
      throw new Error('unexpected_upstream_request');
    };
    const response = responseHarness();
    await quietly(() =>
      handler({ method: 'GET', url: path, headers: {} }, response),
    );
    assert.equal(fetchCalls, 0);
    assert.equal(response.statusCode, 404);
    assert.equal(response.body.toString('utf8'), 'Not found\n');
    assert.equal(response.headers.get('x-wahojobs-preview-owner'), 'rejected');
  }
});

test('one Preview flag rolls all exact routes back to legacy', async () => {
  for (const [environment, enabled] of [
    ['preview', '0'],
    ['production', '1'],
  ]) {
    process.env.VERCEL_ENV = environment;
    process.env.WAHOJOBS_PREVIEW_PUBLIC_ROUTES_ENABLED = enabled;
    for (const [handler, path] of [
      [jobsHandler, '/jobs'],
      [detailHandler, NEW],
      [detailHandler, KARL],
    ]) {
      let captured;
      globalThis.fetch = async (target) => {
        captured = String(target);
        return new Response('', {
          status: 307,
          headers: { location: '/404' },
        });
      };
      const response = responseHarness();
      await quietly(() =>
        handler({ method: 'GET', url: path, headers: {} }, response),
      );
      assert.equal(captured, `https://www.wahojobs.com${path}`);
      assert.equal(response.statusCode, 307);
      assert.equal(response.headers.get('location'), '/404');
      assert.equal(
        response.headers.get('x-wahojobs-preview-owner'),
        'legacy-fallback',
      );
    }
  }
});

test('missing or mismatched origin release identity fails closed', async () => {
  enablePreview();
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
      detailHandler({ method: 'GET', url: NEW, headers: {} }, response),
    );
    assert.equal(response.statusCode, 503);
    assert.equal(response.headers.get('x-wahojobs-preview-owner'), 'new-origin');
  }
});

test('origin failure is visible and exact owned methods are constrained', async () => {
  enablePreview();
  globalThis.fetch = async () => {
    throw new Error('unavailable');
  };
  let response = responseHarness();
  await quietly(() =>
    jobsHandler({ method: 'GET', url: '/jobs', headers: {} }, response),
  );
  assert.equal(response.statusCode, 503);
  assert.equal(response.headers.get('x-wahojobs-preview-owner'), 'new-origin');

  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return originResponse();
  };
  response = responseHarness();
  await quietly(() =>
    detailHandler({ method: 'POST', url: NEW, headers: {} }, response),
  );
  assert.equal(fetchCalls, 0);
  assert.equal(response.statusCode, 405);
  assert.equal(response.headers.get('allow'), 'GET, HEAD');
});
