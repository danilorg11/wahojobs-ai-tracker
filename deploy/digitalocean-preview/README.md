# Isolated DigitalOcean preview origin

This deployment is intentionally a single-instance, guest-only preview origin.
It does not reuse either legacy droplet and does not contain account, session,
profile, invitation, WorkOS, source-body, or M009 public-job identity rows.

The Python process binds only to `127.0.0.1`. Caddy is the public ingress and
allows only exact `GET`/`HEAD /jobs` plus token-protected operator health paths.
All other paths are rejected before Python. The origin token is stored outside
the release and must be independently configured in the Vercel Preview
environment. Caddy access logging is deliberately disabled; the application
emits bounded JSON events with request ID, method class, route class, status,
and duration only—never IP, query, cookie, authorization, user agent, or body.

The systemd service runs as `wahojobs-preview`, uses a persistent database at
`/var/lib/wahojobs-preview/catalog.sqlite3`, and has one supervised process.
The install script refuses to replace an existing database.
