# Daily inventory reliability repair — September 27

Status: local candidate based on accepted beta release
`68488ca54ac7901e9ca2198e00af08707ebf8462`. No deployment, production inventory
write, source re-enablement, scheduler change, or notification delivery occurred.
The owner's requested all-company daily operation is not yet resolved in production.

## Corrections

* Mindrift now retains versioned evidence for each explicitly published,
  non-internal Workable record. A sharp count drop can publish those exact
  positive observations while withholding all absence-based closure authority.
  Missing records retain their old verification clocks. The original captured
  snapshot remains unchanged; the resulting publication is correctly partial.
  Old unqualified observations cannot acquire this authority through replay.
* OneForma requires consistent `X-WP-Total`, `X-WP-TotalPages`, exact page sizes,
  positive integer post identities, and valid nonempty application-variant lists.
  Truncated pages or omitted variant fields cannot close existing postings.
* The cold backup validates the copied journal chains once instead of decoding
  the complete growing history twice. The copied bytes and full before/after
  source file identities and hashes must still agree. Empty pinned journals are
  supported. This reduces redundant work, but does not establish a host timing.
* The supervisor preserves the actual failed phase and safe worker diagnostic.
  Failed-cycle emails distinguish a shared pre-publication backup failure from
  individual source failures and explicitly state when nothing was published.

## Fresh public collection checks

All fifteen existing source collectors returned observations from this machine
on September 27 at approximately 23:06–23:16 UTC. These were read-only source
checks using the existing endpoint scopes and request ceilings; no observations
were published. Counts are exact posting variants emitted by the adapter, not
distinct cross-provider opportunities. The collection checks used 153 requests
in total, excluding the separate initial page/index probes.

| Source | Emitted records | HTTP requests | Collection authority |
| --- | ---: | ---: | --- |
| Alignerr | 5,624 | 47 | Complete current pagination; no overlap reproduced |
| Appen | 26 | 1 | Complete snapshot |
| DataAnnotation | 10 | 20 | Individually verified evergreen application pages |
| DataForce | 8 | 15 | Individual supported Thyme roles; broader index contains 54 cards |
| Handshake | 117 | 29 | Individual public CMS records; 43 other rows rejected |
| Meridial | 832 | 2 | Complete configured AI department snapshot |
| Mercor | 373 | 1 | Partial public listing surface |
| micro1 | 327 | 4 | Complete public expert portal from this machine |
| Mindrift | 92 | 12 | Complete capture; publication count-drop guard remains applicable |
| OneForma | 457 | 1 | Complete 32-post WordPress snapshot |
| Outlier | 8 | 9 | Individual current board/detail records |
| RWS | 25 | 1 | Complete configured TrainAI subset |
| Surge | 7 | 9 | Individual workforce roles |
| Turing | 248 | 1 | Complete current public jobs response |
| Welocalize | 390 | 1 | Complete configured AI Services subset |

The prior micro1 HTTP 403 did not recur locally. This does not prove access from
the beta host's IP; its installed disabled flag has not been changed.

Private collection journals and the Alignerr/Micro1 receipts are retained in the
September 27 task evidence directory. Each new journal records original request
and capture times and the tested code fingerprint. These diagnostic captures are
not a substitute for a fresh authorized server collection or a publication receipt.

## Remaining production work

Direct SSH to the pinned existing beta host timed out before authentication.
The automatic approval reviewer rejected starting the previous private credential
handoff helper without explicit owner approval for supplying the SSH key and
administrative access. That approval was requested; no key was read or retained.

Once access is available, inspect the September 27 `run.json`, phase timings,
`backup-failure.json`, and per-source publication receipts before selecting the
remaining repair. The common all-source publication failure is not yet diagnosed:
backup timeout is a hypothesis, not a verified incident cause. The previous day's
backup already took 51.465 seconds against its 60-second phase limit.

Deploy only against freshly verified host/release/configuration and storage
identity, preserving intervening owner changes. Use the existing coherent backup,
exclusive maintenance ownership, compatible code rollback, and readiness checks.
Measure the actual full-history backup and complete publication on the host. Then
perform fresh bounded collection, verify each publication receipt and protected
domains, validate micro1 from the host before enabling it, and verify the existing
06:00 UTC timer's next execution and the following natural cycle.

Six adapters intentionally provide individual/partial authority: DataAnnotation,
DataForce, Handshake, Mercor, Outlier, and Surge. They cannot confirm employer
closure from absence. Outlier/DataForce rotation also needs expansion if admitted
inventory grows. A complete daily all-company removal guarantee requires solving
those source-specific coverage contracts; changing an alert or extending an old
verification date cannot solve it.

The backup still processes all retained history inside 60 seconds and retains its
10,000-file ceiling. This candidate removes duplicate validation, but future
growth needs measured archival/preparation or incremental-backup work. No limit
was increased and no historical evidence was deleted.

## Validation

The integrated Linux suite completed 277 tests in 93.502 seconds: 276 passed,
one skipped because the beta service account does not exist on the disposable
local Linux host. That ownership test remains required on the beta host. The
initial two subprocess failures came from the relocated test interpreter losing
its shared-library path under the deliberately restricted worker environment;
an isolated interpreter launcher fixed the environment without changing the
application. The tested Python files match the current candidate byte-for-byte.
The tested-source archive SHA-256 is
`0274b632d04919a052a828e138a6dc40e4a6c0765a7c0fbd1365c46cf7ad8088`.

Tests include the 117→92 Mindrift case (50 old
positives, 42 new, 67 omissions), rollback on tampered records, unchanged omitted
clocks, valid closures, truncated OneForma responses, copied-backup tampering,
empty pinned journals, and shared backup-failure reporting.
