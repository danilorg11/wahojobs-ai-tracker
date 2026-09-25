# Focused beta cold-load correction

Parent release: `edf9c344d75d130ffadc9070591af1ef324fbfdf`.

## Observed conditions and causes

The hosted Browse request after the September 25 restart took 23,672 ms in
the server. A later return from detail, after its 300-second cache deadline,
took 26,561 ms. These were whole catalog reconstruction, not separate worker
or account caches. This service has one process and request threads. Warm
Profile/My Jobs → Browse clicks took 1.18–1.56 seconds (one sample each).

The first hosted Matches request timed out; its 78,663 ms interrupted response
overlapped a read-only diagnostic and is not a clean latency comparison. A
56,391 ms interrupted response also existed on the prior release. A later
successful retry took 20,547 ms server time. Browse and Matches both prepare
stored source evidence, but their measured expensive paths differ: Browse
validates enrichment and constructs catalog variants; Matches parses accepted
task evidence on process-cold entry, then scores every inventory row.

Ordinary Matches navigation discarded the retained run. Furthermore, any
unverified row without a source timestamp made the entire retained result's
validity window zero. Missing time stays unverified until an inventory commit;
it cannot become verified merely because time passes.

## Correction and boundaries

* Prepare Browse and the existing source/task projection caches before binding
  the hosted listener. No profile, account, model or employer request is used.
  Linux preparation has a 60-second alarm and failure closes the runtime.
* Retain at most 8,192 canonical preparation entries per integration. Reuse
  validated enrichment only when actual stored enrichment/override bytes match;
  reuse variant presentation only when source rows, enrichment and eligible IDs
  match. Refresh all temporal presentation fields. The response cache retains
  its 300-second maximum and source-expiry deadline, and database commits now
  invalidate it immediately. Saved/applied state is read separately.
* Check time after acquiring the catalog lock and after preparation. If a
  deadline was crossed, remove expired variants and reselect representatives;
  never extend source trust. Changed or malformed evidence fails closed.
* Plain Matches entry may use the owner's latest bounded retained run, subject
  to current authorization, profile/hidden-item inputs, inventory commit proof,
  all known freshness boundaries and current workflow rendering. Unknown source
  clocks remain unknown and no longer invalidate an otherwise sound proof.
* Memoize three pure text computations only inside one evaluation, with an
  8,192-entry limit per function, defensive result copies and exception-safe
  context cleanup. Compile the existing locale/title patterns once. Every row
  is still scored using the same matching rules and ordering.

Install `deploy/private-beta/60-catalog-readiness.conf` as the corresponding
`wahojobs-beta.service.d` drop-in on the existing host. It changes only
`TimeoutStartSec` to 75 seconds; do not replace existing host configuration.
The health probe waits at most 70 seconds. Native recovery keeps its total
120-second limit, reserves 80 seconds for start/readiness, and retains existing
failure/hold behavior. Rollback removes this new drop-in and selects the prior
release and policy; it never replaces the database. Timers are unchanged.

## Local evidence and remaining hosted checks

The unchanged September 6 snapshot contains 12,163 jobs, 4,041 canonicals and
23,711 captures; 9,059 rows enter matching. Tests use synthetic profiles/accounts,
empty workflow substitutes and read-only inventory or disposable copies. They
make no source/model requests and do not restart the live service.

Baseline first Matches took 25.07 seconds without profiling. Source preparation
alone took 14.47 seconds. After the initial correction, combined startup
preparation took 23.10 seconds; first Matches for three accounts took
13.88/10.21/9.71 seconds and nine repeats took 0.17–0.21 seconds. Browse requests
took 0.31–0.35 seconds, a 301-second idle refresh 3.02 seconds and an isolated
inventory invalidation 3.22 seconds. These are local handler measurements,
not hosted browser latency; later compiled-pattern optimization is additional.

An independent baseline/candidate run hashed all 9,059 scored rows and selected
matches. Both hashes were identical:
`4401b2ec32e40bf6ace68dfa2b23389d3cbbe5ea697be01d7a58d86771616fb2`
and `6ac5e8aaf6f95837742f063429e85ae60f24b76cd5224a9d80bbfded178a6903`.
This demonstrates matching-output parity for these inputs, not every consumer.

Before promotion: run the actual Linux alarm test, verify effective startup
configuration, available memory and the established backup/rollback preflight.
After promotion: verify the process release and bounded hosted first/repeated
Browse and Matches clicks, including Profile/My Jobs → Browse, without concurrent
profiling. Record residual first-account scoring delay plainly.

The September 26 06:00 UTC scheduled cycle remains a separate execution gate.
No forced cycle or timer change is authorized. Physicist extraction remains
unfinished. micro1 remains disabled. This change neither completes downstream
consumer validation nor changes Production authentication, invitations, public
domains or indexing. Initial real-candidate launch still needs the existing
separate Production/fresh-storage/reviewed-invitation approval after the gates.
