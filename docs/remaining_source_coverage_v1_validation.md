# Remaining Source Coverage V1 — controlled validation handoff

Baseline: development branch `codex/remaining-source-coverage-v1` starts at deployed
`31ac4b51c5ba691d0afe6835662545ef312142c2`, which descends from
`33262a4856ce3af04b0781a794055d0dcb0d9801` through the commissioning
receipt `ba8eedbafe460c67ac3ea3a30742b4a24b2b7513`. The active nine,
182-request/1,800-second policy, hourly health, corrected alert history, and
Resend recipient are unchanged. All five pending sources and micro1 remain
disabled. This package is a proposal for later owner-controlled **read-only
validation**; it executes no employer requests or authoritative writes.

## Corrections and qualification

| Source | Implemented boundary | Tested now | Exact missing evidence before publication readiness |
| --- | --- | --- | --- |
| DataAnnotation | One canonical `/coding` → `/job-board/software-engineer` hop on `www.dataannotation.tech`; reject other destinations, error pages and pages without role/application evidence. Preserve evergreen classification and stable domain identity. | Scoped redirect and page-evidence contract fixtures. | Capture the actual canonical page body and apply destination for each fixed domain. Existing June observation does not supply current raw HTML. |
| DataForce | Require recognizable project-view rows or explicit empty state; reject generic 200, challenges and access errors. Wrapper is **partial**: invented terminal markup never closes absent records. | Rows, empty-state and error fixtures; no closure from partial. | Retain a genuine terminal response and page/header sequence; establish its exact empty-state markup before complete-snapshot promotion. |
| Handshake | Bound page-linked modules to 32, declared chunks to eight per collection, require same module path mapping, contiguous chunk indices, validated Framer CMS URLs, visible record identity/title/slug and no duplicate identity. Keep public-inventory type. | Synthetic module/chunk and visible-record rejection fixtures. | Retained page/module/CMS bodies proving the actual path mapping, all declared chunks and individual visibility. Chunk-zero alone is never completeness proof. |
| Outlier | Removed runtime sample fallback; errors propagate, empty list fails qualification, public URL/ID/title/visibility must be present. Returned records remain partial with no absence authority. | Failure/empty/invalid-record fixtures; sample exclusion. | Capture actual JSON envelope and public visibility field, plus one independently public record URL. The conservative `isPublic` test is provisional and may need a source-evidenced revision. |
| Surge | Removed slug title fallback; detail must declare a title and role-level apply link; generic page and missing fellowship mailto fail. Public-inventory and evergreen types remain distinct. | Generic/role/application/fellowship fixtures. | Retain index, detail and fellowship HTML proving exact role and application link form; verify the strict role-level URL condition against actual pages. |

No positive fixture above is asserted to be a retained employer response.
These corrections fail closed when the missing evidence is absent. Their current
partial source captures cannot yet serve as accepted content under the existing
record-promotion policy; a source-specific, versioned promotion contract and
isolated lifecycle replay are required **before activation**. Mere first
Wahojobs discovery is never an employer posting date.

micro1: the retained September 23 request was one POST to
`https://prod-api.micro1.ai/api/v1/job/portal?page=1&limit=100&keyword=`,
with the reviewed expert-filter body, and received HTTP 403 plus a 118-byte
generic Forbidden page. Earlier September 5 observation used the same
documented portal contract and returned 312 records. The available metadata
shows an explicit upstream refusal on this request; it does not establish
whether the cause is authorization, endpoint policy or a transient restriction.
No vendor, IP, cookie or login mechanism is inferred. Keep disabled. The exact
prerequisite is provider/owner confirmation that this unauthenticated portal
POST remains authorized for the beta host, or a documented authorized endpoint
and credential contract. No further probe is proposed here.

## Single controlled live-validation manifest

These are hard **proposal** ceilings, not additions to the active daily policy.
DA and DF are eligible for a read-only contract observation. The other three
are listed to keep one package, but are **not dispatched** until their stated
raw-evidence prerequisites and source-specific promotion contract review pass.
No automatic validation follows this document.

