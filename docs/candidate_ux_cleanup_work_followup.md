# Candidate UX Cleanup V1 — simpler abilities and work history

Status: included in the integrated preview manually tested and accepted by the owner on 19 September 2026. The subsequent authorization covers relevant commits and code-only beta promotion; see `candidate_ux_beta_promotion.md`. The verification below records the original isolated implementation pass.

## Candidate-facing changes

The old form exposed Roles, an unrestricted employment-description box, Activities, Skills, Software and tools, and additional skill categories. The repeated “Add experience details” links made these categories appear interchangeable without explaining what belonged where.

- **What you can do** presents skills, tools and tasks together with one Add action. Visible examples include Python, Excel, proofreading, translating Portuguese, labeling images and reviewing AI answers. Copy explicitly includes work, study and personal projects.
- **Work history (optional)** is for jobs, freelance work and internships. Each new job has a title, optional company/client, optional start/end years and an explicit current-work checkbox. The form explains that years are sufficient and unknown periods may stay blank. Current work hides/disables the end-year control while retaining its local value if the checkbox is reversed.
- Existing unstructured work descriptions remain editable verbatim. They are not parsed into guessed employers or dates. Previously independent job titles remain in a contextual disclosure when present.
- Remove is quiet; Add is outlined and separated; Review changes remains the filled primary action. Both sections use one contextual Undo stack, retaining earlier removals and restoring original nodes, order, values and focus.
- Optional per-item details now say “How you have used this” and explain courses, projects and jobs. Those facts are shown with abilities rather than as additional employment history.
- Confirmed-profile and review headings use the same simpler concepts. The previous separate Professional domains and Skills cards are presented together under What you can do; distinct professional-domain facts remain visible with a Field label.

## Data boundaries

Existing ability rows retain their original field, provenance and optional item-detail identity. The presentation does not migrate specialties or tool lists into another category. Newly entered general abilities use the existing normalized-skills input without claiming professional employment or duration. No schema, inventory, matching/admission/ranking or qualification-rule changes were made.

Guided jobs use a reversible, explicitly labelled rendering of the existing recent_roles string contract. Only that exact format is reopened as separate controls; older free text stays intact. The 128-character server contract remains enforced. No work duration is calculated from entered years. Start-only summaries say “Started 2021”, never imply current work. Compact Unicode JSON matches browser serialization so a reversible edit does not leave a false dirty state.

## Verification

- Affected run: 83 tests, with 82 passing and one newly exposed JSON-format no-op failure. After fixing it, all 18 lifecycle tests passed, including a new start-only regression (84 unique affected checks passing across these runs).
- Two final focused checks passed after readable Undo wording and current/end-year visibility were completed.
- Shipped DOM/FormData → real isolated review/save → durable profile → reopen verified new jobs, comma-containing employer names, retained legacy descriptions, new abilities, original field classification, unrelated edits, multiple removals/Undo, one submitted copy, invalid date ordering/length and no-op dirty state.
- Independent review: four focused tests and a separate six-job synthetic journey verified Unicode, legacy punctuation, row identities/order/focus, current-work toggle reversal, successive Undo and no revision on no-op review. All findings were corrected.
- Browser QA at 1440, 390 and 320 pixels: readable examples, clear action hierarchy, zero-height removed rows, one Undo notice, restored focus and no horizontal overflow. At 320 pixels, document width and scroll width were both 305 pixels.
- Functional checks used verified HTTPS. In-app visual QA used current handler captures; the generated-certificate browser warning remains a limit on a full interactive IAB login.

The same interactive preview is **https://localhost:8875/login?next=/account/profile**. Read-only gallery: **http://127.0.0.1:8874/**. The preview's current profile/source/workflow and draft fingerprints match the values captured before reload; no test job or ability was saved to its account. See sibling `candidate-ux-evidence/work-preview-verification.json`.

Screenshots: `abilities-editor-desktop.png`, `work-history-desktop.png`, `work-history-mobile.png`. Main, beta/public sites, authoritative user data and other apps were untouched.
