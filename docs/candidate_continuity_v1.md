# Candidate continuity v1 — execution plan and contract

Outcome: an authenticated candidate saves an exact posting, records progress,
hides it, returns through My Jobs, restores consideration, and reads current local
source details without losing history or transferring another posting's state.

Baseline: `0760ea6d45323508b8b82feb596ed4bf66d7c335`. Isolated branch:
`codex/candidate-continuity-v1`. The original main checkout is protected.
No applicable AGENTS.md was found. This is the single execution plan, following
the repository's `docs/` contract-document convention.

## Contract map and decisions

- `pipeline_postings` links new workflow to owner, persisted job row ID, company
  ID and exact source hash. Titles, URLs, canonical groups and accepted captures
  are not workflow keys. Same stable posting recaptures preserve history;
  different postings receive different owner-scoped keys.
- `pipeline_records`, `pipeline_actions`, `pipeline_state` retain the existing
  normalized workflow, visibility, reminders, versions and append-only events.
  No new schema or migration is needed for this milestone.
- Legacy records link only through exact provider/external-ID evidence, or a
  unique exact source URL when no external ID exists. Opaque identifiers stay
  case-sensitive. Unresolved/ambiguous records retain their histories and receive
  bounded returns. Duplicate linked histories remain separate; any linked hidden
  history suppresses that posting until explicitly restored.
- `authenticated_profile_matches` removes hidden rows before representative
  selection and display caps. Cache proofs include visibility and enclose the
  workflow read within the inventory commit boundary. Qualification, geography,
  language, freshness, preferences, scores and ranking are unchanged.
- Source actions revalidate exact run/source/capture binding on POST. Private
  history actions use current authenticated owner authority and version, so a
  closed source does not prevent recording progress. CSRF, ownership, idempotency
  and mutation transactions remain effective. GETs do not mutate workflow.
- `/tracker/item?item=...` resolves current owner history and renders the existing
  authenticated exact-source component without requiring an old run. It never
  substitutes a sibling. Exact authenticated returns bypass public canonical
  redirects; ordinary unselected public routing is unchanged. Missing or unsafe
  evidence preserves history, the original safe external link and controls.
- Shared cards/details/My Jobs show English feedback and Hidden/Show again
  controls. Full history owns its reminder display. Inline scripts have a precise
  CSP hash and same-origin requests. The durable login wrapper forwards workflow
  routes to the normal authenticated composition.

Restore grants ordinary consideration, not guaranteed display, eligibility or
availability. Applied stays applied; reminders and events survive the cycle.
Opening an employer link does not mark an application. Professional-background
interpretations retain stricter capture-bound validity. No source collection,
semantic preparation or model prompts were changed.

## Progress and validation

Acceptance correction (2026-09-13): the owner's actual browser failure suspended
integration readiness. The served client sent a charset parameter
that the strict authenticated form adapter rejects. A real DOM click through the
shipped script reproduced Save and Applied returning 400 without mutations;
controls recovered but later actions hit the same rejection. The client now sends
the existing strict media type; the authenticated parser and security contract
are unchanged. The complete DOM-to-HTTPS journey passes, including response-specific
feedback, failure recovery, exact sibling isolation, foreign-owner rejection and
fresh-process return. Console demo instructions use ASCII to avoid a Windows
redirected-output encoding crash. Focused independent review and final 636-ID plus
affected-suite validation are the remaining acceptance gates; their final receipts
and fresh-demo source identity belong in the existing evidence bundle. Preserve
the original failure and all intermediate observer failures there.

1. **Complete:** verified baseline; reproduced hidden final-slot consumption,
   fuzzy state inheritance and missing local My Jobs return. Existing normalized
   hide/restore history preservation was protected.
2. **Complete:** connected implementation and synthetic product demo. Tests cover
   fresh processes, owners, duplicate/stale actions, recapture/change/closure/
   reappearance/expiry, ambiguous legacy histories, conditional placement,
   competition and exact returns.
3. **Complete:** independent read-only review, repair and re-review. Corrected
   public redirect loss of exact identity, a concurrent-Hide cache race and
   duplicate inline reminder rendering. No actionable review findings remain.
4. **Final verification:** run the unchanged 636-ID selection plus new continuity
   tests and affected workflow/login suites against the exact Git source snapshot.
   The linked bundle records final results, source hashes, selectors, commit/tree
   identity and preservation. Retain failures and classify unrelated baseline
   failures explicitly.
5. **Delivery boundary:** checkpoint only this feature branch. Main integration
   and activation remain the owner's later decision.

One [evidence bundle](../../candidate-continuity-evidence/index.md) holds receipts.
Runtime: Python 3.12 (`C:\Python312\python.exe`), existing locked dependencies,
UTF-8, no startup hooks/bytecode, deterministic hash seed, unittest and disposable
storage. The runner blocks external traffic and port 8802. The existing no-access
test uses a clearly synthetic sentinel rather than a real database.

Browser: recorded real-handler pages inspected at desktop, 390px and 320px;
keyboard restore focus and Enter-operated history disclosure checked. A separate
actual-handler JSON replay checks inline reminder count, feedback and focus.
Live authenticated browser interaction is unverified because the available
browser rejected the generated local TLS certificate. Handler authentication
and HTTP journeys were executed normally, independently of that DOM replay.

## Reproducible synthetic demonstration

From this feature checkout, using the documented Python/dependencies:

```powershell
C:\Python312\python.exe -B scripts\candidate_continuity_demo.py
```

Open the printed local URL and use its controlled local login. In Matches, open
a job, Save, Mark as applied, set a reminder, then Not interested. Open My Jobs,
choose Hidden, View job details, and Show again. Applied and the reminder remain.
Return to Matches; the restored posting competes normally. Similar postings are
independent. External links use `example.test`; no employer request is needed.
Ctrl+C stops this disposable app and removes its synthetic database and owners.

Automated journey, including fresh process returns:

```powershell
C:\Python312\python.exe -B -m unittest tests.test_candidate_continuity
```

Client regression (Node 22+, test-only pinned jsdom; no production dependency):

```powershell
npm ci --prefix tests/client_dom --ignore-scripts --no-audit --no-fund
C:\Python312\python.exe -B -m unittest tests.test_candidate_continuity_client
```

This executes native DOM click/submit, the actual served inline script,
FormData/URLSearchParams, and real authenticated HTTPS responses, then compares
rendered feedback with persisted synthetic state. Only browser-managed cookies,
headers and transport are supplied by the observer; successful action fields and
responses are never invented. It does not claim full-browser TLS/CSP enforcement,
keyboard or layout coverage. The earlier static/response replay did not cover
the client request boundary and is retained only as historical visual evidence.

## Stable working boundary and limitations

Work only in this feature worktree and evidence bundle. Do not access recovery
port 8802, real/workspace/recovery databases, companion store, configuration,
unrelated untracked contents or other worktrees. No real migrations, source/model
calls, live semantic imports, automatic preparation, external authentication/
submissions, push, merge, deployment or existing-app restart. Disposable
authentication, fixtures, test processes and scoped local commits are authorized.
Do not alter historical evidence or attribute directory-metadata discrepancies.

Ambiguous legacy links intentionally retain separate histories. Missing local
source evidence cannot be reconstructed from title similarity. Synthetic tests
and code review are not live authorization evidence, human acceptance or general
matching-quality validation.
