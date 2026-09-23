# Daily inventory commissioning — September 23, 2026 UTC

**Nine sources are active after the first automatic cycle. micro1 failed access
qualification and is disabled. The five previously blocked sources remain disabled.
All-15-source daily coverage is still incomplete.** No duplicate collection was run.

This dated receipt supersedes the September 22 “first automatic result pending”
status. The [machine-readable receipt](../deploy/private-beta/daily-inventory-commissioning-receipt-20260923.json)
accounts for all 15 sources, exact approved endpoints, budgets, outcomes, evidence
lineage, verification cohorts and the effective policy reduction. The reviewed
[activation manifest](../deploy/private-beta/daily-inventory-activation-manifest.json)
remains the historical approved ten-source scope; disabled example files are not
the active host configuration.

## Release and real automatic execution

- Deployed implementation: `33262a4856ce3af04b0781a794055d0dcb0d9801`.
- Tree: `b567c67ba2f096613080cf0b98e5caa01073369d`.
- Archive: `daily-online-33262a4.tar.gz`; SHA-256
  `a3934390017ed4d370deaeac1595e479513e759a94d96cbd0bebd35e86ee9a6e`.
- Development line: `codex/daily-inventory-operations-v1`; accepted `094f496` and
  compensation-retention checkpoint `9bd4712` are ancestors. This receipt adds
  documentation only; it does not represent another hosted code deployment.
- Host: `wahojobs-private-beta-rehearsal-20260917`, Ubuntu 24.04 / systemd 255.
- Authority: `/var/lib/wahojobs-beta/rehearsal-recovered-001/product.sqlite3`,
  its existing correction-draft sidecar and sibling `journal`.
- Config-002, effective private-beta runtime, WorkOS mode, public routing and
  invitation readiness were preserved. Main and the public site were not changed.

`wahojobs-inventory.timer` actually fired at **September 23, 06:00:00 UTC**.
Native invocation: `285d087ad5a94db6a4a0ada73d4c4a06`.
Run: `20260923T060000Z`; trigger evidence and native journal agree. No other
collection run was created. The service exited 2 with `partial_or_failed` because
micro1 failed; normal beta service resumed. This is not reported as ten-source success.

Next collection: **September 24, 06:00 UTC**, then daily at 06:00 UTC.
Both timers remain enabled and active. Failed slots remain consumed, no automatic
retry or catch-up burst occurs, and restart catches only the latest slot within
the existing 60-minute window. Mindrift retains its 12-hour cooldown.

## Actual source results

“Opportunities” means canonical opportunities. “Variants” means exact normalized
posting records. “HTTP” counts attempts, including micro1's failed request.
“Pages” counts successful response pages, not listing rows or opportunities.

| Source | Outcome | HTTP / successful pages | Raw source rows | Observed opportunities / variants | New opportunities / variants | Changed existing variants |
|---|---|---:|---:|---:|---:|---:|
| Alignerr | Complete supported surface | 47 / 47 | 5,624 | 491 / 5,624 | 0 / 0 | 0 |
| Appen | Complete public board | 1 / 1 | 32 | 32 / 32 | 32 / 32 | 0 |
| Meridial | Complete approved board + department check | 2 / 2 | 832 | 569 / 832 | 569 / 832 | 0 |
| Mercor | Qualifying individual observations; partial surface | 1 / 1 | 375 | 375 / 375 | 13 / 13 | 8 |
| micro1 | Failed: HTTP 403; now disabled | 1 / 0 | Unknown | Unknown | Unknown; nothing published | Unknown |
| Mindrift | Complete validated public Workable surface | 14 / 14 | 117 | 12 / 117 | 12 / 117 | 0 |
| OneForma | Complete approved WordPress inventory | 1 / 1 | 32 posts | 32 / 457 | 32 / 457 | 0 |
| RWS | Complete board, TrainAI filter applied | 1 / 1 | 73 | 42 / 42 | 42 / 42 | 0 |
| Turing | Complete supported job-list response | 1 / 1 | 247 | 223 / 247 | 223 / 247 | 0 |
| Welocalize | Complete board, Welo Data filter applied | 1 / 1 | 530 | 170 / 421 | 170 / 421 | 0 |

