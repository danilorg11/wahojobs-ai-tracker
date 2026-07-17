# Greenhouse source-registry pilot

The Greenhouse pilot is deliberately separate from normal crawling and product
inventory. Registry contract version 2 uses three exact Boolean controls:

- `connector_enabled_for_dry_run` permits public GET validation through the pilot.
- `product_enabled` permits product inventory admission.
- `production_crawl_enabled` permits ordinary crawler dispatch.

GitLab, Customer.io, and Testlio are dry-run-only. Invisible is disabled for all
three controls, including legacy aliases. Meridial is the reviewed production
control and retains root department `4012485101`.

The registry rejects unknown fields, unsupported versions, malformed nested
objects, unknown enum values, and arbitrary family, country, region, or language
targets. Job hosts are explicit per source. A Greenhouse job URL is accepted only
when its HTTPS host is allowed and its path is exactly
`/{board_identifier}/jobs/{greenhouse_job_id}`.

## Operational safety

Each entry has a count-drop policy. The default applies after a previously accepted
snapshot of at least 10 rows and quarantines a new result retaining less than 50%.
The observed rows remain diagnostic, but the anomalous result is non-successful and
cannot close missing rows. Failed, partial, and anomalous observations never replace
the last accepted count.

Readiness uses only version-controlled or future durable observations. It requires
three distinct run IDs and timestamps, the same parser contract, complete and
non-anomalous outcomes, an unbroken trailing streak, and at least 24 hours between
the first and last observation. Repeating `--snapshots 3` is a technical probe and
records zero readiness observations.

Regional labels are applicant restrictions. `Americas`/`AMER`, `EMEA`, and `APAC`
use the documented country mapping in `wahojobs/matching/locations.py`; exact labels
such as `Remote US` and `Remote Germany` remain country-specific. Generic `Remote`
is unknown rather than worldwide. Only explicit `Global` or `Worldwide` location
metadata is broad.

## Dry-run command

The command prints deterministic JSON to stdout:

```powershell
python -B scripts/greenhouse_pilot.py --snapshots 1 --lifecycle-probe
```

Use `--format human` for a compact summary derived from the same report object.
Both formats name technical validity, structural completeness, count-anomaly
safety, closure authorization, historical readiness, terms, coverage, product
enablement, and production-crawl enablement separately.

It uses public Greenhouse GET surfaces. Lifecycle and Meridial canonicalization
probes use automatically removed temporary SQLite databases. Persona coverage reads
the current baseline database in SQLite read-only mode, keeps pilot rows in memory,
and reports baseline versus each board and all pilots combined.

Exit codes are:

- `0`: every selected board completed a strict, non-anomalous technical dry run;
- `1`: any board was partial, malformed, contract-invalid, URL-invalid, anomalous,
  or otherwise incomplete;
- `2`: command usage or registry/configuration validation failed before execution.

`--require-production-ready` also returns `1` when technical validation passes but
terms, historical readiness, closure, acceptance, coverage, or enablement gates are
not satisfied.

## Metric definitions

The report separates raw records, accepted source records, stable identities, exact
titles, normalized title repetition, and actual canonicalization. Canonical count
and yield are `null`/`unmeasured` unless the existing canonicalizer ran in a temporary
database. Title repetition is never labeled canonical duplication.

`relevant_posting_count` is the number of distinct fetched source records admitted
by the unchanged personalized product projection for at least one of the 28 reviewed
personas when that source is evaluated independently. It is explicitly `unmeasured`
when `--skip-coverage` is used. Coverage deltas are separate:
they compare the complete read-only product baseline with each pilot and with all
pilots combined. Regional and qualification rejection counts are calculated across
the full evaluated result set rather than a capped top-results sample.

Rich source records preserve typed scalar fields plus deterministic JSON for public
metadata, compliance, education, compensation, and the bounded raw public payload.
HTML and URLs remain untrusted source data; browser rendering must still escape them.

## Production boundary

Technical validity is not production readiness. Until company-specific terms are
approved, independent acceptance is recorded, three historical snapshots pass,
temporary closure behavior passes, and persona coverage has no new eligibility
leakage, pilot entries remain `product_enabled=false` and
`production_crawl_enabled=false`.

Testlio `Future Roles` remain always-open public leads. Testlio airport work is kept
as local field work in pilot rows, not promoted as remote digital inventory.
