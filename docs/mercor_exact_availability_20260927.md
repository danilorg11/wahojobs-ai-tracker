# Mercor exact public availability, 27 September 2026

The anonymous explorer is still a partial listing surface. Its retained 373-row
response and the public Explore page expose the same identities without an
authoritative total or continuation contract. This change does not label that
response complete and never closes jobs because they disappear from it.

Daily collection reads existing active Mercor rows from the live database with
the existing read-only connection. After the explorer request, it checks up to
100 known missing identities. Up to100 are checked oldest last_seen_at first;
if more exist, a deterministic daily UTC rotation prevents failed old IDs from
permanently starving other known IDs. Each receives at most
two anonymous GET attempts: its already stored public role URL and at most one
observed redirect to the same hostname and exact listing ID. Cross-ID redirects,
private/authenticated destinations, repeated redirects, 403/404, challenges,
missing fields and failures remain unconfirmed. There are no retries.

The compiled Mercor envelope is 201 HTTP attempts and 360 seconds. Configuration
must explicitly increase the existing 1-request/60-second default to use this
extension. The aggregate native execution ceiling remains 2580 seconds; increased
Mercor budgets must fit alongside every configured sibling source. A 59-record missing cohort requires at most 119 requests including
the explorer. Reduced budgets, source deadlines and more than 100 missing
identities leave explicit pending IDs in the journal and source report. Every
request consumes the existing shared budget; there is no second crawler budget.

A qualifying HTTP 200 public page must bind its route, query listing ID, role ID,
canonical HTML link, Next SEO canonical and slug to the final exact URL. It must
contain typed status, deletedAt, isPrivate and disableApplications fields,
plus the supported closedListing and candidateStatus values.

- Open: active, not deleted, public, applications enabled, and visible “Apply now”
  control. A server-rendered disabled attribute on that control is ignored because
  the retained currently open control has that hydration state.
- Closed for public applications: active, not deleted, typed applications-disabled
  state, and the exact visible “This listing is no longer accepting applications.”
  message linking to Explore. Privacy alone is not a closure signal.
- Any other combination: unconfirmed, with no lifecycle or freshness change.

Every response is retained before parsing, including redirect/error bodies.
The sealed result stores an exact-page record binding original bytes/hash,
transport timestamps, request path and current known database identity. Loading
the collection rechecks those requests and body hashes against the retained raw
HTTP journal. Publication rechecks the HTML and all current job identities before
any catalog mutation, and the existing pipeline transaction rolls back all work
if any record/candidate fails validation.

A validated negative makes only the corresponding known active job inactive and
adds its dated removal event. last_seen_at and accepted content are untouched.
A validated positive appends mercor_public_page_active_record_v1, using
job_source_promotion_v2 / held_degraded / mercor_page_availability_only.
That contract renews only the exact availability clock; it does not promote the
page HTML, overwrite accepted details/pay or change semantic acceptance. Jobs
without an existing accepted-content row remain pending on a positive page.

The exact transport timestamps remain in the journal. Published lifecycle clocks
are truncated to whole seconds to match the existing crawl-run clock, never
advanced to publication time.

## Compatibility and rollback

No database tables or columns are added. Existing catalog/detail contracts retain
their historical validators. Nevertheless, publishing the new positive capture
introduces a new versioned contract in the capture history. A pre-change reader
may reject that history while verifying captures. A code-only rollback to
68488ca (or any release without the new reader/contract/SQL freshness branch)
is therefore **not a verified rollback** after positive publication.

Keep a rollback build that understands mercor_public_page_active_record_v1,
or restore a consistent pre-publication inventory backup under the established
maintenance procedure. Do not claim older code is compatible merely because the
schema and accepted semantic payloads are unchanged.

## Validation

Offline controls cover open/closed pages; absent, private, malformed and challenge
responses; bounded redirects and errors; allowlisted IDs; retention failure;
shared request exhaustion; codec round-trip; sealed journal/body tampering;
forged identities, content and dates; unchanged unseen jobs; explicit exact
closure without freshness renewal; positive freshness with identical accepted
content/pay; current acceptance replay; and full pipeline transaction rollback.

Local parsing of the two retained real public pages also qualified the current
open role and the historical applications-closed role as expected. No additional
public fetches, database repair, production configuration changes or deployment
were performed for this implementation.

