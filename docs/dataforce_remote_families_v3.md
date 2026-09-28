# DataForce remote contributor families V3

This extension closes an internal adapter coverage gap. The retained public
inventory contained paid remote contributor families that the old Thyme-only
reader inspected but could not admit. This document describes local validated
code. Installation and fresh production collection require their own receipts;
replaying the retained fixture does not establish current availability.

## Scope and retained evidence

The new individual-record contract is
`dataforce_remote_contributor_family_v3`. The existing exact Thyme V1 and
general Thyme V2 readers remain unchanged and continue validating their own
historical records.

| Family | Retained eligible records | Required work evidence |
| --- | ---: | --- |
| Existing Thyme V1 | 8 | Existing AI writing contract, unchanged |
| Cadence | 10 | Paid educational content evaluation by qualified educators for a multilingual learning application |
| Ronia | 8 | Compensated accepted RAW photos for AI and computer vision |
| Gardenia | 4 | Compensated accepted speech recordings for voice assistants |
| Triton | 1 | Compensated accepted French speech recordings for voice assistants |
| TTS casting | 1 | Professional voice casting for TTS model work, with compensation negotiated upon selection |

The fixture contains the original 53 public HTTP response bodies from the
September 24 controlled validation: three index pages with 54 cards and 50
detail responses. Original response SHA-256 values are preserved and checked
when tests load it. It is not a newly performed crawl.

- Journal plan: `ccff84160528b1e726170915822f83ea156307d49b59bcba0258fb6ff553594d`.
- Fixture: `tests/fixtures/dataforce_remote_families_v3.json.gz`.
- Fixture SHA-256: `56fce62bb7abdac026979bd777d8f34d785aa35e32a128d1f776568aeed8edd9`.
- Each fixture entry retains its URL, observation time, raw response hash and
  original journal filename. The gzip container uses a deterministic timestamp.

## Individual acceptance proof

V3 requires an exact current index card and its own linked detail. Family URL,
title, category, remote metadata, country, and external identity must agree.
Only the five evidenced contributor families above are recognized; unrelated
corporate roles, arbitrary new families, studies for minors, and onsite cards do
not gain authority.

The detail reader requires one canonical URL matching the index URL, one main
element, one visible role body, and one exact heading. It ignores hidden and
inert content, scripts, templates, and case-insensitive `aria-hidden` content.
Only the observed Ronia heading without the word “Remote” and the exact French
Gardenia heading have narrow aliases. A challenge, closed-application message,
onsite contradiction, absent paid remote work facts, or ambiguous role identity
rejects that record.

Ronia, Gardenia, Triton and TTS require the explicit adult eligibility statement
present in their retained English or French detail. Cadence requires qualified
middle/high-school educators with whole-class teaching experience; references to
their students aged 11–18 are not interpreted as applicant ages. Explicit
child-only or minor-applicant contradictions reject the record.

The visible application action must be in the role's single main element, use
the official TransPerfect hub, and have exactly one registration token with no
additional query or fragment. The 24 observed V3 identities have exact pinned
registration tokens. A new evidenced country/language variant in a supported
family may qualify from its own current card/detail, but cannot borrow another
known V3 role's application action. Changes to a pinned action fail closed.

The attestation binds exact identity, title, detail URL, family, application
URL, original index URL and response hash, exact card hash, and normalized
detail hash. Publication and historical semantic replay reconstruct the record
from the retained card and detail; asserted classification or metadata alone
does not grant acceptance. Source-run and original observation clocks keep the
existing pipeline rules.

TTS casting is `public_inventory_opportunity`, with public-page availability
and `include_in_live_market_estimate=False`. Its original description, including
“Sample submissions are not compensated” and the conditional project payment,
is retained. The other admitted families use the individual live-posting
classification supported by their current application and paid work evidence.

## Partial-source boundary

DataForce remains a partial source with no absence-based closure authority.
A failed detail does not erase successful sibling observations. An omitted or
failed record receives neither a new verification time nor a closure inferred
from the index. Exact unresolved identities remain in pending-qualification
telemetry; incomplete supported coverage also emits a qualification warning.

The retained Viola card is internally contradictory: its metadata says remote
while its title and retained detail identify onsite Bengali voice work in Pune.
It is not an admitted remote family and remains the one pending remote-labelled
identity after the 32 supported records qualify. The other 21 filtered cards
are outside this extension's remote adult contributor scope. This extension
does not rewrite those cards to fit the remote contract.

## Daily request budget

The retained full supported sweep consumes **35 HTTP transactions**: three index
pages plus all 32 supported detail pages. Supported records have priority over
exploratory cards; the old configured budget of 15 covers only 12 of these detail
pages after the three-page index, and truthfully reports the remaining work.

The policy ceiling becomes **70 HTTP transactions**, matching the unchanged
hard bounds of at most 20 index pages and 50 exact linked detail pages. Explicit
deployment configuration is required: `default_sources()` retains DataForce's
15-request default and existing disabled-by-default status for this scope.
The code's 360-second ceiling and the existing explicit production allocation
of 180 seconds are unchanged. With the other current source allocations held
constant, an explicit 70-request DataForce allocation makes the global HTTP
budget 583 instead of 528; the global execution ceiling stays 2,580 seconds.

This bounded policy can sweep all currently evidenced supported records daily.
If supported detail count grows beyond 50, or the operator chooses a smaller
budget, the source remains incomplete and pending records stay visible. It must
not claim a complete inventory or extend verification for unobserved records.

## Validation and activation

`tests/test_dataforce_remote_families.py` contains nine offline regression tests:
retained full sweep; real tracking/public admission and historical replay;
original clocks and no closure on partial omission; isolated detail failure;
smaller-budget accounting; unsupported/contradictory identities; exact
card/detail/application tampering; adult and visible-body eligibility; a new
evidenced family variant; and TTS casting semantics. Some methods exercise
multiple related cases. All nine passed locally; independent review also ran
two existing V1/V2 authority cases, for 11 passing tests.

Production activation must preserve the unchanged source clock/closure rules,
install the reviewed reader before publishing V3 records, explicitly apply the
70-request source allocation, and retain fresh collection/publication receipts.
An older application release without the V3 reader cannot replay V3 accepted
records; code-only rollback after V3 publication must therefore retain a reader
that understands this contract. No historical capture is deleted or rewritten.
