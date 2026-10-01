# Public catalog GA4 integration

The `/jobs` GET documents do not inherit Next's root layout. The public catalog
and detail shell now include the existing `G-QFMW1WX907` destination and existing
Gatekeeper CMP. The website gateway also needs the paired route/CSP change:
it constructs its own CSP instead of forwarding the backend's policy.

## Consent and collection boundary

Google remains unloaded until Gatekeeper finishes its regional request. In TCF
regions, a loaded CMP plus `tcloaded`/`useractioncomplete`, purpose-1 consent, and
Google vendor 755 not explicitly denied permit Analytics. Missing purpose/vendor
structures or region/status keep it off. This follows the observed public CMP
purpose-1 / vendor-755 check. Its non-TCF regional policy permits collection only
after `ezCMPQueue.gotResponse`, `ezTcfConsent.loaded` and `store_info` are true.
CMP failure does not grant consent. A listener stays installed for withdrawal;
withdrawal disables/removes the collector. Advertising remains denied here.

This integration deliberately measures one document view. It passes only a
whitelisted public pathname, generic title and empty referrer. Queries, fragments,
variant/return targets, search strings, authentication/callback URLs, account or
profile data, user IDs and candidate actions are not measurement inputs. Preview,
beta, private and arbitrary routes do not initialize this production destination.
Duplicate bootstrap execution and consent callbacks do not add another view.

The current public Google tag enables Enhanced Measurement. An intercepted real
tag test demonstrated that a `page_location` override alone still dispatches a
`view_search_results` containing the original `q`. `send_page_view:false` alone
also does not control history-based automatic views. No property setting was
changed to solve this. Instead, Google runs in the empty, first-party
`/jobs/_analytics` document. That route rejects queries, stays noindex, owns no
forms, links, user content or candidate script, and only accepts initialization
from its same-origin consenting parent. Public pages keep `frame-ancestors 'none'`;
only this empty document allows same-origin framing.

A transport guard in that document allows one sanitized `page_view` to the
existing GA destination and rejects automatic events, user/custom properties,
unsafe locations/referrers and unexpected payload types. It covers beacon, fetch
and XHR without changing the product's transport. First-party GA cookie scope is
retained after consent. It is collector isolation, not a sandbox security boundary
against trusted third-party code. New Google transport/payload formats fail closed
and need renewed browser verification. Engagement/search/action metrics are
outside this view-only integration.

## Verification

Focused backend validation:

```
python -m unittest tests.test_public_catalog_analytics tests.test_public_catalog_reader tests.test_public_job_page tests.test_public_jobs_catalog tests.test_public_catalog_origin tests.test_public_seo
```

The Node client contract runs through the Python test. The opt-in Edge harness
uses synthetic rendered documents, a downloaded public tag snapshot, and optional
paired gateway module/CMP snapshot. All GA collection requests are intercepted;
it does not populate the production property. Set `NODE_PATH` to the isolated
Playwright dependency installation, then:

```
python -m tests.export_public_catalog_analytics_fixture PATH_TO_FIXTURE_JSON
node tests/public_catalog_analytics_browser.cjs FIXTURE_JSON GTAG_SNAPSHOT_JS OUTPUT_JSON OPTIONAL_GATEWAY_MODULE OPTIONAL_CMP_SNAPSHOT
```

The optional real CMP check allows only its public GETs; logging writes are blocked.
TCF accept/deny/withdrawal use deterministic CMP decisions. A real regional CMP
run supplements those cases; it does not prove a real GDPR banner acceptance.

Supporting primary references:
[Google consent mode](https://developers.google.com/tag-platform/security/guides/consent),
[Google pageviews](https://developers.google.com/analytics/devguides/collection/ga4/views),
[GA configuration](https://developers.google.com/analytics/devguides/collection/ga4/reference/config).

## Coordinated publication and acceptance

1. Parent owns the exclusive backend/website window, fresh source/config drift,
   capacity and backup checks with the daily inventory and candidate work. This
   preparation authorizes no server restart, maintenance, credential extraction,
   property change, candidate enablement or independent publication.
2. Choose GA-only commits on the published backend/website ancestors, or reviewed
   candidate consolidation with its own release authorization. Keep candidate
   frame injection excluded from the empty measurement document. Preserve current
   DB, native maintenance budgets, privacy disclosure and protected configs.
3. Publish backend and then the matching website relay/CSP within that window.
   An older relay blocks the new external scripts/frame and measurement fails
   closed. Do not promote an unrelated or stale website source.
4. Repeat the intercepted Edge test against the freshly selected sources/tag.
   In a clean real browser verify direct `/jobs`, filtered `/jobs`, the exact live
   detail with variant/return target, and homepage-to-jobs document navigation.
   Confirm CMP UI/acceptance and stored choice; deny, unknown/failure and withdrawal
   must produce no collection. Confirm one request with `tid=G-QFMW1WX907`,
   `en=page_view`, clean `dl`, generic `dt`, empty `dr`, no sensitive properties,
   no CSP errors, and no duplicate view on repeated consent/normal navigation.
5. Capture a real allowed network dispatch/HTTP outcome after publication using
   only a designated test visit. Keep client/cookie/session identifiers out of the
   handoff. Then verify the corresponding view in GA Realtime/DebugView with
   authorized property access. Neither a tag, a queued command, an intercepted
   request nor a Google HTTP response establishes account-side receipt. The GA
   property connector is unavailable in this task, so receipt remains unverified.
6. Roll back only the paired code revisions through the same controlled release
   process if needed; retain data, consent records and existing GA property.