Total: **70 HTTP attempts / 69 successful pages**, below the approved 232-request
cap. Qualifying observations cover **1,946 opportunities / 8,147 variants**;
**1,093 opportunities / 2,161 variants** were newly published, **8** existing
variants changed and **5,978** reconfirmed. Unknown micro1 counts are not zeros.
No source reported an abnormal count drop.

No confirmed closures occurred among qualifying sources. Complete sources have
zero missing/uncertain/stale records. Mercor has **49 missing records**, including
**39 with expired verification** and **10 still within their older validity
period**; none was closed because it was absent. micro1's lifecycle outcome is
unknown because its collection failed.

Alignerr's stable 5,624-row total at 120 rows/page requires `ceil(5624/120) = 47`
requests; the 100-request cap was not approached. Unique identities, pagination
and the terminal count were validated. Mercor returned exactly one `listings`
array, with no retained continuation signal. Its 375 individual observations do
not establish provider-wide completeness. The one-request limit is not a reason
to refetch the same response or to infer closures.

RWS excluded 31 non-TrainAI board rows; Welocalize excluded 109 rows outside the
approved Welo Data surface. Those are filters, not rejected eligible opportunities.
OneForma's 32 source posts expanded to 457 application/language variants. There
were no rejected normalized records among qualifying sources. Thirty Alignerr
summaries and one Mercor summary retained compatible older accepted detail;
those content holds did not prevent qualifying availability reconfirmation.
All 8,147 observed qualifying variants are displayable. micro1 had no qualifying
listing body to publish. No optional description backfill or paid model ran.

## Product and evidence

At the read-only check, current catalog: **1,956 opportunities / 8,157 variants**.
Stored inventory: **1,995 opportunities / 8,196 records**. The current catalog
includes ten older, still-fresh Mercor variants beyond this cycle's observations.
Its net opportunity increase of 1,094 comprises 1,093 new opportunities and one
previously expired Mercor record reconfirmed by this cycle.

Real stored Browse and detail rendering, all five filter groups, and shared
Matches verification data passed. Representative details from each of the nine
providers used the same exact variant and compensation presentation as Browse.
The checks used current runtime expiry, without an injected clock or synthetic
candidate account. Existing source classifications were preserved.

Mercor's regression record still displays **$50 per hour USD**. Detail evidence
and original capture date remain **2026-09-21T01:49:14.011208+00:00**; availability
alone advanced to **2026-09-23T06:00:27+00:00**. No manual restoration was used.
All nine publication journals match their retained collection lineage, are
non-synthetic, and record **zero publication HTTP requests**.

The daily cold snapshot verified 628 files at
`/var/lib/wahojobs-beta/daily-inventory-v1/backups/20260923T060000Z`.
The finish worker proved unchanged protected account/profile/draft/saved-job and
workflow domains and passed database integrity checks before reopening beta.
No unrelated user data was modified during this commissioning inspection.

| Source/cohort | Last qualifying verification (UTC) | Verification expiry (UTC) |
|---|---|---|
| Alignerr, 5,624 variants | Sep 23 06:00:17 | Sep 26 06:00:17 |
| Appen, 32 | Sep 23 06:00:20 | Sep 26 06:00:20 |
| Meridial, 832 | Sep 23 06:00:23 | Sep 26 06:00:23 |
| Mercor, 375 returned | Sep 23 06:00:27 | Sep 26 06:00:27 |
| Mercor, 10 missing but still fresh | Sep 21 23:00:39 | **Sep 24 23:00:39** |
| Mercor, 39 missing and expired | Sep 18 13:07:32 | Expired Sep 21 13:07:32 |
| Mindrift, 117 | Sep 23 06:00:36 | Sep 26 06:00:36 |
| OneForma, 457 | Sep 23 06:00:48 | Sep 26 06:00:48 |
| RWS, 42 | Sep 23 06:00:52 | Sep 26 06:00:52 |
| Turing, 247 | Sep 23 06:00:54 | Sep 26 06:00:54 |
| Welocalize, 421 | Sep 23 06:01:08 | Sep 26 06:01:08 |

