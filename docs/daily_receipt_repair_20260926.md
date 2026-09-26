# September 26 source execution and receipt repair

This repair is based on accepted release `e81f6c8449c1d653f417827f7bd07d9377e9da72`.
It changes daily publication preparation, durable receipt recovery and operational
classification. Source contracts, budgets, timers, matching/admission, candidate
state, authentication and public launch configuration are unchanged.

## Authoritative execution

The native timer triggered run **20260926T060000Z** at
`2026-09-26T06:00:00.224601+00:00`, invocation
`16cb8f075b2c46c6a28f0f671d0cb933`. Execution started at 06:00:03.506143;
collection finished at 06:01:44.059507 and normal service was restored by
06:06:17.834457. The original outcome remains `partial_or_failed`.
The completed worker retained its protected-domain equality proof.

All fourteen collection plans name release
`2b5aeae3521da5f4a1e7d221f2cdfc08417e023a`, with contract fingerprint
`5a10a84143d6b8da5712fef6cc199bc65c329b6693b60f40b0b6bd4e4a30f6f8`.
The retained coverage plan establishes all fourteen enabled sources were due;
micro1 was disabled. The September 26 policy file byte hash was not persisted
with that run, so the current policy hash is not substituted for it. The current
pre-repair policy hash is
`93fd918b240db2a89c1f8d85da84fdc7ce910b04b7feded0f4d713b2e21060e3`.
The approved aggregate budget remains 286 HTTP transactions / 2,580 seconds,
with a separate existing 240-second publication window and bounded restoration.

## Reconciled provider table

All times below are UTC on September 26. **Every row was scheduled and actually
attempted**; each has a sealed `collected_unpublished` capture. “Complete” refers
to its source snapshot; “individual” means only the observed records can qualify,
with no absence-based closure authority. A retained capture alone is not a
publication or a new verification date. No extra collection was used here.

| Provider | First actual request / requests | Capture | Qualification | Receipt/accounting | Publication participation | Freshness effect of this run | First decisive failure / recovery status |
|---|---|---|---|---|---|---|---|
| Alignerr | 06:00:08.938478 / 9 | 1,080 raw, 1,068 unique; incomplete, 12 duplicate IDs at offset 960 | Not established live; retained incomplete snapshot cannot qualify full inventory | Capture and saved publication plan exist; no publication journal or matching transaction | Failed before lifecycle entry | None; 5,624 records retain Sep 25 15:28:04 verification | Incomplete pagination; separate preparation failure has only a generic RuntimeError. New preparation/receipt fix; natural cycle pending |
| Appen | 06:00:16.737725 / 1 | Complete, 26 | Accepted | Original complete receipt | Committed crawl 37 | 26 verified at 06:00:17 | No failure |
| DataAnnotation | 06:00:19.505294 / 20 | Ten approved 301→200 pairs; 10 attested individual roles | Not established in failed publication | Bound capture and failed publication; crawl 38 deadline error | Transaction failed, no catalog effects | None; 10 retain Sep 25 06:00:23 | Publication deadline, not HTTP 301. Same approved canonical redirects retained |
| DataForce | 06:00:24.630124 / 15 | 8 attested individual roles; 46 index items filtered; 2 exploratory inspection failures | Accepted | Original terminal receipt failed after commit; exact crawl/capture/event proof recovers accounting | Committed crawl 39 | 8 verified at 06:00:31 | Post-commit reporting timeout; current reporting corrected without replay |
| Handshake | 06:00:33.643060 / 29 | 117 attested individual records | Not established live | Capture and saved plan; no publication journal or matching transaction | Failed before lifecycle entry | None; 117 retain Sep 25 06:00:40, despite older operational state | Preparation stage, exact exception lost. New preparation/receipt fix; natural cycle pending |
| Meridial | 06:00:42.520047 / 2 | Complete, 832 | Not established live | Capture and saved plan; no publication journal or matching transaction | Failed before lifecycle entry | None; 832 retain Sep 24 06:00:16 | Preparation stage, exact exception lost. Natural cycle pending |
| Mercor | 06:00:46.318866 / 1 | 357 individually attested records; partial surface | Not established live | Capture and saved plan; no publication journal or matching transaction | Failed before lifecycle entry | None; all original cohorts retained | Preparation stage, exact exception lost. Partial surface remains intentional; natural cycle pending |
| Mindrift | 06:00:49.251721 / 12 | Collector returned 92, claiming complete | Rejected by existing count-drop guard | Bound capture and failed crawl 40 | No lifecycle publication | None; 117 retain Sep 24 06:00:26 | 21.4% count drop, 67 active records absent. Guard preserved; no forced success |
| OneForma | 06:00:55.197540 / 1 | Complete, 457 | Accepted | Exact crawl/capture/event proof recovers post-commit failure | Committed crawl 41 | 457 verified at 06:01:05 | Post-commit reporting timeout; current reporting corrected without replay |
| Outlier | 06:01:07.492348 / 9 | 8 attested individual records | Accepted | Original complete receipt | Committed crawl 42 | 8 verified at 06:01:10 | No publication failure |
| RWS | 06:01:12.739868 / 1 | Complete, 25; 20 filtered | Accepted | Original complete receipt | Committed crawl 43; 8 closures | 25 verified at 06:01:13 | No failure; complete-source absence authority verified |
| Surge | 06:01:15.655695 / 9 | 7 attested workforce roles; fellowship filtered | Not established live | Capture and saved plan; no publication journal or matching transaction | Failed before lifecycle entry | None; 7 retain Sep 24 17:35:22 | Preparation failure, separate from the old source blocker. Approved exact-record contract was already deployed; natural cycle pending |
| Turing | 06:01:19.952178 / 1 | Complete, 253 | Not established in failed publication | Bound capture and failed crawl 44 deadline error | Transaction failed, no catalog effects | None; 247 retain Sep 24 06:00:44 | Publication deadline; natural cycle pending |
| Welocalize | 06:01:23.834384 / 1 | Complete, 390; 146 filtered | Accepted | Exact crawl/capture/event proof recovers post-commit failure | Committed crawl 45; 13 new variants, 44 closures | 390 verified at 06:01:42 | Post-commit reporting timeout; current reporting corrected without replay |

