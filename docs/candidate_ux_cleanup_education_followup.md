# Candidate UX Cleanup V1 — education corrective iteration

Status: included in the integrated preview manually tested and accepted by the owner on 19 September 2026. The subsequent authorization covers relevant commits and code-only beta promotion; see `candidate_ux_beta_promotion.md`. The verification below records the original isolated implementation pass.

## Removal cause and correction

Reproduced the reported sequence against the current preview's rendered editor and shipped script: add two education rows, two Other qualifications and two Other study topics; remove each; undo and re-remove. The old code created six separate Undo controls for six distinct removals. Each legacy control occupied its own grid row, while empty Additional studies wrappers could remain visible.

Education now has one compact contextual notice backed by a section-wide LIFO stack. Each successive Undo identifies the next removed item. Original DOM nodes remain hidden with zero layout height until restoration, preserving identity, relative position, entered values and unrelated edits. Structured controls retain and restore their prior disabled state, including native validation. Restoring opens collapsed ancestor sections and focuses an enabled control. Re-removal creates one new stack action. Empty Additional studies wrappers disappear; legitimate Other qualifications/topics/schools and their Add controls remain.

Remove is a quiet entry-level action. Add education or study is separated below the entries with existing secondary styling. Review changes remains the filled primary action. No new confirmation dialog or review step.

At the entry limit, Undo still retains every removed value. If restoring after adding a replacement produces more than 24 entries, a clear limit message asks for a removal before review; server rejection preserves all submitted rows rather than dropping data.

## Degree and completion-year data flow

The owner's synthetic preview already persisted the selected type correctly:

- Current profile: `prf_d2f27826031fac14d2e335fefc06f083`.
- Confirmed correction revision 2: `pvr_6dc5c2410a77b23fc3983900dd5170cd`.
- Entry: `kind=phd`, qualification/field `Biology`, institution `U Penn`, status `completed`.

The missing PhD was a display defect: nonempty qualification text took precedence over the selected type. Server and browser labels now show **PhD in Biology · U Penn · Completed**, without repeating Biology. Existing named degree titles remain intact, generic doctorate stays Doctorate, and unspecified types do not acquire an inferred degree. Institution wording and employment history are unchanged.

The stronger save regression also exposed a directly involved completion-year defect. The confirmed-profile merge omitted represented graduation-year changes, causing a newly entered year to fail review. Once that merge was repaired, changing/clearing/removing an existing entry revealed stale year shadows. The correction now derives independent years from trusted prior V2 education, then projects the explicitly reviewed entries. Browser-submitted data cannot supply that trusted prior. New, changed, cleared and removed years round-trip correctly; independent legacy years and years still shared by another entry are preserved. This changes neither schema nor qualification/matching rules.

## Why the current matches remain generalist

This was verified against the actual preserved preview, not the live owner account or newly added demo opportunities.

The inventory has six source rows representing five canonical opportunities: Generalist, Biology Expert Network, Scientific Computing, AI Content Evaluation with Python (two variants), and AI Generalist. Thus relevant biology inventory does exist.

A current HTTPS request consumed confirmed revision 2 and projected `education_level=phd` and `degrees_or_domains=[Biology]`. The trace recorded:
1. Fresh current request: one projection/evaluation, new run.
2. Explicit request for that unchanged run: reuse, zero projections.
3. Another current request: one projection/evaluation, another new run.

All three displayed IDs 9400, 7003 and 9403. The completed entry, institution and full V2 education remained available to source-condition comparisons. The current request did not present a pre-update snapshot as current. A separate real-save regression confirms that an old run URL is reevaluated after an education revision and leaves the historical run snapshot unchanged. No explicit refresh step is required when requesting current matches.

Before the profile change, Biology was excluded for unsupported domain evidence. With the saved PhD/Biology profile, that exclusion clears and its score rises from 0 to 16 (base 4 + Biology alignment 12). It remains in `explore_only`, below the existing actionable threshold of 18; that section is omitted from the current browser list. It is not lost to the displayed-card limit, location, hidden state or stale profile data.

Scientific Computing remains uncertain at score 12 because its title-defining specialization/professional-background route is not established. The preserved source also asks for depth in named subdomains, programming proficiency, Git/Docker and stated available hours. Unknown conditions are not treated here as proven candidate failures.

**Concrete unresolved issue, unchanged by scope:** the source-condition comparator maps source “PhD” to `doctorate` and compares it literally against saved `phd`. Its degree comparison can therefore incorrectly say not established. For this Scientific Computing fixture, repairing that comparison alone does not change its separate admission/presentation outcome. In other postings comparisons may influence conditional/support states, so it is not universally a display-only defect. Qualification-rule changes were explicitly excluded from this iteration; no alias, score, admission, ranking or inventory change was made.

The owner's earlier in-memory run cannot be recovered retrospectively from SQLite. The fresh runtime trace verifies current behavior; it does not invent proof of which historical URL the owner originally viewed.

Detailed independent evidence: sibling `candidate-ux-evidence/matching-investigation.md`. Runtime evidence: `current-match-trace.jsonl` and `education-preview-verification.json`.

## Validation

- Affected regression run: 156 passed; one older guard errored because it assumed a workspace database existed. The guard now verifies that conversion does not create a database in an empty isolated checkout, and passed on rerun. Total: 157 affected checks passing across the run and guard follow-up.
- Native DOM and shipped script through verified HTTPS: mixed removals, successive Undo/re-removal, retained unrelated edits, review then final save, exactly one submitted education payload, correct remaining entries and PhD persistence.
- Focused year tests: shared linked years, independent year, year change, clear, removal, unchanged resubmission, no redundant revision.
- Matcher regression: actual persisted-current-profile authorization, real form confirmation, old-run invalidation, new/current evaluation, preserved historical snapshot. This plus the existing reuse suite passed 17 tests independently.
- Independent education reviewer: 17 focused regressions plus three new DOM scenarios; separate stale-save, untrusted prior-education injection, original-node/focus and year-lifecycle probes passed. No blocking findings remain in the reviewed correction scope.
- Desktop QA at 1440 pixels, mobile at 390 and 320. Repeated removal left one notice, no old per-item controls, and all six removed rows measured `display:none` and zero height. Checked mobile pages had no horizontal overflow. Successive real-browser Undo restored all six values.
- Full interactive IAB HTTPS login still encounters the generated-certificate warning, which browser-control rules require the owner to accept personally. Functional tests used certificate/hostname-verified HTTPS; browser visual/interaction QA used actual captured handler pages with no persistence submissions.

## Preview and screenshots

Same verified interactive URL: **https://localhost:8875/login?next=/account/profile**

Use My profile → Edit profile → Education and studies. Try several removals and successive Undo, then Review changes and Save changes. The existing synthetic PhD profile remains present. Current Matches can be requested from navigation.

Current read-only gallery: **http://127.0.0.1:8874/**. Screenshots in the sibling evidence directory:
- `education-editor-desktop.png`
- `education-confirmed-desktop.png`
- `education-editor-mobile.png`
- `education-undo-mobile.png`

The task's preview was reloaded using its existing storage. Profile/revision/source/workflow logical hash stayed `cc0edc036970d8138238337d72cbdd390e35bf5b372f6cd9f616606ac9dd1b12`; pending-draft dump remained identical. No fixture inventory was added or refreshed. Synthetic backups were retained before reload.

Active beta, public site, main, authoritative user data and other recovery/demo apps were untouched. Awaiting the owner's visual feedback.
