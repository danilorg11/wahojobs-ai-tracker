# Focused transport review, 1 October 2026

The review hardened the prepared implementation before any publication:

- Reject repeated parameters within the URL or between URL and body instead of
  validating an overwritten value and then sending the original request.
- Require the exact current parent document path and its corresponding generic
  title. Another otherwise public pathname cannot substitute a false pageview.
- Reject credential-bearing URLs, nonstandard origins and URL fragments.
- Blocked beacon returns false, blocked fetch rejects, and blocked XHR aborts.
  No blocked request receives a synthetic HTTP 204 success response. A permitted
  request still calls the original native transport and gets its real outcome.
- Withdraw consent safely even if the empty frame is unavailable/CSP-blocked.
- Confirm in Edge that the parent page's fetch, beacon and XHR functions retain
  their original identity. Only the empty measurement document is instrumented.

The gate still permits at most one native pageview dispatch attempt. It does not
confirm account-side receipt. A network failure, unexpected body type/format or
blocked frame can undercount; the privacy boundary fails closed rather than
inventing a successful view or loosening property settings. No callback/result is
used to claim GA receipt. The page has no SPA hooks, and repeated CMP decisions
do not create additional collectors. Public parent `gtag` is the scoped CMP bridge;
candidate/product requests continue to use their original transports.

Six intercepted Edge scenarios passed after hardening, including the actual
regional Gatekeeper bootstrap and the prepared website gateway. Real GDPR UI and
real GA receipt remain publication acceptance steps. No GA property settings,
candidate feature flags, deployment configuration or database were changed.

This is a separate follow-up commit on the prepared GA change, so its small
shared-file diff can also be cherry-picked after the GA-only backend commit.
Both website alternatives remain unchanged; the parent must select either the
GA-only pair or the separately authorized candidate lineage.