Thus the old proposed split is not authoritative. Six sources committed
qualifying observations: Appen, DataForce, OneForma, Outlier, RWS and Welocalize.
Seven sources have publication failures (the five pre-lifecycle failures plus
DataAnnotation and Turing); Mindrift has an observation-qualification rejection.
No enabled source's attempt status remains unknown after this investigation.
The exact exception for the five pre-journal failures is unavailable: worker
output was discarded, and the parent retained only `RuntimeError`.

DataAnnotation's destinations are the existing fixed HTTPS canonical paths on
`www.dataannotation.tech`: software-engineer, generalist, legal-expert,
mathematician, medical-expert, physicist, finance-expert, accountant, chemist and
biologist under `/job-board/`. Each redirect consumed an approved transaction.
The same contract succeeded September 25. No redirect or request policy changed.

Surge's old slug-title/generic-200 blocker preceded accepted contract commit
`a9e296e`. A qualifying seven-record partial run completed September 24 at
17:35:22, and September 26's coverage plan and retained captures use that approved
contract. This repair bypasses no source gate. micro1 stays separately disabled.

## Demonstrated defects and correction

Daily catalog publication repeatedly calculated derived matching/enrichment
freshness during plan creation, plan validation and post-commit inspection.
Catalog-only daily plans now omit that unrelated derived inspection, retaining
the same source trust checks, accepted evidence, fingerprints and lifecycle.
This is confined to the existing daily catalog path.

Previously a database commit preceded a fallible, expensive reporting inspection.
DataForce, OneForma and Welocalize committed and then lost their summaries to a
timeout. The transaction now prepares its full receipt durably before commit;
recovery verifies the exact terminal crawl row, database and collection linkage
before reporting success. An uncommitted prepared receipt cannot qualify.
Atomic exclusive journal entries prevent a partial final file hiding the valid
prefix after termination. Bounded phase diagnostics preserve the failure stage.

Source reconstruction now reads the collection independently of the publication
journal. The old order discarded five valid attempt/capture histories when the
publication journal was absent. A shared classifier distinguishes collection,
publication, qualification, unavailable accounting and blocked/disabled states.
It no longer presents eleven materially different conditions as crawler failures,
nor uses an older failed attempt's timestamp for a current unknown attempt.

