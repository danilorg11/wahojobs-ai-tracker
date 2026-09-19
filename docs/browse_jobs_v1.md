# Browse Jobs V1 — accepted integrated preview

Implemented in `codex/candidate-ux-cleanup-v1`, based on `d6ae22fab2bb9bd2a930249c3d051c123fc00aff`. On 19 September 2026 the owner reported successful manual testing of the integrated preview and authorized the relevant commits and code-only promotion to the existing hosted beta. That acceptance does not establish correctness of the full hosted inventory. See `candidate_ux_beta_promotion.md` for release status.

The starting 40 modified/untracked files were recorded and copied in `candidate-ux-evidence/browse-baseline/manifest.json`, `files/`, and `tracked.patch` before Browse edits. `browse-change-manifest.json` distinguishes this milestone from the profile cleanup.

## Behavior

- Reuses `/jobs`, its existing inventory query, publication/freshness checks, local search, supported facets, canonical grouping, ordering, 30-result pagination and detail/workflow routes. Shared primary navigation is Browse jobs, Matches, My Jobs, My profile.
- Keeps eligible source variants until all predicates pass on the same variant, then selects a representative and counts distinct canonical opportunities. Multi-variant enrichment uses existing source-hash-scoped `variant_facts`; unrelated shared country/language/skills cannot decorate another posting. Links identify the displayed variant.
- Country filters include explicit country, applicable region and explicit worldwide availability. Region filters include member-country postings. Remote includes remote work with unknown applicant location, and does not establish worldwide eligibility. Unspecified locations remain visible without a country filter.
- Search includes existing title, company, description, required/preferred skills and exact-bound source text. Secondary supported facets are disclosed under More filters; engagement is no longer hidden. Applied filters remain visible and can be individually removed. Empty results preserve filters, and server failure remains a separate error.
- GET forms reset the page when conditions change. High page numbers recover to the last valid page with filters retained. Detail/back links preserve normalized catalog context; canary redirects preserve validated context and variant. Arbitrary return destinations remain rejected.
- Catalog reads do not evaluate a profile or run matching. Browse details reuse the existing source presentation, currency wording, caveats and workflow, without profile comparisons or invented fit claims. Existing Matches/run paths retain their personalized behavior. Save and later saved-record actions retain Browse context. Saved status uses the existing account-bound tracking identity and never enters shared catalog cache. External application links remain ordinary links.
- Accounts without a completed profile can browse and read details; existing workflow authority still requires a profile to save. The detail explains this with a profile link. Hosted beta session/invitation gates remain intact. The controlled localhost preview now also gates catalog/detail/company routes through its existing durable session gateway, and allows `/jobs` as a safe login return.

## Verification

182 distinct tests passed across affected and follow-up runs; one existing Linux/POSIX socket-restart test is skipped on Windows. The first broad run found four old local-login assertions expecting 401; the new catalog boundary correctly redirects to login with 303. Those assertions were updated and their full preparation test class passed. Public test fixtures now use their own frozen clock and distinct canonical identities; published-link assertions validate the new variant/return query.

Coverage includes source variant conjunctions and non-mutation, inactive/stale exclusion, country/worldwide/region/unknown behavior, explicit skill and bound-description indexing, canonical counts, 65-opportunity pagination without repeats, high-page recovery, filter removal, safe return context, no-profile access, separate error/empty states, cross-account rejection, saved/applied state, invitations, auth re-entry and the existing profile cleanup.

`tests/browse_jobs_client.cjs` executes actual rendered GET forms and the shipped inline workflow JavaScript against disposable authenticated HTTPS. It verifies page 2, details/back/reload, Save, Applied, My Jobs, profile preservation and filter reset. Its 65 extra opportunities exist only in disposable tests. No test opportunities were inserted in the owner preview.

Two independent reviewers found and rechecked context fixes; no remaining blocking findings in their bounded reviews. The local session-boundary addition also passed independent HTTPS probes for anonymous, expired and signed-in profileless accounts.

Desktop (1366×900) and mobile (390×844) layouts, detail views and the More filters disclosure were inspected in the browser using captures fetched from the current preview with certificate and hostname verification. Full interactive browser testing remains blocked by `ERR_CERT_AUTHORITY_INVALID` in the automated browser; no certificate warning was bypassed. Actual forms/actions were tested through the verified HTTPS DOM integration above. Browser back/forward in the live UI remains unverified.

## Owner preview and data limits

- Interactive: `https://localhost:8875/login?next=/jobs`
- Read-only gallery: `http://127.0.0.1:8874/`
- Review: search Python → open details → Back to Browse jobs → open and Save → My Jobs. Use a country filter to inspect the honest empty state, then remove its chip. Do not use gallery forms to operate the live preview.
- Preserved inventory: 6 source postings, grouped into 5 opportunities. Python currently returns 2. Brazil returns 0 because the catalog has no confirmed applicant-country evidence for these fixtures. The preview cannot demonstrate a second page; representative pagination/geography cases were verified only in disposable tests.
- The two profiles, five stored profile revisions, eight profile-source rows, all inventory/source/enrichment rows, workflow tables and correction drafts have identical logical hashes before/after. Preview Save was not submitted during this pass. Earlier profile UX and clean Matches layout are preserved; only shared navigation changed there.

Evidence is under the sibling `candidate-ux-evidence` directory: `browse-preview-verification.json`, `browse-preview-before.json`, `browse-preview-after.json`, `browse-client/`, regression logs and `visual/browse-*.png`. Screenshots are current read-only page captures, not proof of an interactive browser action.

Owner acceptance covers the manually tested preview interactions. Certificate-blocked browser automation remains recorded accurately above; it is not counted as a successful automated journey. Main integration, public-site changes, invitations and other beta-readiness gates remain outside this promotion.