micro1 has no qualifying verification or freshness deadline. All nine active
sources next check September 24 at 06:00 UTC; older Mercor cohorts remain separately
visible to health monitoring even when other Mercor records are renewed.

## Maintenance and effective operating policy

Total automatic execution: **261.472 seconds (4m 21.472s)**.
Collection finished at 06:01:10.331918 UTC while beta remained online.
Measured stop/publication/recovery-to-ready interval:
**06:01:10.331975–06:04:23.218601 UTC**, **192.887 seconds (3m 12.887s)**.
Recovery accounted for 6.479 seconds. Reporting and email were outside maintenance.
This is the measured maintenance boundary, separate from total collection runtime.
The September 22 deployment outage was separately measured at **27.302 seconds**.

At September 23 21:19 UTC, the reviewed micro1-only policy change disabled its
entry and recalculated the aggregate to **182 HTTP attempts / 1,800 seconds
(30 minutes), plus 120 seconds recovery**. The nine remaining per-source caps
and approved endpoints are unchanged. Publication still has its independent
**240-second bound plus 120 seconds recovery**; a 30-minute website outage is
not authorized. The native outer service timeout remains the reviewed 2,040-second
safety ceiling; the runner enforces the reduced 1,800-second policy deadline.

Policy: `/etc/wahojobs-inventory-v1.json`, root:beta 0640.
SHA-256 after reduction:
`5c07229ddc5711955ff7383992bf727009dd4cb4388f30ef2fb7c8531c592554`.
Restricted historical policy backup:
`/etc/wahojobs-inventory-v1.pre-micro1-disable-20260923.json`.
Do not restore that historical policy blindly: it would re-enable micro1.
The change held the established shared maintenance gate, preserved its inode,
needed no beta restart, made no employer request and caused **zero downtime**.

## Alerts, native identities and review

Resend Free HTTPS delivery is configured from
**Wahojobs Operations <alerts@ops.wahojobs.com>** to **danilo@wahojobs.com**.
Sender domain verification is complete; paid overages are disabled. The existing
sending-only, domain-restricted credential stays outside Git and archives and is
provided through systemd LoadCredential. The owner confirmed **“Received”** for
the live operational test. API acceptance is not substituted for that confirmation.

`wahojobs-inventory-health.timer` invokes `wahojobs-inventory-health.service` hourly
at :40 UTC, using stored state only. The automatic 06:40 check sent one batch
covering the first-run changes and recoveries; Resend accepted request
`01a0ccfe-2698-76bc-83e5-5f3c24662a46`. Receipt of this later batch was not separately
claimed. Checks through **20:40 UTC** recorded no additional unchanged batch.
The next native health trigger at handoff is **21:40 UTC**. Age-36 monitoring covers
49 Mercor records; age-48 escalation covers the 39 older expired records. Coverage
and collection failures remain open, including the five disabled sources and micro1.
The newly disabled micro1 policy will also appear in the next hourly coverage check.

`wahojobs-inventory.service` runs the supervisor as root and drops source/publication
workers to `wahojobs-beta`. `wahojobs-inventory-recovery.service` remains enabled
for boot recovery. Normal beta continues as `wahojobs-beta` with its original
authoritative runtime. Both timers' observed trigger and next-execution fields
were read from systemd after the policy change.
The final 21:25 UTC probe confirmed actual normal-beta readiness, the reduced
policy hash and exactly one collection run; it made no employer requests.

Prior applicable evidence remains valid: 140 Linux regressions, 71 changed-path
Linux checks, 47 local weighted-publication checks, native timer/locking/process
failure/timeout/recovery checks, and the focused independent implementation review.
These suites overlap and are not summed as unique tests. This closeout additionally
verified real publication lineage, live stored-data presentation, native execution,
compensation dates, alert deduplication and the policy reduction. Independent
review found no material blocker in that reduction. Its validator accepted the
182/1,800 limits, rejected an inconsistent aggregate, and confirmed that only the
micro1 enabled bit and aggregate execution limit changed.

## Remaining source coverage

