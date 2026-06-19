Implement a flexible opportunity/source classification layer.

Context:
This tracker started as a live job/project inventory tracker for AI-work platforms such as Mercor, Turing, Mindrift, Alignerr, OneForma, RWS, Welocalize, DataForce, Meridial, etc.

But the AI-work market is broader than clean live feeds. Some important platforms expose opportunities in other ways:

API-backed or scrapeable live feeds
public structured opportunity inventories
evergreen application pages
public teaser/detail pages
corporate careers pages
strategic market signals
experimental or low-confidence sources
login-gated marketplaces where public pages are only the entry point

Examples that motivated this:

Handshake AI appears to expose a public structured opportunity inventory through Framer CMS.
DataAnnotation exposes evergreen application/domain pages rather than a live project feed.

Important:
Do not overfit the implementation to Handshake or DataAnnotation.
Do not implement Handshake.
Do not implement DataAnnotation.
This task is only to prepare the tracker architecture so future source types can be represented cleanly.

Goal:
Add a minimal, flexible, future-proof classification layer that distinguishes how a source exposes opportunities and how each opportunity should be counted in reports.

Product principle:
For job seekers, all of these may be opportunities.
Internally, we need classification so reports, exports, market estimates, and source management remain accurate.

Key requirements:

Inspect the current architecture first:
schema
source/company config
JobCandidate model
canonical opportunity logic
stats.py
trends.py
daily_report.py
market_snapshot.py
run_daily.py
data_quality.py
exports/jobs.csv generation
any source inclusion/exclusion logic
Implement the smallest safe change.
Avoid a large ontology, new complex tables, or unnecessary abstractions unless the existing architecture clearly requires them.
Add source-level classification.

Use controlled string values, preferably centralized as constants or simple documented vocabulary.

Suggested source-level concepts:

source_tier:

core
experimental
strategic

inventory_model:

live_feed
public_inventory
evergreen_application
public_teaser
corporate_careers
strategic_signal
mixed

market_count_policy:

count_live
report_separately
exclude_live_estimate

Interpretation:

source_tier controls source trust / operational status.
inventory_model describes how the source exposes opportunities.
market_count_policy controls whether the source contributes to the existing live market estimate.
Add opportunity-level classification.

Suggested opportunity-level concepts:

opportunity_kind:

live_posting
public_inventory_opportunity
evergreen_application
public_teaser
corporate_careers_posting
strategic_signal
raw_variant

availability_basis:

api_feed
public_feed
public_cms
public_page
public_teaser_page
evergreen_page
corporate_careers_page
login_gated_after_apply
unknown

include_in_live_market_estimate:

boolean

Optional only if easy and natural:

include_in_observable_market_estimate
classification_confidence
application_url
metadata_json

Do not add optional fields if they create unnecessary complexity.

Defaults and backwards compatibility.

Preserve current behavior for existing sources.

Existing normal sources should default to something equivalent to:

source_tier = core
inventory_model = live_feed
market_count_policy = count_live
opportunity_kind = live_posting
availability_basis = api_feed or public_feed, whichever better matches the existing source
include_in_live_market_estimate = true

Existing experimental sources, especially Invisible, should remain excluded by default:

source_tier = experimental
market_count_policy = exclude_live_estimate
include_in_live_market_estimate = false

If a source already has custom inclusion/exclusion behavior, preserve it.

Market estimate rules.

The current estimated live market opportunities number should not change.

This is critical.

The existing live market estimate should continue to mean something conservative: observable live opportunities from sources that count as live inventory.

Do not silently mix future public inventories, evergreen application pages, or strategic signals into the live estimate.

If you add new report sections, they should be separate, for example:

Live market opportunities
Public inventory opportunities
Evergreen application opportunities
Experimental / excluded opportunities
Opportunities by inventory model
Opportunities by opportunity kind

But since Handshake and DataAnnotation are not being implemented yet, public inventory and evergreen counts may be 0 for now.

Canonicalization.

Do not break canonical opportunity grouping.

Existing canonicalized sources should continue to work exactly as before.

If classification fields interact with canonical_opportunities, make the minimal safe adjustment.

Do not redesign canonicalization.

Source-level vs opportunity-level logic.

Use source-level classification as defaults.
Use opportunity-level classification for specific records and future mixed sources.

This matters because some future sources may expose multiple opportunity types under one source.

Example:
A single platform may have live postings, evergreen application pages, and public teaser pages.

The model should allow opportunity-level overrides without requiring a major refactor later.

Exports.

jobs.csv should include useful classification fields if the schema supports them, such as:

source_tier
inventory_model
market_count_policy
opportunity_kind
availability_basis
include_in_live_market_estimate

Do not remove existing export columns.

Reporting.

Update relevant scripts so they keep working:

scripts/stats.py
scripts/trends.py
scripts/daily_report.py
scripts/data_quality.py
scripts/market_snapshot.py
scripts/run_daily.py

Reports should preserve the current live estimate.

If classification is implemented, add a small, clear section showing counts by opportunity/source classification.

Keep output readable and not noisy.

Migration / initialization.

Make the schema change safe for:

a fresh database
an existing local database

If the project uses init_db.py as the main schema initializer, update it accordingly.

Avoid destructive migrations.

Design constraints.

Prefer boring, explicit, easy-to-change implementation.

Avoid:

over-engineering
too many new tables
hard-coding Handshake/DataAnnotation logic
changing the meaning of current metrics
large refactors
breaking current crawlers
changing current source counts unless unavoidable and documented
After implementation, run:

python scripts/init_db.py
python scripts/stats.py
python scripts/trends.py
python scripts/daily_report.py
python scripts/data_quality.py
python scripts/market_snapshot.py
python scripts/run_daily.py
python -m compileall wahojobs scripts

If any command fails, fix the issue or explain clearly why it failed.

Final summary.

At the end, summarize:

Files changed
Schema/model changes
New classification vocabulary
Default classification for existing sources
How experimental sources are handled
Whether the estimated live market opportunities number changed
How reports expose the new classification
How exports expose the new classification
How this prepares for future sources like public inventories and evergreen applications
Any limitations or follow-up recommendations