| Source | Exact request scope | Expected / hard maximum | Timeout and stop |
| --- | --- | --- | --- |
| DataAnnotation | GET 11 fixed `https://www.dataannotation.tech/{coding,generalist,law,math,medicine,physics,finance,accounting,bilingual,chemistry,biology}` pages; only coding may follow one GET to `https://www.dataannotation.tech/job-board/software-engineer`. | 12 / 12 HTTP attempts including redirect. | 30 s/request; 360 s/source. Stop on any other redirect, more than one hop, access error or generic page; preserve successful individual pages only. No retries; no cooldown. |
| DataForce | GET `https://dataforcecommunity.transperfect.com/projects`; then only `?project_type=All&page=1..19` in order. | 3 retained / 20 hard. | 45 s/request; 360 s/source. Stop on duplicate ID, changed markup, missing explicit terminal, challenge or cap. No retries; no cooldown. |
| Handshake | GET `https://joinhandshake.com/ai/opportunities/`; only page-linked `https://framerusercontent.com/sites/<exact>.mjs` and those modules' mapped `https://framerusercontent.com/cms/<exact>-chunk-default-<index>.framercms`. | 29 retained / 40 hard, 32 modules and eight chunks/collection within that total. | 60 s/request; 360 s/source. Stop on redirect, unrelated asset, chunk gap, mismatched module, malformed/duplicate visible record or cap. No retries; no cooldown. Not dispatched in this proposal. |
| Outlier | One POST `https://app.outlier.ai/internal/experts/job-board/jobs`, body `{}`; no alternative host or auth. | 1 / 1. | 30 s; 60 s/source. Stop on access error, unsupported envelope, empty/nonpublic record. No retries; no cooldown. Not dispatched in this proposal. |
| Surge | GET `https://surgehq.ai/workforce`, up to 18 exact index-linked `/workforce/<slug>` details and GET `https://surgehq.ai/fellowship`. | 9 retained / 20 hard. | 30 s/request; 360 s/source. Stop on index/detail mismatch, generic page, missing role-level apply evidence or cap. No retries; no cooldown. Not dispatched in this proposal. |

Proposed dispatch now: DataAnnotation and DataForce only, **32 attempts hard
aggregate and 720 seconds hard source time**. A denied, redirected or timed-out
attempt consumes its slot. Use isolated evidence storage, retain raw bytes,
status, final URL, headers, request ordinal and UTC capture time. Never use
candidate credentials, proxies or challenge solving. No public database write,
maintenance entry or expected candidate downtime occurs in this validation.
Qualification requires exact source identity, visible public/apply evidence,
complete-vs-individual semantics, and an isolated pipeline replay proving
accepted capture, canonical/variant identity, geography, compensation/manual
override preservation, and no absence closure from failure or partiality.
Only a separately reviewed activation may later publish qualified records.
A successful read-only observation alone changes no displayed jobs.

## Publication timing boundary

The September 23 automatic run lasted **261.472 s**. Receipt timestamps place
online collection/preparation at **68.617 s** (06:00:01.715–06:01:10.332 UTC).
The maintenance boundary was **192.887 s** (06:01:10.332–06:04:23.219).
The part before recovery was **186.408 s**; recovery/service readiness was
**6.479 s**. The first completed source (Alignerr, 5,624 variants) was recorded
at 06:02:52.804, **102.472 s** after maintenance entry, including stop,
backup and its publication. The remaining eight sources completed over
**82.008 s**, ending 06:04:14.812; finish and recovery entry took another
**1.927 s**. The receipt does not separate stop, backup, reconciliation and
derived-field/index time, and it lacks an independent candidate HTTP probe.
These are maintenance timestamps, not a measured steady-state cycle.

The later alert-only promotion's successful **27.63 s** maintenance boundary
and preceding failed attempts are separate deployment events, not daily
collection publication measurements. No steady-state replay or availability
claim is made from them.

Narrow isolated instrumentation now writes worker phase elapsed times to the
run receipt and separates stop from restore/readiness. No publication algorithm
or 240 s publication plus 120 s recovery bounds were changed. The retained
local commissioning receipt lacks the staged raw observations and authoritative
cold snapshot needed for three comparable replays. The exact next isolated
observation is to provide copies of that immutable staged evidence and cold
snapshot, replay (1) initial large publication, (2) same content with genuine
availability renewal, and (3) a small changed subset, on isolated storage while
probing the local candidate HTTP route. Record collection/preparation, lock,
stop, backup, each source's reconciliation/derived work, integrity and actual
HTTP unavailable interval separately. Preserve conflict and user-action
checks. No optimization is claimed until this measurement identifies a safe
cause; authoritative storage must not be replaced by a staged copy.
