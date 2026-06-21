# Wahojobs AI Work Tracker Status

Last updated: 2026-06-21

## Purpose

Wahojobs tracks the public AI-work market over time. It started as a market intelligence tracker rather than a traditional job board: which opportunities exist, which sources expose them, what changed since the last crawl, and how much of the observable market is live versus public inventory or evergreen application surface.

The tracker currently runs as a local Python and SQLite system with manual CLI workflows, deterministic crawlers, historical job lifecycle tracking, CSV exports, and terminal/Markdown reports.

Wahojobs is now evolving from a crawler/job tracker into a personalized AI-work opportunity pipeline. The system is beginning to help users find, prioritize, and track AI-work opportunities based on editable profile signals, current source inventory, application status, and directional applicant-signal data.

Internal reports can remain technical because they support source quality, canonicalization, and market intelligence. User-facing prototypes, especially `scripts/product_demo_report.py`, are intended to use simpler product language and focus on what a job seeker should do next.

## Product Prototype Milestones

Recent product-facing prototypes now included in the project:

- Profile-based opportunity matching through `scripts/profile_match_digest.py`.
- Editable sample profiles through `profiles/sample_profiles.json`.
- Profile coverage analysis through `scripts/profile_coverage_report.py`.
- User opportunity pipeline prototype through `scripts/user_pipeline_digest.py` and `profiles/sample_user_pipeline.json`.
- Applicant signals prototype through `scripts/applicant_signal_report.py` and `profiles/sample_applicant_updates.json`.
- User-facing product demo report through `scripts/product_demo_report.py` and `exports/product_demo_report.md`.

The product demo combines:

- selected user profile
- best opportunity matches
- concrete next actions
- application tracker status
- new matches this week
- always-open/evergreen applications
- other public or supplemental leads
- applicant signals relevant to the selected profile
- profile strength notes

The current product direction is to make Wahojobs feel less like a raw opportunity database and more like a guided AI-work pipeline: what fits this user, what is already in motion, what changed recently, and what action should happen next.

Applicant signals are currently mock and directional. They are useful for prototyping how aggregate applicant outcomes could guide prioritization, but they should not be presented as guarantees of assessment, acceptance, or paid work.

## Source Taxonomy

Sources are classified at the source level and, where needed, at the opportunity level.

Source-level fields:

- `source_tier`: operational trust/status, such as `core`, `experimental`, or `strategic`.
- `inventory_model`: how the source exposes opportunities.
- `market_count_policy`: whether the source contributes to the conservative live market estimate.

Opportunity-level fields:

- `opportunity_kind`: what kind of opportunity the row represents.
- `availability_basis`: how the opportunity is publicly available.
- `include_in_live_market_estimate`: row-level live-estimate inclusion flag.

Current source types:

- `live_feed`: API or structured feed of live jobs/projects. These can contribute to live market estimates when `market_count_policy = count_live`.
- `public_inventory`: public structured inventory that is useful to job seekers, but not treated as confirmed live project supply.
- `evergreen_application`: public application/domain pages that let users apply into a platform or assessment funnel, but do not expose live project inventory.
- `corporate_careers`: company careers boards. These may be useful context, but are not core AI-work marketplace inventory by default.
- `experimental`: a `source_tier` for non-core or lower-confidence sources. Experimental sources remain visible when requested but are excluded from default live estimates.

## Live Market Estimate

`Estimated Live Market Opportunities` is intentionally conservative.

It includes only:

- companies with `market_count_policy = count_live`
- jobs with `include_in_live_market_estimate = 1`
- active, non-simulation rows
- canonical opportunities for canonicalized live sources where available
- raw active jobs for live sources that have not been canonicalized yet

It excludes:

- `report_separately` sources
- `exclude_live_estimate` sources
- public inventory rows
- evergreen application rows
- experimental sources by default
- corporate careers sources by default

Current known counts from the latest local run:

- Raw active live postings: 8,639
- Estimated live market opportunities: 2,409
- Handshake public inventory opportunities: 152
- DataAnnotation evergreen opportunities: 10
- Surge AI mixed/report-separately opportunities: 9

## Handshake AI

Handshake AI is classified as:

- `source_tier = core`
- `inventory_model = public_inventory`
- `market_count_policy = report_separately`

Handshake exposes a public structured opportunity inventory through Framer CMS-backed data. These records are useful job-seeker opportunities, but they are not modeled as a normal live API feed. Some public records may point into application flows and public detail pages rather than representing confirmed live project supply.

