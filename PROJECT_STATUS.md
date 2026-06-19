# Wahojobs AI Work Tracker Status

Last updated: 2026-06-19

## Purpose

Wahojobs tracks the public AI-work market over time. It is focused on market intelligence rather than acting as a traditional job board: which opportunities exist, which sources expose them, what changed since the last crawl, and how much of the observable market is live versus public inventory or evergreen application surface.

The tracker currently runs as a local Python and SQLite system with manual CLI workflows, deterministic crawlers, historical job lifecycle tracking, CSV exports, and terminal/Markdown reports.

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

- Raw active live postings: 8,841
- Estimated live market opportunities: 2,481
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

## Known Limitations

- The system is still local/manual: SQLite, CLI scripts, CSV/Markdown exports, no hosted UI.
- Some source APIs and public pages are unofficial and may change without notice.
- Handshake depends on public Framer CMS module/chunk structure.
- DataAnnotation is an evergreen source, not a live project feed.
- DataAnnotation `/bilingual` is retained in the allowlist but currently returns 404 and is skipped.
- Some sources are canonicalized while others still count raw jobs as opportunities.
- Source taxonomies are not yet normalized into a single Wahojobs market taxonomy.
- Historical counts reflect local crawl history and may include local failed crawl_run artifacts.
- Exports are snapshots of the local SQLite database, not a hosted canonical dataset.

## Recommended Next Source Research

High-value candidates to research or revisit:

- TELUS Digital AI Community: next recommended research/spike candidate; high relevance for search/ads evaluation, but crawl reliability needs confirmation.
- Centific / Pactera EDGE: potentially relevant for data annotation and evaluation work.
- Scale AI / Remotasks: strategically important; Remotasks should remain a lower-priority evergreen or strategic candidate unless a stable public worker-facing inventory is found.
- DataAnnotation expansion: monitor whether additional public domain pages become reachable, especially bilingual/language pages.
- Handshake AI expansion: monitor whether public inventory structure, pagination, or detail/apply fields change.
- Search evaluation platforms beyond Appen/RWS/Welocalize/TELUS: useful for broader coverage if public feeds exist.
