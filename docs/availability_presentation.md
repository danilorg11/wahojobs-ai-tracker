# Availability wording on authenticated details

`public_job_page.prepare_public_job` attaches `availability_trust` from the
existing `assess_opportunity_trust` function for the selected source variant.
Authenticated scoped details already use the same authoritative observation
inputs as admission, including qualifying individual Mercor observations.
This additional assessment is presentation provenance only: it does not change
`public_state`, representative selection, recommendation membership, or actions.

The authenticated detail renderer maps existing reasons as follows:

| Evidence | Heading | Meaning |
| --- | --- | --- |
| Current/live | No historical-status banner | Existing current behavior remains. |
| `stale_source` | Availability needs rechecking | A qualifying check exists but is no longer recent. |
| `unverified_source` | Availability not verified | There is no qualifying verification; no prior successful check is implied. |
| `inactive`, or explicitly inactive flags in a retained packet | Listing marked inactive | This is the status in saved records, not a claim that the employer confirmed closure. |
| Retained non-current packet with missing/unrecognized reason | Availability not established | Saved information cannot establish current availability. |

All non-current messages retain the saved description for reference. The
renderer does not inspect dates, calculate freshness, or fetch source pages.
It retains the original source-reference link; this is distinct from enabling
the existing current-only application/source action button.

The 72-hour live-feed verification rule, 168-hour Matches fallback, profile
eligibility checks, run validity, and application-action restrictions are
unchanged. Stale fallback cards still carry their existing availability caution.
Missing identities remain not-found, and inconsistent source identities remain
rejected before rendering. Employer text, comparisons, and source references
are preserved.

Synthetic fixed-clock coverage lives in
`tests.test_authenticated_detail_availability`, alongside the existing
per-record observation, expiry, partial-source, and old-run tests. This change
does not verify external application acceptance or imply user visual acceptance.
