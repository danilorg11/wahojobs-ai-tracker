# Outlier and micro1 bounded source round — 2026-09-24

The beta-host capture ledger and raw responses are retained outside Git at
`/var/lib/wahojobs-beta/outlier-micro1-round-v1` and in the task's local
`remaining-source-delivery-20260924/outlier-micro1-round-v1` evidence copy.
Attempts 1–12 used 11 Outlier and one micro1 HTTP requests, with no retry:
2.807 seconds of measured HTTP time, 7 minutes 59 seconds from the first
reserved attempt to the final completed attempt. These are cumulative task
counts, below the approved 40/20/60 request and 900-second limits.

## Outlier record authority

The public board POST returned eight unique indexed rows (`response-001.raw`,
SHA-256 `a241f7cd239c83a65239428e62ca9bd12beeb83a274eb2708ed1ececbf23777b`).
One public role page and its linked client chunk were captured in attempts 2–3.
That exact client chunk (SHA-256
`4502c226c3025b82a7e04c99035da0a384f44a588a825482e16a3e303001e1f2`)
calls `GET /internal/experts/job-board/jobs/<id>`, uses `signupFlowId` to
enable the role's Apply action, and displays nonempty `allowedCountries`
instead of the fallback `location.name`. Attempts 4 and 6–12 fetched all eight
exact indexed IDs. Every detail returned HTTP 200 and matched its index ID,
title, content, location field and allowed-country list, with a nonempty
role-bound `signupFlowId`. The index-issued `absolute_url` is kept as the
candidate link. Only one final `/opportunities/<id>` page was fetched in this
batch; a constructed final URL is not claimed to have been observed for all
eight. Earlier retained redirects remain earlier evidence.

The versioned `outlier_index_detail_record_v1` contract rechecks the complete
retained index payload, exact index row, detail record, signup identity,
client-contract hash, remote AI-work scope and country restrictions during
promotion and stored replay. It gives **individual** public-inventory authority
for six roles and evergreen application authority for two General Inbound
records. It does not assert active project availability, provider-wide complete
discovery, or absence-based closure. `allowedCountries` is used for displayed
geography because the captured official client does so; both the original
`location.name` and country list remain in provenance. The client hash records
the observed contract, not a fresh check of a future frontend release. No
employer posting date is derived from `createdAt` or first Wahojobs discovery.

The isolated real-response replay verifies eight source variants and eight
canonical catalog opportunities, two of which are evergreen. They are **not
published on the beta**. The thirteen enabled source time limits already sum
to 2,280 seconds; the required 240-second overhead reaches the authorized
2,520-second daily ceiling. Even a narrow positive Outlier time allocation
would exceed that ceiling unless an existing allocation is changed. Existing
source budgets and the ceiling are preserved, so Outlier remains disabled.
Its daily qualification is deliberately limited to these eight IDs. Other
future board cards are neither fetched nor published by this contract. The
transport permits 1 exact index POST plus at most eight exact-ID detail GETs,
with a 9-request / 60-second per-source safety maximum. The HTTP ceiling
matches the frozen scope; it does not grant discovery of new roles.

## micro1 access result

The public opportunities page (`response-005.raw`) is accessible from beta and
its own client uses the previously documented unauthenticated portal POST at
`https://prod-api.micro1.ai/api/v1/job/portal`. The retained beta-host POST
returned generic HTTP 403. The new page did not establish a distinct public
inventory endpoint, documented credential, or supported correction. The 403
alone does not identify its cause. No unchanged POST was repeated, and no
candidate account or bypass path was used. micro1 remains disabled. The
provider or owner must establish whether this unattended beta-host portal
request is authorized and, if applicable, supply the documented access
contract or public alternative before another probe or activation.

## Operations and gate

The previously deployed diagnostic archival/restart correction remains in
release `8b47803877ec3c80838c1234402fa293ad718c44`: lossless closed-run
archives, reference links, hashes, strict online preflight and nonfatal app
startup housekeeping. This round makes no scheduler, alert, compensation,
candidate UI or publication-maintenance change. The actual thirteen-source
policy remains 277 HTTP attempts and 2,520 seconds plus 120 recovery, with a
240-second publication-maintenance bound. No new source is activated.