Handshake opportunities are therefore stored and exported, but they do not affect `Estimated Live Market Opportunities`.

## DataAnnotation

DataAnnotation is classified as:

- `source_tier = core`
- `inventory_model = evergreen_application`
- `market_count_policy = report_separately`

DataAnnotation exposes public worker-facing domain application pages such as coding, law, finance, medicine, chemistry, biology, math, accounting, and generalist. These pages are valid opportunities for job seekers because users can apply, complete assessment steps, and access matched work later.

The public site does not expose live project inventory. DataAnnotation rows are therefore modeled as evergreen application opportunities, not live jobs/projects, and do not affect `Estimated Live Market Opportunities`.

## Surge AI

Surge AI is classified as:

- `source_tier = core`
- `inventory_model = mixed`
- `market_count_policy = report_separately`

Surge currently tracks public worker-facing pages, not corporate careers roles:

- 8 workforce public opportunities from `/workforce/{slug}` pages
- 1 fellowship evergreen opportunity from `/fellowship`

The workforce rows are modeled as public inventory opportunities. The fellowship row is modeled as an evergreen application opportunity. Surge records are useful job-seeker opportunities and are exported with classification fields, but they do not affect `Estimated Live Market Opportunities`.

## micro1 Quality Update

micro1 now has conservative source-specific canonicalization.

Raw micro1 jobs remain stored as job variants, but micro1 now contributes canonical opportunities to `Estimated Live Market Opportunities` instead of raw active jobs. The current observed reduction is 14 opportunities:

- micro1 raw active postings: 350
- micro1 canonical opportunities: 336
- micro1 posting variants: 14

The canonicalization is intentionally conservative. It preserves meaningful differences such as language, locale/country variants, specialty/domain, seniority, commitment, and non-remote or hybrid work mode.

## Mindrift Quality Update

Mindrift remains classified as:

- `source_tier = core`
- `inventory_model = live_feed`
- `market_count_policy = count_live`

Mindrift now has a source-specific lifecycle guard against suspicious partial successful crawls. The guard runs before any job writes, job events, or removals are applied.

The guard compares the fetched job count against the stronger of:

- current active Mindrift raw job count
- recent successful Mindrift high-water count

If a successful-looking crawl is sharply lower and would imply many missing active Mindrift jobs, the crawl fails as non-authoritative instead of marking many Mindrift jobs removed. This protects the live estimate from partial Workable responses while keeping Mindrift in the live/countable source set.

Handshake, DataAnnotation, Surge, Invisible, and other `report_separately` or excluded sources remain outside `Estimated Live Market Opportunities`.

## Canonicalization Health Report

The project now includes `scripts/canonical_health.py`, a read-only diagnostic report for monitoring source quality and canonicalization health.

The report helps monitor:

- raw active jobs by source
- canonical opportunity counts
- estimated live contribution
- raw-to-canonical reduction
- duplicate title and URL signals
- unlinked active rows for canonicalized sources
- unknown taxonomy
- `report_separately` sources
- experimental or excluded sources
- top canonical variant groups
- encoding artifact diagnostics for replacement characters, mojibake, and C1 control characters
- expected source-specific duplicate URL patterns

Current key findings from the latest local run:

- canonicalized sources currently have no unlinked active rows
- the largest raw-to-canonical reductions are Alignerr, OneForma, Mindrift, and Meridial
- Handshake Unknown taxonomy rows do not affect the live estimate because Handshake is `report_separately`
- Invisible Unknown taxonomy rows do not affect the live estimate because Invisible is experimental and excluded
- encoding artifact diagnostics currently find no stored text corruption
- OneForma duplicate URLs are not treated as generic suspicious errors when they are expected application-variant URL reuse
- OneForma Vega duplicate URL groups that span multiple canonical opportunities remain watch items

## Current Core Sources

Core sources currently configured:

- Alignerr
- Appen
- DataAnnotation
- DataForce
- Handshake AI
- Meridial
- Mercor
- micro1
- Mindrift
- OneForma
- Outlier
- RWS TrainAI
- Surge AI
- Turing
- Welocalize

## Current Experimental Sources

Experimental sources currently configured:

- Invisible Technologies

Invisible remains available as a crawler, but it is treated as a non-core corporate careers source and is skipped by the default daily workflow.

## Postponed High-Value Backlog

### TELUS Digital AI Community

TELUS Digital AI Community is highly relevant for AI-work coverage. It exposes real worker-facing AI Community roles, including search evaluation, ads evaluation, data analysis, QA/rating, and onsite/community study opportunities.

