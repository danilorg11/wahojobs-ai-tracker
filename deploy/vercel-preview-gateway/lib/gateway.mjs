import { randomUUID } from 'node:crypto';

import { release } from './release.mjs';

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
  'x-wahojobs-release-id',
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

function parseIncoming(request) {
  try {
    return new URL(request.url, 'https://preview.invalid');
  } catch (_error) {
    return null;
  }
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
}

async function sendUpstream(upstream, request, response, owner) {
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

async function fetchLegacy(request, response, incoming) {
  const target = new URL(incoming.pathname + incoming.search, 'https://www.wahojobs.com');
  const upstream = await fetch(target, {
    method: request.method,
    redirect: 'manual',
    signal: AbortSignal.timeout(6000),
    headers: { accept: request.headers.accept || 'text/html' },
  });
  await sendUpstream(upstream, request, response, 'legacy-fallback');
  return upstream.status;
}

function rejectDirectFunction(request, response) {
  response.setHeader('cache-control', 'private, no-store, max-age=0');
  response.setHeader('cdn-cache-control', 'no-store');
  response.setHeader('vercel-cdn-cache-control', 'no-store');
  response.setHeader('content-type', 'text/plain; charset=utf-8');
  response.setHeader('x-wahojobs-preview-gateway', '1');
  response.setHeader('x-wahojobs-preview-owner', 'rejected');
  response.status(404);
  if (request.method === 'HEAD') response.end();
  else response.send('Not found\n');
}

function rejectMethod(response) {
  response.setHeader('allow', 'GET, HEAD');
  response.setHeader('cache-control', 'no-store');
  response.setHeader('x-wahojobs-preview-gateway', '1');
  response.setHeader('x-wahojobs-preview-owner', 'rejected');
  response.status(405).send('Method not allowed\n');
}

function requestIsDirectFunctionPath(path) {
  return path === '/api/job' || path === '/api/jobs';
}

function originRequestTarget(incoming, origin) {
  // Vercel presents query spaces to functions as %20. The catalog's existing
  // canonical query encoder uses '+', so restore that equivalent spelling at
  // this internal hop to avoid a self-redirect through the platform rewrite.
  const search = incoming.search.replaceAll(/%20/gi, '+');
  return new URL(incoming.pathname + search, origin);
}

export function createPreviewGatewayHandler({ routeClass, ownsPath }) {
  if (!['jobs', 'detail'].includes(routeClass) || typeof ownsPath !== 'function') {
    throw new Error('invalid_gateway_configuration');
  }
  return async function previewGateway(request, response) {
    const started = Date.now();
    const requestId = randomUUID();
    let owner = 'new-origin';
    let route = routeClass;
    let status = 503;
    try {
      const incoming = parseIncoming(request);
      if (incoming === null || requestIsDirectFunctionPath(incoming.pathname)) {
        owner = 'rejected';
        route = 'rejected';
        status = 404;
        rejectDirectFunction(request, response);
        return;
      }
      if (!ownsPath(incoming.pathname)) {
        owner = 'legacy-fallback';
        route = 'legacy';
        status = await fetchLegacy(request, response, incoming);
        return;
      }
      if (!['GET', 'HEAD'].includes(request.method)) {
        owner = 'rejected';
        status = 405;
        rejectMethod(response);
        return;
      }
      const enabled =
        process.env.VERCEL_ENV === 'preview' &&
        process.env.WAHOJOBS_PREVIEW_PUBLIC_ROUTES_ENABLED === '1';
      if (!enabled) {
        owner = 'legacy-fallback';
        status = await fetchLegacy(request, response, incoming);
        return;
      }
      const token = process.env.WAHOJOBS_ORIGIN_AUTH_TOKEN || '';
      if (!/^[A-Za-z0-9_-]{43}$/.test(token)) throw new Error('missing_secret');
      const origin = validatedOrigin(process.env.WAHOJOBS_NEW_ORIGIN_URL || '');
      const target = originRequestTarget(incoming, origin);
      const upstream = await fetch(target, {
        method: request.method,
        redirect: 'manual',
        signal: AbortSignal.timeout(6000),
        headers: {
          accept: request.headers.accept || 'text/html',
          'x-wahojobs-origin-auth': token,
          'x-wahojobs-origin-request-id': requestId,
          'x-wahojobs-release-id': release.releaseId,
        },
      });
      if (upstream.headers.get('x-wahojobs-release-id') !== release.releaseId) {
        throw new Error('origin_release_mismatch');
      }
      status = upstream.status;
      await sendUpstream(upstream, request, response, owner);
    } catch (_error) {
      response.setHeader('cache-control', 'no-store');
      response.setHeader('cdn-cache-control', 'no-store');
      response.setHeader('vercel-cdn-cache-control', 'no-store');
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
          duration_ms: Math.max(0, Math.min(Date.now() - started, 3_600_000)),
        }),
      );
    }
  };
}
