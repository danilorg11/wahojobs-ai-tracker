# Remaining Source Coverage V1: DataAnnotation and DataForce capture handoff

## Scope and lineage

This handoff follows the reviewed validation manifest in
`docs/remaining_source_coverage_v1_validation.md` and the separate capture
tooling commit `a4dc1150f7ac7138c27586a2deeaa25d88797967`. It preserves
`25ce4c7`, timing instrumentation `dd71ca2`, and the deployed alert correction
in their beta-derived ancestry. Only DataAnnotation and DataForce were queried.
No hosted service, authoritative database, scheduled source policy, request cap,
candidate account, notification or publication was changed.

The local Codex Windows workspace executed one bounded batch at
`2026-09-24T00:49:42.445401Z` through `00:49:47.543082Z`: **6 HTTP attempts,
5.11 seconds**, zero retries, zero paid calls. DataAnnotation used 3/12,
DataForce 3/20, aggregate 6/32 and 5.11/720 seconds. Capture occurred away
from the beta execution host, so local access proves nothing about hosted
access. The raw immutable journal and request/response accounting are in
`remaining-source-coverage-v1-evidence-20260924` beside this worktree. The
batch receipt is `batch-receipt.json`; the derived isolated replay receipt is
`isolated-replay-receipt.json`. Earlier observations were not changed.

## DataAnnotation: one role, stopped source

The first request to `/coding` returned HTTP 301 to the manifest's single
approved destination, `/job-board/software-engineer`. The destination returned
HTTP 200 at `00:49:42.806396Z`; its raw 43,416-byte response has SHA-256
`4b2f3b05cdda8daee1d8a3b5f998c11c6d7f52e1700b820765813d0c0a47bb34`.
The page declares the `software-engineer` role slug, an actual “Software
Engineer” heading, remote location, AI coding/evaluation work, and a
`/worker_signup` link on `app.dataannotation.tech` whose `utm_role` and role
content both match `software-engineer`. The candidate's canonical source
identity remains `dataannotation::coding`; the employer did not provide a
posting date. This is an **evergreen application**, not a confirmed active
project or a live-market count.

The third request, `/generalist`, returned HTTP 301 to
`/job-board/generalist`, which was outside the approved redirect allowlist.
The batch stopped without requesting that destination or the other nine fixed
pages. The original collection journal correctly remains `collection_failed`;
there is no complete DataAnnotation snapshot. The offline replay independently
derives only the retained coding record from the raw response and labels the
result `coding_only_partial`. A future controlled collection now stops at a
later unsupported redirect while retaining earlier individually attested
records as partial. It does not reclassify this historical failed collection.

`dataannotation_coding_evergreen_record_v1` is the narrow versioned record
contract. It checks exact source and destination identity, role slug and actual
heading, a role-bound signup link, body SHA-256, captured metadata, provenance,
evergreen classification, sample exclusion and partial outcome. The stored
attestation is replayed against persisted semantic fields and body. Other
DataAnnotation domains have no such authority and are excluded from lifecycle
input, including reactivation. The coding record is **one distinct opportunity
and one variant eligible in isolated replay**. It reached a versioned-accepted
capture and canonical projection without absence-based closure. This means
Wahojobs newly observed the role; it does not mean the employer posted it on
September 24.

**Conclusion:** ready for a narrowly scoped controlled hosted publication
review of the coding evergreen record only, subject to a fresh approved
observation from the beta host and separate activation approval. Full
DataAnnotation source activation remains blocked by the unapproved generalist
redirect and unobserved other domains. The evergreen model has no configured
72-hour live-feed expiry, but this local observation cannot assert availability
on the beta host at activation time. The minimum next request scope for the
single coding record is the known `/coding` →
`/job-board/software-engineer` pair, counted as at most two attempts; any
extension to `/job-board/generalist` requires a separately reviewed allowlist
decision. Neither request is authorized by this handoff.

## DataForce: bounded public index, no application authority