Public browser-visible listings and detail pages exist, including paginated AI Community search results and stable-looking job detail URLs. However, normal local HTTP fetches currently return Cloudflare challenge pages for search results, detail pages, robots/sitemap URLs, and guessed JSON/API paths.

No production-safe public JSON, API, or sitemap fetch path has been confirmed. TELUS should therefore not be implemented yet. If a reliable fetch path is found later, the likely classification would be:

- `source_tier = core`
- `inventory_model = live_feed`
- `market_count_policy = count_live`

TELUS should affect `Estimated Live Market Opportunities` only after reliable crawling is confirmed. Until then, blocked or challenged fetches would be non-authoritative and unsafe for lifecycle/removal tracking.

### Remotasks / Scale Ecosystem

Remotasks is relevant to the Scale ecosystem, but it does not currently provide a public live inventory source for Wahojobs. Public worker-facing pages exist, including the Remotasks homepage and signup/application pages, but no public live task or project inventory was found.

Actual task/project availability appears login-gated and user-specific. Outlier remains the correct Scale-adjacent live inventory source because it exposes public live opportunities that can be tracked without treating a generic signup funnel as market supply.

Remotasks should not affect `Estimated Live Market Opportunities`. It may be represented later as a strategic or evergreen entry point if ecosystem completeness becomes important. If implemented later, the likely classification would be:

- `source_tier = strategic`
- `inventory_model = evergreen_application` or `strategic_signal`
- `market_count_policy = report_separately`
- `opportunity_kind = evergreen_application` or `strategic_signal`
- `include_in_live_market_estimate = false`

### Centific Expert Network

Centific is relevant to the AI data and expert-work ecosystem. The public Centific Expert Network page is worker-facing and application/community-oriented, but Centific itself does not expose public live project inventory.

OneForma remains the correct Centific ecosystem live/project inventory source already implemented in the tracker. Centific should not affect `Estimated Live Market Opportunities`. It may be represented later only as strategic or evergreen ecosystem coverage if needed.

If implemented later, the likely classification would be:

- `source_tier = strategic`
- `inventory_model = evergreen_application` or `strategic_signal`
- `market_count_policy = report_separately`
- `opportunity_kind = evergreen_application` or `strategic_signal`
- `include_in_live_market_estimate = false`

Do not ingest Centific corporate Workday roles as AI-work opportunities. Also avoid adding a separate OneForma Domain Expert Program record unless there is a clear product reason, because it may muddy the existing OneForma source.

## Known Limitations

- The system is still local/manual: SQLite, CLI scripts, CSV/Markdown exports, no hosted UI.
- Some source APIs and public pages are unofficial and may change without notice.
- Handshake depends on public Framer CMS module/chunk structure.
- DataAnnotation is an evergreen source, not a live project feed.
- DataAnnotation `/bilingual` is retained in the allowlist but currently returns 404 and is skipped.
- Some sources are canonicalized while others still count raw jobs as opportunities.
- micro1 canonicalization is conservative and may mildly undercount if separate clients post identical broad roles.
- Mindrift may still require monitoring because Workable/source churn can cause shortcode reposting.
- OneForma duplicate URL diagnostics are source-specific: expected application-variant URL reuse is labeled separately, while Vega cross-canonical duplicate URL groups remain watch items.
- Source taxonomies are not yet normalized into a single Wahojobs market taxonomy.
- Historical counts reflect local crawl history and may include local failed crawl_run artifacts.
- Exports are snapshots of the local SQLite database, not a hosted canonical dataset.

## Recommended Next Source Research

High-value candidates to research or revisit:

- Other worker-facing AI training platforms with stable public application surfaces: prioritize sources that expose clear job-seeker opportunities without login or challenge-gated fetches.
- TELUS Digital AI Community: high-value postponed backlog item; revisit only if a production-safe public fetch path is found.
- Remotasks / Scale ecosystem: strategic/evergreen backlog item; consider only if ecosystem completeness becomes more important than live market sizing.
- Centific Expert Network: strategic/evergreen backlog item; consider only if ecosystem completeness becomes more important than live market sizing.
- DataAnnotation expansion: monitor whether additional public domain pages become reachable, especially bilingual/language pages.
- Handshake AI expansion: monitor whether public inventory structure, pagination, or detail/apply fields change.
- Search evaluation platforms beyond Appen/RWS/Welocalize/TELUS: useful for broader coverage if public feeds exist.
