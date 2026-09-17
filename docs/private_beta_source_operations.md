# Initial beta inventory — bounded existing-provider plan

This is an authorization-ready plan, not a source verification receipt. No live
source or model request was made in this implementation phase. Evidence maintenance
v1 is the only operating entry point; no new adapter, model choice, ranking policy,
recommendation quota or background refresh is introduced.

## Scope and prior evidence

Choose **Alignerr and Mercor**, the previously reviewed maintenance-v1 cohort.
Alignerr offers structured catalog identity plus supported exact detail recovery;
Mercor supplies positively observed listing bodies with explicit partial-catalog
limits. Their prior public wording covers general evaluation, language-adjacent,
support and specialist work useful for comparing the intended candidate profiles.
That is a reason to evaluate the cohort, not a claim that it currently has suitable
vacancies. Meridial/micro1 remain historical contrasts and are not requested.

The named accepted release's SOURCE-OPERATIONS.md preserves a 2026-08-29 inventory
metadata snapshot (8,451 then-active rows), three Mercor bodies captured
2026-09-05T00:57:11.035749Z, six Alignerr detail bodies captured
2026-09-05T12:57:48.991449Z–12:57:50.392765Z and accepted Generalist wording captured
2026-09-14T13:00:44.369032Z. Their original manifests/provenance remain unchanged in
test fixtures. September 15 simulated acceptance was not a live refresh. No old
database is read/copied; no historical snapshot or synthetic job is transferred to
the beta. Start with zero jobs and populate only through the subsequently approved
current source operation.

## Exact first refresh/detail batch

Stop the selected beta runtime and verify its lifetime owner has exited. Run both
plans sequentially as the dedicated OS identity, with one persistent journal at
`/var/lib/wahojobs-beta/rehearsal-001/journal`. Plans are based on actual fresh storage
and delivered code, expire after one hour, and must execute only if exact inputs
still agree. A changed plan requires a new inspected plan within the remaining
batch ceiling, not a larger permission allowance.

| Plan | Exact requests | Hard batch ceilings | Expected writes and authority |
| --- | --- | --- | --- |
| A: Alignerr | GET `https://www.alignerr.com/api/jobs?limit=120&offset=<offset>` through existing pagination; GET `https://www.alignerr.com/jobs/<validated-external-id>` for needed details returned in that observation | **120 total HTTP attempts**, at most **20 detail attempts**; adapter ≤100 pages, ≤20,000 records, page size≤120; catalog timeout90s, detail25s, detail body≤2,000,000 bytes | Source jobs/captures/acceptance and crawl runs/events; canonical rollup and deterministic enrichment; ≤20 full-detail acceptance attempts. Exact row counts depend on provider response; no invented quota. Complete validated catalog can authorize supported absence lifecycle; partial/failed catalog cannot. Detail is content evidence and never catalog/availability authority. |
| B: Mercor | GET `https://aws.api.mercor.com/work/listings-explore-page` once | **1 HTTP attempt**, timeout30s, **0 detail attempts** | Positively returned supported records, accepted source bodies when admissible, run/events and deterministic canonical/enrichment effects. The catalog is always partial: absence never deactivates unreturned rows. No supported separate exact detail endpoint is assumed. |

**Combined ceiling: 121 HTTP attempts, 20 details, zero model/preparation calls,
zero document extraction.** Redirects/retries are not extra allowance; existing
transport validation and budget reservations fail closed. An interrupted/uncertain
attempt is consumed even without a response. No automatic rerun. Record every
attempt from the journal; unused allowance does not authorize a different provider.

After the batch is approved, these commands concretize its exact input-bound plans
on the new host. Names are examples of new private output files; no commands below
have been executed against a live source:

```sh
cd /opt/wahojobs-beta/current
.venv/bin/python -B scripts/evidence_maintenance.py inspect \
  --db /var/lib/wahojobs-beta/rehearsal-001/product.sqlite3 \
  --providers alignerr mercor --phase source --details needed
.venv/bin/python -B scripts/evidence_maintenance.py plan \
  --db /var/lib/wahojobs-beta/rehearsal-001/product.sqlite3 \
  --providers alignerr --phase source --details needed --http-limit 120 --detail-limit 20 \
  --out /var/lib/wahojobs-beta/plans/alignerr-first.json
.venv/bin/python -B scripts/evidence_maintenance.py execute \
  --plan /var/lib/wahojobs-beta/plans/alignerr-first.json \
  --journal /var/lib/wahojobs-beta/rehearsal-001/journal --yes --allow-provider-requests
.venv/bin/python -B scripts/evidence_maintenance.py plan \
  --db /var/lib/wahojobs-beta/rehearsal-001/product.sqlite3 \
  --providers mercor --phase source --details none --http-limit 1 --detail-limit 0 \
  --out /var/lib/wahojobs-beta/plans/mercor-first.json
.venv/bin/python -B scripts/evidence_maintenance.py execute \
  --plan /var/lib/wahojobs-beta/plans/mercor-first.json \
  --journal /var/lib/wahojobs-beta/rehearsal-001/journal --yes --allow-provider-requests
```

Run under `sudo -u wahojobs-beta` or its equivalent; create private app-owned `plans`
parent first. Save JSON output privately. Inspect/report the exact plan IDs afterward.
Do not pass `--owner-session`, `--enable-preparation`, `--allow-preparation`,
`--allow-enrichment-model` or a model budget. Source authorization includes only the
accepted pipeline's deterministic derivation effects, not paid semantic repair.

Failure handling: preserve raw/partial responses, manifest and consumed plan. Keep
held observations separate from accepted full bodies; do not label teasers full
details, stitch partial catalogs into complete snapshots or reset consumed requests.
Malformed payloads, mismatched identity, failures and ceilings remain visible. A
new observation beyond the remaining approved budget requires an amended batch.

## Freshness, usefulness and invitation acceptance

Record actual capture time, latest successful source observation, accepted-body
identity/time, held newer observation, age, lifecycle authority and supported
description coverage per source. The existing live-feed trust policy has a 72-hour
maximum observation age; other inventory categories do not automatically acquire
live-feed authority. The source clock and admission logic are unchanged. Scheduling
is disabled; an operator must obtain/run a later approved refresh when needed, and
stale content must remain honestly labelled or excluded by the existing product.

Use the intended profiles already represented in the accepted product—manual
general entry, language evidence, support experience and specialist contrast—to
assess utility on the real hosted path. With owner consent, create at most the
approved owner profiles; synthetic comparison fixtures stay local. Record for each:

- which current exact postings have sufficient accepted content to interpret;
- declared location/language/experience/workload compatibility and unresolved facts;
- Matches, plausible alternatives and withheld/blocked explanations as actually
  produced, plus the source date and exact employer destination;
- whether the bounded source set offers actionable information for that profile,
  or why it does not and what evidence remains missing.

There is **no fixed recommendation count**. A well-explained empty set can be
correct, but a cohort offering no useful accepted opportunities for the intended
candidates is not an invitation-readiness success. Do not force membership or
invent model authority to meet a target. Source freshness, recommendation fit and
confirmed employer availability are three separate claims: this source batch does
not submit an employer application or confirm that an employer will consider it.
