# September 25 inventory recovery correction

The 06:00 UTC cycle on cc27e846bcf9d7f8cbfd247f8b6089d0bd5c68a2
committed catalog work without completing every publication receipt. A later
interruption left a 94,904-byte, zero-header DELETE-mode journal. Startup's
strict sidecar guard then prevented service restoration. Journal existence
did not establish corruption or a hot transaction.

Under confirmed quiescence, the complete database context, matching journal,
operational state and configuration were preserved on the private host.
Evidence manifest SHA-256:
`f69bddf4561f1d512cd174d02f6c845fa6ac35653bc824667f75c94b17efa43c`.
An isolated native SQLite rehearsal passed full integrity, foreign keys,
application schema, catalog projection and protected-domain comparisons.
SQLite finalized the non-hot artifact through an unchanged user_version write
and rollback. The same validated authoritative recovery left main database
bytes unchanged. No journal was manually removed and no historical database
was substituted. Internal readiness returned at 13:20:45 UTC; the first
confirmed external HTTP 200 was 13:21:10.530786 UTC. This is a measured recovery
timestamp, not an exact measurement of the full outage duration.

Recovered durable source effects compared against the prepublication snapshot:

| Source | Durable catalog effect |
| --- | --- |
| Alignerr | 2,519 variant renewals; receipt interrupted after commit; safe partial classification retained |
| Appen | 26 renewals and six authorized inactive transitions; receipt confirmed |
| DataAnnotation | 10 renewals; partial; receipt confirmed |
| DataForce | Eight renewals and corresponding enrichment recalculations; partial; receipt confirmed |
| Handshake | 117 renewals; receipt interrupted after commit; partial |
| Meridial | Started crawl metadata only; no durable catalog transaction |
| Other sources | No durable inventory changes in this cycle |

No new canonical opportunities or variants were created. Recovered stored
totals were 2,144 canonical opportunities and 8,376 variants. The validated
eligible projection at recovery contained 2,076 opportunities and 8,284 variants.
Full-row protected-domain hashes, including drafts and manual overrides,
matched the trusted prepublication baseline. The original failed run remains
failed; separate reconciliation and application-recovery receipts record the
later recovery without inventing original completion times.

## Permanent safety changes

Cancellation first permits transaction rollback and connection closure, then
terminates and checks the entire worker process group. A timeout or surviving
SQLite recovery file stops additional publication. Before application restart,
a root supervisor holding the actual maintenance gate invokes a bounded child;
the child verifies the supervisor and stopped service, checks open descriptors,
and drops to the established database owner. Managed native recovery preserves
the complete supported file pair, rehearses it, validates schema/integrity and
protected data, and requires the authoritative result to match. Unsupported
linked or companion transactions fail closed. Ordinary startup guards remain
strict. Clean-storage recovery does not require a nonexistent completed backup.

Recovery receives a fresh bounded allowance independent of publication's
exhausted deadline. Terminal failure receipts are immutable; a hash-bound
application-recovery receipt makes later recovery idempotent. Failure writes a
persistent publication hold and disables the inventory timer. Health checks
require actual application/database readiness and generate a critical
application-unavailable transition through the existing delivery adapter.

## Validation and operating constraints

Real subprocess tests cover hot journals, cold artifacts, cancellation during
a large write, surviving children, earlier commits followed by rollback,
strict startup rejection, permissions, live draft connections, production
ownership, repeat recovery, protected data, the supervisor gate, timer holds,
and true readiness-driven recovery alerts. A retained 2,519-job Alignerr
observation was validated against its original producing release and replayed
only on an isolated copy: cancellation inside the transaction rolled it back
and retained protected data, with zero employer requests. Read-only inspection
of the recovered copy took approximately 45.6 seconds for Alignerr, 4.3 for
Handshake and 14.6 for Meridial, supporting expensive receipt inspection as a
contributing factor. Exact historical child liveness at 06:04 is unproven.

After a hold, an operator must establish authoritative integrity, application
readiness, corrected deployment and passing recovery tests before removing the
hold and restoring the existing timer. Preserve its original 06:00 UTC cadence,
source scopes and budgets; verify its last-trigger stamp before enabling so
there is no surprise catch-up run. Never infer success from a wrapper exit or
rewrite the original failed cycle as successful. Authenticated visual checks
require an existing authorized session; no disposable owner activity is created.
