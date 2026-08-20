# Public Job Identity Foundation

## Status

This foundation is dormant. Migration `009_public_job_identity.sql` is not part
of the base schema and must not be applied to the workspace database as part of
this milestone. Current `/job/opportunity-<id>` staging routes remain unchanged.
No real public identities, legacy mappings, redirects, or backfill are created.

The code-only M009 operational closure adds exact M008-to-M009 schema
attestation and an atomic callable apply boundary. The boundary accepts only an
already-open exact M008 database, installs the empty authorities and
`009_public_job_identity` marker in one transaction, and allocates nothing. No
command-line or configured-database M009 apply surface exists.

## Authority boundaries

Permanent public routing is separated into three authorities:

| Authority | Portable between databases | Purpose |
| --- | --- | --- |
| `public_job_identities` | Yes | Immutable opaque identity and global serving, redirect, or gone disposition |
| `public_job_paths` | Yes | Exact immutable path ownership, primary role, and aliases |
| `public_job_bindings` | No | Versioned binding to the current database's canonical opportunity number |

Crawler jobs, canonical keys, representative selection, titles, and source
URLs are deliberately outside path ownership. Changing any of them cannot
rewrite an issued path.

## Issuance and import

An ID is `j` followed by 32 lowercase hexadecimal characters generated from 16
operating-system-random bytes. `allocate_public_job` requires an explicit
`PublicJobIdAllocator` capability so production configuration has a visible
single-authority boundary. Importing databases do not construct an allocator;
they import the portable identity/path registry and create only local bindings.

Portable exports have one canonical ASCII JSON serialization with a trailing
line feed and a SHA-256 over those exact bytes. Bindings are absent. The
disposable transfer verifier requires exact M009 source and target databases,
imports into empty authorities, establishes an explicit complete set of local
bindings, reconciles the target, and requires a byte-identical re-export.

New paths use a one-time ASCII slug made from company slug plus canonical title,
followed by the immutable ID. The readable slug is capped at 80 characters on a
word boundary and is never regenerated. Proven legacy paths can preserve exact
casing and trailing slashes. Percent-encoded paths are rejected fail-closed so
URL-decoding cannot create a second owner; any such historical path requires a
future reviewed policy before registration. All resolution is by complete
registered path; an ID suffix alone conveys no ownership.

## Mutations

- Issued identity values and all path rows cannot be updated or deleted.
- `INSERT OR REPLACE` cannot replace an issued identity, exact/normalized path,
  or binding, including when SQLite recursive triggers are disabled.
- Aliases redirect directly to their identity's primary path.
- Binding changes require the caller's expected version and advance it once.
- Binding updates fail before conflict resolution when another identity already
  owns the requested canonical opportunity, including `UPDATE OR REPLACE`.
- Direct binding deletion is forbidden. The schema retires a loser binding only
  as part of the identity's irreversible transition to `redirect`.
- Merge operations preserve loser path ownership, remove the loser local
  binding, and point every loser or prior redirect directly at the serving
  survivor.
- Redirect chains, cycles, self-targets, and targets that are not serving are
  rejected.
- Gone and restore transitions keep the same identity, path rows, and local
  binding. Temporary availability is not registry state and therefore cannot
  change a path.
- Reconciliation checks primary ownership, exact/normalized paths, direct
  redirect targets, local bindings, and canonical referential integrity.

## Structured data

The public identity is Wahojobs routing infrastructure and is never a
`JobPosting.identifier`. That property is emitted only for a supported source
contract whose rich-source posting ID is present and agrees exactly with the
repository's source copy. Otherwise it is omitted. `JobPosting.url` is a
separate decision and uses the page's primary public path at route cutover.

## Deferred work

The staging runtime accepts either exact M008 or exact, reconciled M009. Its
public-ID canary routing gate is an explicit exact-ID allowlist and is empty by
default in staging composition. An empty gate does not inspect M009 routing
tables and preserves every temporary `/job/opportunity-<id>` route. No path,
slug, canonical-ID, job-ID, wildcard, or request value can activate the gate.
The optional external `public_job_canary_ids` field accepts only exact public
IDs; omission or an empty array keeps the gate disabled, while a non-empty array
requires exact, reconciled M009 before startup.

Permanent route cutover requires the separately reviewed legacy evidence set.
Ambiguous mappings, historical reuse, case collisions, and unproven routes must
remain unresolved. The later cutover is also responsible for replacing staging
links, canonical tags, structured-data URLs, and sitemap entries. None of those
changes belong to this foundation.
