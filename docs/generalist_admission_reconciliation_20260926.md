# Generalist admission reconciliation — September 26, 2026

Prepared on release `2b5aeae3521da5f4a1e7d221f2cdfc08417e023a`, which includes
`4dc91650bfab99e8e526829cc1e31782fe7ab5d1`. This is a local, reviewed correction;
current host ancestry, Sam's saved-input reconciliation, deployment and hosted
verification remain pending fresh authorized access. Synthetic results below
are not findings about Sam. The prior 14-provider evaluation evidence is reused.

## Existing authority and demonstrated defects

`private_beta_readiness_v1.md` records both accepted product decisions: confirmed
non-AI activities may support generalist task relevance, and accepted beginner
scope with a relevant confirmed interest may establish a separate relevance basis.
Neither supplies specialist credentials or establishes all independent requirements.
The corrections below implement these decisions; no new product policy is assumed.

| Retained opportunity | Source evidence and defect | Corrected behavior |
| --- | --- | --- |
| Mercor Generalist Expert (5644 / 511) | Everyday prompts, grading model output and a professional or academic background in any field. A standing-pool availability notice was treated as a restrictive audience; task grammar missed plain model output; scope recognition required narrower entry wording. The generic-only quality penalty separately exempted prior AI work and beginner interest but omitted valid transferable activity. | Relevant confirmed review activity plus the complete broad scope can support conditional relevance. Interest alone cannot establish the any-field background/activity basis. Remove only the erroneous generic-only penalty when that source-bound fit exists. Reading, writing, background and other material conditions remain independently assessed. No prior AI employment is invented. |
| DataAnnotation Generalist (8228 / 2001) | Everyday topics/general reasoning, explicitly nontechnical scope, reviewing/rating AI responses and writing improved answers. Its actual-duty and applicant headings were missed, while related-role footer content could be read as current qualifications. No degree alone is insufficient evidence. | Complete nontechnical scope paired with actual current duties supports the accepted interest route or related confirmed activities. Reading, written English and assessment remain separate. Related openings supply neither duties nor prerequisites. |
| RWS Speech AI Evaluation Portuguese Brazil (7525 / 1599) | Guided scenarios and a broad entry audience; native-level Brazilian Portuguese and fluent or advanced English are required. AI/data capabilities are explicitly preferred. Unbulleted labeled conditions were merged, losing separate language/modality interpretation. | Preserve distinct required language conditions and preferred experience. Guided beginner scope with relevant interest can support the existing conditional path. Written review alone does not establish spoken-task competence. Fluent Portuguese does not establish native proficiency; unknown is distinct from explicit incompatibility. |

The retained full public source fixtures and material hashes are in
`tests/fixtures/generalist_admission/manifest.json`. Tests replace provider names
and identities; production logic contains no account, provider or posting IDs.
No source fetching, model calls, collection or backfill is involved.

## Boundaries and intentional result changes

Recognition uses complete affirmative source clauses paired with accepted duties
from the current role. Negated, historical, other-role and restrictive continuation
controls fail closed. Related-role sections terminate the current-role scope.
Condition parsing separates unindented labels only in the condition reader;
general scope parsing retains paragraph restrictions and wrapped lines.

The source-bound transferable exemption removes a contradictory quality penalty,
not its weight or any numerical section threshold. A retained Mercor row and a
minimal synthetic confirmed-review profile change from score 0 with a 10-point
generic-only penalty to score 4 with no such penalty. The existing task-supported
section path then permits conditional review. In the same three-case diagnostic,
DataAnnotation remains conditional at score 3 and RWS remains outside recommendations
at score 17 because that synthetic profile has only written-review activity.
Separately, a relevant confirmed-interest profile can use RWS's demonstrated
beginner path, still at score 17, with unknown native proficiency clearly conditional.
There is no 18-to-17 threshold change or target recommendation count.

Specialist, actual prior-experience, language, location, freshness, trust and typed
preference guards remain in force. Unconfirmed profile provenance is rejected at
the final binding step, including fresh and 73-hour source controls; a preliminary
score is not evidence of qualification. Existing account authority and current
saved/applied state remain separate from reusable catalog data.

Projection versions advance to task projection 7, source eligibility 6 and task
admission 13, invalidating obsolete derived recommendations through the established
signature. No candidate storage or history is reset.

## Validation and remaining operational gates

- 234 affected tests pass on Windows and isolated Linux, including positive and
  negative source, eligibility, recommendation reuse and exact-detail consumers.
- Independent review cleared the source corrections and the additional penalty
  correction; its final focused selection passed 69 tests. Restrictive continuation
  and related-role counterexamples found during review were fixed and retested.
- Two test-only repairs isolate mocked interpretations from legitimate saved-result
  reuse and forward the existing `personalized` argument in the detail observer.
  Both fixture defects were reproduced on the exact deployed baseline.
- Three paired isolated Linux processes used the same retained inventory and fixed
  clock. Beginner first-computation medians were 3.279 seconds baseline and 3.481
  seconds candidate; a second fresh account was 2.583 versus 2.886 seconds. Other
  uncached cases differed by at most 0.056 seconds in median. Maximum process RSS
  was 1,198,956 versus 1,198,508 KiB. These are impact checks, not hosted timings or
  a performance improvement. They preceded the final transferable penalty fix;
  that added admission path has a separately labeled final comparison.
- With the final correction, one additional paired process used a synthetic
  confirmed-review profile in two fresh accounts: first computations were
  3.581/3.241 seconds baseline and 3.507/2.717 seconds candidate. Repeats were
  0.004/0.006 versus 0.023/0.025 seconds; the candidate now renders supported
  recommendations. Maximum RSS was 1,199,420 versus 1,199,884 KiB. This bounded
  impact sample does not establish a hosted speedup or a capacity guarantee.

The old temporary firewall rule was removed and its original rule preserved.
Fresh private handoff, temporary narrow access approval and Sam sign-in are pending.
The relevant saved profile and reviewed input/source path must be inspected read-only
before classifying Sam's three results or making a profile-normalization change.
Do not equate an empty structured activity field with absent reviewed input.

Deployment remains gated on actual host ancestry, protected-state comparison,
scheduled-operation coordination, backup/rollback and hosted owner verification.
The September 26 06:00 UTC cycle requires actual execution evidence; it is not
established by this local work. Do not force a cycle or alter timers.

Previously reported first Matches computation around 12 seconds, Browse natural
expiry delay and unproven multi-user memory margin remain launch limitations.
If retained after validation, a limited invitation beta needs an explicit owner
acceptance. Physicist extraction remains unfinished; micro1 remains disabled.
This focused coverage does not establish that every downstream consumer is unaffected.
No invitations, production authentication, public domain/indexing, hosting upgrade
or real-beta storage changes are authorized by this correction.
