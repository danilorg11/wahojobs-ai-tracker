import { randomUUID } from 'node:crypto';

const SAFE_RESPONSE_HEADERS = [
  'content-language',
  'content-security-policy',
  'content-type',
  'location',
  'referrer-policy',
  'x-content-type-options',
  'x-frame-options',
  'x-robots-tag',
  'x-wahojobs-origin',
  'x-wahojobs-origin-request-id',
];

function validatedOrigin(value) {
  const candidate = new URL(value);
  if (
    candidate.protocol !== 'https:' ||
    candidate.username ||
    candidate.password ||
    candidate.pathname !== '/' ||
    candidate.search ||
    candidate.hash
  ) {
    throw new Error('invalid_origin');
  }
  return candidate;
}

function copyResponseHeaders(upstream, response) {
  for (const name of SAFE_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) response.setHeader(name, value);
  }
  response.setHeader('cache-control', 'private, no-store, max-age=0');
  response.setHeader('cdn-cache-control', 'no-store');
  response.setHeader('vercel-cdn-cache-control', 'no-store');
  response.setHeader('x-wahojobs-preview-gateway', '1');
  response.setHeader('x-wahojobs-preview-owner', 'new-origin');
}

async function sendUpstream(upstream, request, response, requestId, owner) {
  response.statusCode = upstream.status;
  copyResponseHeaders(upstream, response);
  response.setHeader('x-wahojobs-preview-owner', owner);
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  const bytes = Buffer.from(await upstream.arrayBuffer());
  response.setHeader('content-length', String(bytes.length));
  response.end(bytes);
}

async function fetchLegacy(request, response, requestId) {
  const incoming = new URL(request.url, 'https://preview.invalid');
  const target = new URL('/jobs' + incoming.search, 'https://www.wahojobs.com');
  const upstream = await fetch(target, {
    method: request.method,
    redirect: 'manual',
    signal: AbortSignal.timeout(6000),
    headers: { accept: request.headers.accept || 'text/html' },
  });
  await sendUpstream(upstream, request, response, requestId, 'legacy-fallback');
  return upstream.status;
}

function rejectUnownedRoute(request, response) {
  response.setHeader('cache-control', 'private, no-store, max-age=0');
  response.setHeader('cdn-cache-control', 'no-store');
  response.setHeader('vercel-cdn-cache-control', 'no-store');
  response.setHeader('content-type', 'text/plain; charset=utf-8');
  response.setHeader('x-wahojobs-preview-gateway', '1');
  response.setHeader('x-wahojobs-preview-owner', 'rejected');
  response.status(404);
  if (request.method === 'HEAD') {
    response.end();
  } else {
    response.send('Not found\n');
  }
}

export default async function jobsPreviewGateway(request, response) {
  const started = Date.now();
  const requestId = randomUUID();
  let owner = 'new-origin';
  let route = 'jobs';
  let status = 503;
  try {
    let incoming;
    try {
      incoming = new URL(request.url, 'https://preview.invalid');
    } catch (_error) {
      incoming = null;
    }
    if (incoming === null || incoming.pathname !== '/jobs') {
      owner = 'rejected';
      route = 'rejected';
      status = 404;
      rejectUnownedRoute(request, response);
      return;
    }
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.setHeader('allow', 'GET, HEAD');
      response.setHeader('cache-control', 'no-store');
      response.status(405).send('Method not allowed\n');
      status = 405;
      return;
    }
    const enabled =
      process.env.VERCEL_ENV === 'preview' &&
      process.env.WAHOJOBS_PREVIEW_JOBS_ENABLED === '1';
    if (!enabled) {
      owner = 'legacy-fallback';
      status = await fetchLegacy(request, response, requestId);
      return;
    }
    const token = process.env.WAHOJOBS_ORIGIN_AUTH_TOKEN || '';
    if (!/^[A-Za-z0-9_-]{43}$/.test(token)) throw new Error('missing_secret');
    const origin = validatedOrigin(process.env.WAHOJOBS_NEW_ORIGIN_URL || '');
    const target = new URL('/jobs' + incoming.search, origin);
    const upstream = await fetch(target, {
      method: request.method,
      redirect: 'manual',
      signal: AbortSignal.timeout(6000),
      headers: {
        accept: request.headers.accept || 'text/html',
        'x-wahojobs-origin-auth': token,
        'x-wahojobs-origin-request-id': requestId,
      },
    });
    status = upstream.status;
    await sendUpstream(upstream, request, response, requestId, owner);
  } catch (_error) {
    response.setHeader('cache-control', 'no-store');
    response.setHeader('content-type', 'text/plain; charset=utf-8');
    response.setHeader('x-wahojobs-preview-gateway', '1');
    response.setHeader('x-wahojobs-preview-owner', owner);
    response.status(503).send('Preview origin unavailable\n');
    status = 503;
  } finally {
    console.log(
      JSON.stringify({
        event: 'wahojobs_preview_route',
        request_id: requestId,
        method: ['GET', 'HEAD'].includes(request.method) ? request.method : 'other',
        route,
        owner,
        status,
        duration_ms: Math.max(0, Math.min(Date.now() - started, 3600000)),
      }),
    );
  }
}