The all-project selector was selected on both populated pages. `/projects`
returned 25 project rows, and `?project_type=All&page=1` returned 16 different
rows. Its active pager was page 2 with no next link, supporting a terminal
**index-page sequence** for these 41 cards. The earlier adapter then requested
`?project_type=All&page=2`, which returned an explicit “no active projects for
this location” view but a pager still active on page 2. That is an out-of-range
empty page, not proof of zero inventory or a separate availability snapshot.
The three raw response SHA-256 values are, in request order:

- `6e098e00c40a292b22539e5bb2c685ed15fab6dc13101dfe5a66b745cd70c16a`
- `20594db1cd888a3d006bd620879215c9ee3ea56cf4a3906130113e4f1cd361e6`
- `2115c011db9daf4373901e2052c0cb37dbb43dfd15baa9ce1ffbf5feebc82acc`

The parser recovered 41 unique `/project/` or `/study/` identifiers; 30 cards
say Remote, 11 say On Site, and four titles explicitly concern minors. For
example, `/project/thyme-freelance-writer-spanish-us` describes remote writing
for machine-translation evaluation, while `/study/viola-voice-collection-telugu-hyderabad`
is onsite. Ronia photo cards mention machine learning but describe photo
collection. These distinctions matter for the remote AI-work filter, and country
restrictions remain variant-specific. The cards offer **Preview Job** links;
no linked detail or application destination was in this approved capture.
Therefore none of the 41 cards has observed application authority. Page 2's
generic empty text and any unrelated/challenge/error page are rejected by the
exact pager and view checks. No compensation or employer publication dates are
created from these cards.

The corrected parser validates the selected all-project filter, ordered active
pager and next link, and stops after the observed last page. The wrapper keeps
the source partial, counts all 41 as filtered source observations, and passes
zero candidates to lifecycle. This prevents an unqualified index sighting from
inserting a pending opportunity or reactivating a previously accepted inactive
variant. There is **no DataForce publication-eligible opportunity or variant**
from this batch. A terminal index alone is not a reviewed complete publication
or absence-closure contract.

**Conclusion:** blocked. The exact missing requirement is a separately
authorized, bounded read of the linked public role/detail pages showing the
record's application action and a source-specific versioned contract binding
that action to exact index identity, role, execution and provenance. The
established remote AI-work scope must then filter onsite and unrelated work
before lifecycle. DataForce is configured as a live feed; a qualifying
verification would have a 72-hour freshness limit, but this partial,
unqualified capture never starts that eligibility clock. Hosted access and
availability would need a new authorized check at activation.

## Isolated lifecycle and review

`scripts/replay_remaining_source_evidence.py` verifies retained response
checksums, requested URLs, the two DA 301 locations, DA role/body/application
correspondence, DF page order, terminal pager and page-2 mismatch. It builds a
temporary SQLite database, replays only the coding candidate through source
capture, reconciliation, canonical projection and deterministic enrichment,
and removes the database afterward. The same retained DataForce rows pass
through its wrapper into that isolated lifecycle, yielding zero new jobs and
catalog opportunities. The receipt records one promoted DataAnnotation
variant/canonical, zero DataForce eligible variants, and no removal
authorization for either source. Its input uses the **original capture time**; replay
does not renew availability or become a new employer observation.

Labelled contract tests cover generic/wrong-role/wrong-host page rejection,
partial stop, body/identity tampering, stored attestation replay, idempotent
processing at an injected test clock, no absence closure, and non-reactivation
of inactive unattested DA/DF variants. The affected source-capture and daily
policy regressions run alongside them. Earlier compensation/manual-override,
geography, candidate-history and corrected-alert safety coverage remains the
applicable existing suite; no policy or projection schema for those features
was changed here. Independent focused review identified and then confirmed
the fixes for unattested-row lifecycle leakage and replay evidence assertions;
the reviewer found no remaining blocking defect.

The nine active sources, 182-request cap, scheduler, alert history and timing
instrumentation remain unchanged. No all-15 coverage claim or new capture
batch is implied.
