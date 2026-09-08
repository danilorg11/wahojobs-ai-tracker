# Optional experience details

This adds an optional `experience.item_details` list to Canonical Profile V2's
existing JSON. No database migration, intake step or historical-profile rewrite
is required. Profiles without the list retain exactly their previous meaning.

The compact correction editor offers **Add experience details** on existing
skill and activity chips. One dialog edits the selected item. Contexts can be
combined: study/training, personal/volunteer projects, and professional work.
Autonomy uses behavioral self-reports: with guidance, independent routine tasks,
or independent complex tasks. Approximate duration is stored as whole months
for that item only; blank means unknown and zero means less than one month.
These are not independently verified credentials. Professional use, duration,
and a skill mention do not imply one another or advanced proficiency.

The record holds an opaque stable item ID, the existing editable collection and
exact label, contexts, autonomy, months, and `basis: self_reported`. Existing
field-level provenance records explicit user correction/confirmation. An ID is
created only for an item given details and survives a reviewed rename. Records
cannot switch collections or move to a different still-present label. Removal
can be undone in the editor with the original link and details intact. A
confirmed deletion intentionally removes that item's details; it does not
erase unrelated entries. Re-adding a label later does not resurrect historical
experience. Older immutable revisions retain their original contents.

**Save details to draft** changes only the edit form. Cancel or Escape discards
the dialog's pending edits. **Review changes → Apply profile update** remains
the only persistence step. The complete proposal, including optional details,
uses the existing owner/revision/expiry validation and resumable draft path.
Previously retained proposals without the new form field still resume.

Comparisons use these facts only for displayed cards and requested details.
For the existing simple tool clauses, a reported context of use supports
“Experience with R”; independent routine use can support “Working proficiency
in R”. Both are explicitly attributed to the candidate. Required/preferred and
AND/OR meanings remain separate. Guided use, a duration, or professional use
alone does not establish working proficiency. Tool-specific domains, expertise,
years requirements and additional qualifiers remain unassessed or unresolved.
For example, generic R experience does not establish scientific-computing skill.
Source restrictions, contradictions and exact-variant references remain intact.

For already-recognized professional-background clauses, display mode can also
acknowledge optional context on the exact related skill/activity item. It says
"You report using Marketing professionally", or identifies study/projects,
while leaving the particular hands-on responsibilities unresolved. The same
item linkage and explicit confirmation provenance are required; unrelated,
unlinked or unconfirmed details do not supply context. Missing context is not
inability. Stronger existing role/history evidence and genuine conflicts retain
their existing explanations. No autonomy, duration or independently verified
competence is inferred from context.

This professional-background extension changes only explanatory wording and
its supporting profile references in display mode. Qualification status and
decision-bearing support remain unchanged, including when professional context
is supplied. It does not make a suppressed opportunity visible. Existing cards'
Qualifications & conditions disclosure and the normal job-detail comparison
sections render the explanation; no new route or admission exception is added.

The legacy scoring projection deliberately omits these explanatory records.
Pre-admission comparison calls also retain their previous inputs. The current
profile hash invalidates old recommendation contexts after confirmed changes;
there are no score bonuses, new hard gates or ranking rules.

The bounded contract accepts at most 16 detailed items and retains the existing
overall canonical size/provenance limits. No automatic suggestions are derived
from total career years, degrees, employers, extraction confidence or domain-year
totals. Item-specific duration is shown to the candidate but does not yet resolve
source year requirements. Activity details are preserved and reviewable; this
slice adds no new activity-requirement parser.

Focused tests: `tests.test_optional_item_experience`,
`tests.test_background_item_explanation` (in-memory matching and renderer checks;
not browser acceptance), plus the existing correction,
condition-comparison, language-safety and accepted-task tests. The opt-in
`tests.test_optional_item_experience_browser` uses the existing disposable local
login/app tooling and a separate headless Edge context. Set
`WAHOJOBS_BROWSER_NODE` to the local Node executable and `NODE_PATH` to its existing
Playwright package directory. It blocks external browser requests and scopes the
temporary certificate exception to that test context. Screenshots go to
`WAHOJOBS_BROWSER_OUTPUT` or temporary storage, never into Git.
