import assert from 'node:assert/strict';
import test from 'node:test';

import handler from './jobs.mjs';

const TOKEN = 'T'.repeat(43);

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

test('preview-only exact jobs sends the origin secret and no browser credentials', async () => {
  process.env.VERCEL_ENV = 'preview';
  process.env.WAHOJOBS_PREVIEW_JOBS_ENABLED = '1';
  process.env.WAHOJOBS_NEW_ORIGIN_URL = 'https://origin.example.test';
  process.env.WAHOJOBS_ORIGIN_AUTH_TOKEN = TOKEN;
  let captured;
  globalThis.fetch = async (target, options) => {
    captured = { target: String(target), options };
    return new Response('<title>Jobs</title>', {
      status: 200,
      headers: {
        'content-type': 'text/html; charset=utf-8',
        'x-wahojobs-origin': 'public-catalog-preview',
      },
    });
  };
  const request = {
    method: 'GET',
    url: '/jobs?q=python',
    headers: { accept: 'text/html', cookie: 'private=1', authorization: 'secret' },
  };
  const response = responseHarness();
  await quietly(() => handler(request, response));
  assert.equal(captured.target, 'https://origin.example.test/jobs?q=python');
  assert.equal(captured.options.headers['x-wahojobs-origin-auth'], TOKEN);
  assert.equal(captured.options.headers.cookie, undefined);
  assert.equal(captured.options.headers.authorization, undefined);
  assert.equal(response.statusCode, 200);
  assert.equal(response.headers.get('x-wahojobs-preview-owner'), 'new-origin');
});

test('disabled or non-preview deployment falls through to current legacy jobs', async () => {
  for (const [environment, enabled] of [
    ['preview', '0'],
    ['production', '1'],
  ]) {
    process.env.VERCEL_ENV = environment;
    process.env.WAHOJOBS_PREVIEW_JOBS_ENABLED = enabled;
    let captured;
    globalThis.fetch = async (target) => {
      captured = String(target);
      return new Response('', { status: 307, headers: { location: '/404' } });
    };
    const response = responseHarness();
    await quietly(() =>
      handler({ method: 'GET', url: '/jobs', headers: {} }, response),
    );
    assert.equal(captured, 'https://www.wahojobs.com/jobs');
    assert.equal(response.statusCode, 307);
    assert.equal(response.headers.get('location'), '/404');
    assert.equal(
      response.headers.get('x-wahojobs-preview-owner'),
      'legacy-fallback',
    );
  }
});

test('origin failure is visible and does not silently claim a legacy response', async () => {
  process.env.VERCEL_ENV = 'preview';
  process.env.WAHOJOBS_PREVIEW_JOBS_ENABLED = '1';
  process.env.WAHOJOBS_NEW_ORIGIN_URL = 'https://origin.example.test';
  process.env.WAHOJOBS_ORIGIN_AUTH_TOKEN = TOKEN;
  globalThis.fetch = async () => {
    throw new Error('unavailable');
  };
  const response = responseHarness();
  await quietly(() =>
    handler({ method: 'GET', url: '/jobs', headers: {} }, response),
  );
  assert.equal(response.statusCode, 503);
  assert.equal(response.headers.get('x-wahojobs-preview-owner'), 'new-origin');
});