| Source | Disabled reason | Smallest next action before admission |
|---|---|---|
| micro1 | Approved POST page 1 returned HTTP 403 Forbidden; one retained 118-byte error body, no qualifying inventory | Resolve authorized endpoint access; then a separately bounded validation of the existing scope. No access bypass, alternate endpoint, retry or budget increase was attempted. |
| DataAnnotation | Canonical redirect leaves approved path scope | Review canonical route/fixed evergreen pages, test redirect accounting and bounded validation. |
| DataForce | Generic HTTP 200 can be mistaken for an empty terminal page | Prove real inventory/terminal markup before lifecycle acceptance, then bounded validation. |
| Handshake | Unbounded asset traversal, incomplete CMS chunk coverage and missing individual promotion contract | Constrain linked assets/chunks and validate retained record visibility/identity evidence. |
| Outlier | Synthetic fallback and unsupported public-record authority | Remove fallback from qualifying collection and validate real envelope/visibility semantics. |
| Surge | Generic-page/title fallback and no qualifying retained individual-page contract | Require actual role/application evidence in allowlisted page fixtures, then bounded validation. |

The original five blockers were not changed or repaired during activation. These
coverage repairs are subsequent work; they do not delay the nine active sources.
Ordinary daily runs within the effective policy need no conversational approval.
Re-admission requires satisfying the documented access/contract condition; source,
endpoint, request, retry or paid-service expansion requires separate approval.

## Exact disable and recovery procedure

On the existing beta host, stop new timer dispatch and let the native stop handler
restore normal beta if a collection is running:

```sh
systemctl disable --now wahojobs-inventory.timer wahojobs-inventory-health.timer
systemctl stop wahojobs-inventory.service
```

Then atomically set only `enabled=false` in `/etc/wahojobs-inventory-v1.json`,
preserving its root:beta 0640 ownership/mode. As root, this uses atomic replacement
and the persistent shared gate; it fails closed if another maintenance operation owns
the gate:

```sh
cd /opt/wahojobs-beta/current
.venv/bin/python -B - <<'PY'
import json, os, stat, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from wahojobs.daily_inventory import DATABASE, validate_policy
from wahojobs.maintenance_gate import operation_gate
p = Path('/etc/wahojobs-inventory-v1.json')
assert os.geteuid() == 0
with operation_gate(DATABASE, require_existing=True):
    st = p.lstat()
    assert p.resolve(strict=True) == p
    assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == 0
    assert stat.S_IMODE(st.st_mode) == 0o640
    raw = p.read_bytes()
    config = json.loads(raw)
    validate_policy(config)
    config['enabled'] = False
    validate_policy(config)
    fd, temporary = tempfile.mkstemp(prefix=p.name+'.', suffix='.tmp', dir=p.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), 0, st.st_gid)
            stream.write((json.dumps(config, sort_keys=True)+'\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        assert p.read_bytes() == raw
        os.replace(temporary, p)
        directory = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
PY
runuser -u wahojobs-beta -- .venv/bin/python -B scripts/private_beta_health.py --config /run/wahojobs-beta/runtime.json
```

Keep boot recovery enabled until normal readiness is confirmed. Inspect
`journalctl -u wahojobs-inventory.service --all` and
`/var/lib/wahojobs-beta/daily-inventory-v1` run/source/backup receipts. If native
recovery requires operator intervention, use the existing pinned beta recovery
procedure: establish that no worker owns storage, restore normal beta service,
and verify readiness. Never delete locks, consumed slots, phase claims or journals.

Do not overwrite current data with a historical snapshot or roll back to code
incompatible with new captures. Retain compatible code and fix forward; disaster
recovery restores to a new directory and reconciles newer user history. The verified
pre-deployment backup remains
`/var/lib/wahojobs-beta/backups/daily-33262a4-predeploy-20260922` (manifest SHA-256
`4762179adcdf04bc1d808d1b1cf94156d554a71e694212f9a7db34f174deb091`).

Sanitized evidence retained outside the repository: `commission-001.json` (native
run and source receipts), `commission-004.json` (micro1's verified 403),
`commission-005.json` (publication, product, backup and health checks), and
`commission-006.json` (reviewed policy reduction and native next triggers).
`commission-007.json` records the final normal-beta readiness and policy check.
Their hashes are bound in the machine-readable receipt. No credential is included.