Historical reporting correction requires exact original transaction evidence.
It appends an immutable prepared correction and hash-addressed audit document,
then publishes one atomic pointer. Original run, source receipts, journal entries
and notification history remain unchanged. Newer native runs supersede the
overlay. Reapplying is idempotent. Health appends one explicit accounting
correction event, never a false new verification or recovery. No inventory is
replayed, no dates are renewed, and this operation sends no mail itself.

## Actual inventory and freshness consequences

The September 26 effect was **2 new canonical opportunities, 13 new variants,
24 materially changed variants and 52 confirmed closures**. The old 0/0/8/8
headline omitted committed source transactions. The 52 closures are exactly
8 RWS plus 44 Welocalize, each from a complete qualifying snapshot. Exact event
IDs/counts agree with their terminal crawl rows. Partial, rejected, failed and
unaccounted sources caused no live closures. The correction changes reporting,
not these already-committed catalog effects.

The **59 expired Mercor records are disjoint**: 38 verified Sep 18 13:07:32,
10 Sep 21 23:00:39 and 11 Sep 23 06:00:27. They remain availability-unconfirmed,
not employer-closed. An additional 10 Mercor records expire Sep 27 06:00:19;
370 retain their Sep 28 15:28:06 deadline. No cohort dates were extended.

Actual upcoming deadlines differ from stale alert summaries:

| Source/cohort | Exact active records | Applicable deadline UTC |
|---|---:|---|
| Meridial | 832 | Sep 27 06:00:16 |
| Mindrift | 117 | Sep 27 06:00:26 |
| Turing | 247 | Sep 27 06:00:44 |
| Surge | 7 | Sep 27 17:35:22 |
| Handshake | 117 | Sep 28 06:00:40 |
| OneForma | 457 | Sep 29 06:01:05 |
| Welocalize | 390 | Sep 29 06:01:42 |

The 48-hour escalation remains distinct from 72-hour expiry. Counts above are
source cohorts, not a claim about distinct canonical opportunities across sources.

## Validation and launch gate

The affected Linux suite passes 132 tests, including source/transport failure,
blocked and unknown states, partial publication, closure and expiry safety,
historical alert replay, atomic correction/retry and real subprocess termination
before and after commit. Independent review closed its atomicity finding and
found no remaining blocker in the focused changes. Existing unchanged native
unit/timer/restore evidence is reused; no units or scheduling rules are changed.

Retained full-size replay uses 8,389 source records and all fourteen September 26
captures, with no account data or networking. The candidate completes fourteen
separate Linux workers under the unchanged native weighted deadlines in 60.414s;
the accepted baseline takes 88.004s in the equivalent isolated run (one run each).
Both runs retain the Mindrift rejection and Alignerr incompleteness; the local
baseline does not reproduce the host's deadline failures. Source authority state
is identical when excluding only derived enrichment inspection. The actual
51.465s backup cost is deducted from the 240s cap. This does not exercise
live systemd stop/backup/restoration or establish host-independent timing. Alignerr
remains incomplete and Mindrift's existing rejection is preserved.

The next **natural September 27 06:00 UTC run remains pending**. A pass requires
the actual timer-bound release/run, trustworthy accounting for every enabled
source, qualified-only publication, no closures or freshness renewal from failed
evidence, correct expiry, protected-data equality, bounded recovery/readiness and
truthful deduplicated operations reporting. Individual external failures may
remain explicit; fourteen permanent successes are not the launch rule.

Owner acceptance of the resulting named source coverage and expiring inventory
remains separate from proving this operating contract. The roughly eleven-second
first Matches calculation, Browse expiry delay and unproven multi-user memory
margin remain unaccepted limitations. Physicist extraction is unfinished. This
limited operational validation does not establish that every downstream consumer
is unaffected. WorkOS Production, invitations, public domains/indexing and real
beta storage remain outside this repair and require the existing launch action.

Evidence retained with this operational task: original run/capture/plan manifests,
source event IDs and prior receipts, the read-only reconstruction, isolated replay
results, controlled deployment/backup receipt and append-only correction audit.
