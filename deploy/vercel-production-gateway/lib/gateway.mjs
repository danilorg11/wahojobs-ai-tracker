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
const MAX_REQUEST_TARGET_BYTES = 8_192;
const RAW_DOT_SEGMENT = /^(?:(?:\.)|(?:%2e)){1,2}$/i;

function validatedOrigin(value) {
  const candidate = new URL(value);
  if (
    candidate.protocol !== 'https:' ||
    candidate.username ||
    candidate.password ||
    candidate.pathname !== '/' ||
    candidate.search ||
    candidate.hash ||
    ['wahojobs.com', 'www.wahojobs.com'].includes(candidate.hostname.toLowerCase())
  ) {
    throw new Error('invalid_origin');
  }
  return candidate;
}

function parseRawRequestTarget(value) {
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    Buffer.byteLength(value, 'utf8') > MAX_REQUEST_TARGET_BYTES ||
    /[\0\r\n#]/.test(value)
  ) {
    return null;
  }
  let path;
  let search;
  if (value.startsWith('/')) {
    const queryIndex = value.indexOf('?');
    path = queryIndex === -1 ? value : value.slice(0, queryIndex);
    search = queryIndex === -1 ? '' : value.slice(queryIndex);
  } else {
    const absolute = /^(?:https?):\/\/[^/?#]+(\/[^?#]*)?(\?[^#]*)?$/i.exec(
      value,
    );
    if (absolute === null) return null;
    path = absolute[1] || '/';
    search = absolute[2] || '';
  }
  if (path.length === 0 || !path.startsWith('/') || path.startsWith('//')) {
    return null;
  }
  return { path, search };
}

function hasRawDotSegment(path) {
  return path.split('/').some((segment) => RAW_DOT_SEGMENT.test(segment));
}

function parseIncoming(rawTarget) {
  try {
    return new URL(
      `${rawTarget.path}${rawTarget.search}`,
      'https://production.invalid',
    );
  } catch (_error) {
    return null;
  }
}

function setNoStore(response) {
  response.setHeader('cache-control', 'private, no-store, max-age=0');
  response.setHeader('cdn-cache-control', 'no-store');
  response.setHeader('vercel-cdn-cache-control', 'no-store');
}

function copyResponseHeaders(upstream, response) {
  for (const name of SAFE_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) response.setHeader(name, value);
  }
  setNoStore(response);
  response.setHeader('x-wahojobs-production-gateway', '1');
}

async function sendUpstream(upstream, request, response, owner) {
  response.statusCode = upstream.status;
  copyResponseHeaders(upstream, response);
  response.setHeader('x-wahojobs-production-owner', owner);
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  const bytes = Buffer.from(await upstream.arrayBuffer());
  response.setHeader('content-length', String(bytes.length));
  response.end(bytes);
}

function reject(request, response, status, owner, message, allow = null) {
  setNoStore(response);
  response.setHeader('content-type', 'text/plain; charset=utf-8');
  response.setHeader('x-wahojobs-production-gateway', '1');
  response.setHeader('x-wahojobs-production-owner', owner);
  if (allow !== null) response.setHeader('allow', allow);
  response.status(status);
  if (request.method === 'HEAD') response.end();
  else response.send(message);
}

function requestIsDirectFunctionPath(path) {
  return path === '/api/job' || path === '/api/jobs';
}

function originRequestTarget(incoming, origin) {
  const search = incoming.search.replaceAll(/%20/gi, '+');
  return new URL(incoming.pathname + search, origin);
}

export function createProductionGatewayHandler({ routeClass, ownsPath }) {
  if (!['jobs', 'detail'].includes(routeClass) || typeof ownsPath !== 'function') {
    throw new Error('invalid_gateway_configuration');
  }
  return async function productionGateway(request, response) {
    const started = Date.now();
    const requestId = randomUUID();
    let owner = 'new-origin';
    let route = routeClass;
    let status = 503;
    try {
      const rawTarget = parseRawRequestTarget(request.url);
      if (rawTarget === null || hasRawDotSegment(rawTarget.path)) {
        owner = 'rejected';
        route = 'rejected';
        status = 404;
        reject(request, response, status, owner, 'Not found\n');
        return;
      }
      const incoming = parseIncoming(rawTarget);
      if (
        incoming === null ||
        incoming.pathname !== rawTarget.path ||
        requestIsDirectFunctionPath(rawTarget.path)
      ) {
        owner = 'rejected';
        route = 'rejected';
        status = 404;
        reject(request, response, status, owner, 'Not found\n');
        return;
      }
      if (!ownsPath(rawTarget.path)) {
        owner = 'unowned';
        route = 'unowned';
        status = 404;
        reject(request, response, status, owner, 'Not found\n');
        return;
      }
      if (!['GET', 'HEAD'].includes(request.method)) {
        owner = 'rejected';
        status = 405;
        reject(request, response, status, owner, 'Method not allowed\n', 'GET, HEAD');
        return;
      }
      const enabled =
        process.env.VERCEL_ENV === 'production' &&
        process.env.WAHOJOBS_PRODUCTION_PUBLIC_ROUTES_ENABLED === '1';
      if (!enabled) {
        owner = 'disabled';
        route = 'disabled';
        status = 503;
        reject(request, response, status, owner, 'Production routes disabled\n');
        return;
      }
      const token = process.env.WAHOJOBS_ORIGIN_AUTH_TOKEN || '';
      if (!/^[A-Za-z0-9_-]{43}$/.test(token)) throw new Error('missing_secret');
      const origin = validatedOrigin(process.env.WAHOJOBS_NEW_ORIGIN_URL || '');
      const upstream = await fetch(originRequestTarget(incoming, origin), {
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
      status = 503;
      reject(request, response, status, owner, 'Production origin unavailable\n');
    } finally {
      console.log(
        JSON.stringify({
          event: 'wahojobs_production_route',
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
