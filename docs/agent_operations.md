# AI Company Operations: A1 Domain Foundation

## Purpose and milestone boundary

AI company operations are intended to help Wahojobs observe operational health,
prepare internal briefings, propose bounded work, and eventually perform narrowly
authorized actions with a durable audit trail. Milestone A1 defines only the pure
domain language and deterministic safety policy needed for that future work.

A1 is dormant. It executes nothing. It has no model or provider integration, no
tool client or callback, no database or migration, no durable repository, no
route or command-line interface, no background scheduler or autonomous loop, and
no normal-runtime activation. Importing the domain creates no agent, task,
approval, execution, or audit record. The package root does not export it, and
the product, matching, pipeline, crawler, account, ownership, profile, session,
and Greenhouse runtimes do not import it.

An A1 `allow_*` result is a pure policy classification. It is not executable
authority and cannot invoke a model, tool, external service, or product mutation.

## Agent identities and company functions

`AgentDefinition` is immutable and contains only bounded operational
configuration: a stable `agt_` identifier, closed agent kind, closed company
function and environment, lifecycle, policy version, risk ceiling, exact granted
capabilities, allowed data classifications, execution budget, and closed human
escalation target. It cannot hold credentials, provider objects, database paths,
tool clients, or callbacks.

Every granted capability must belong to the configured company function and
have a minimum risk no greater than the agent risk ceiling. Impossible static
configurations are rejected when the agent definition is created.

The A1 agent kinds are:

- `chief_of_staff`
- `company_operations`
- `data_operations`
- `seo_content`
- `product_operations`
- `customer_support`
- `b2b_sales`
- `engineering_operations`
- `finance_operations`

The company functions are company operations, data operations, SEO and content,
product operations, customer support, B2B sales, engineering operations, and
finance operations. Development, test, staging, and production are the only
environment values. Agent lifecycle is `active`, `suspended`, or `retired`; only
an active agent can receive an executable policy classification.

## Capability model

One immutable taxonomy defines every capability. Arbitrary capability strings,
wildcards, `superuser`, `all_access`, and administrator capabilities do not
exist. Each capability specification fixes its company function, action
category, minimum risk, maximum data classification, always-approval flag,
autonomous-prohibition flag, and documentation-safe meaning.

The taxonomy covers bounded reads of inventory, source health, matching quality,
build state, product and SEO metrics, support and B2B summaries; internal
briefings, proposals, deterministic analysis and tests, drafts, and draft pull
requests. Future side-effect capabilities are named individually for metadata
changes, archival, publishing, messages, source changes, deployment, pricing,
billing, finance, legal acts, secret access, user-data deletion, and ownership
mutation. Naming a future capability does not enable it.

## Data classifications

The closed classification order is `public`, `internal`, `confidential`, and
`restricted`.

- Public covers published pages and public listings.
- Internal covers aggregate operational facts and internal task state.
- Confidential covers individual support material, unpublished strategy, and
  commercial leads.
- Restricted covers credentials, financial access, tax records, session
  credentials, and raw private user data.

Policy evaluation computes one effective requirement from the task, intent, and
every evidence reference, then compares the highest classification with the
agent allowance, capability maximum, operation maximum, and A1 restriction.
Approval cannot expand that allowance. A1 agent definitions cannot receive
`restricted` access. Evidence references cannot represent restricted source
content.

## Risk and action categories

The closed risk order is `low`, `medium`, `high`, and `critical`. A calculated
risk can never fall below the capability floor or exceed the agent ceiling.

- Low covers read-only analysis, internal summaries, deterministic tests, and
  internal drafts.
- Medium covers reversible internal changes and bounded publication proposals.
- High covers external communication, source merges, deployment, pricing,
  irreversible or large-scale changes, and requires a rollback plan when the
  proposed action is reversible.
- Critical covers money movement, billing-account mutation, tax or legal
  execution, secret access, ownership mutation, and private user-data deletion.
  Critical execution is prohibited in A1.

Action categories are `observe`, `analyze`, `draft`, `propose`, `test`,
`modify_internal`, `publish`, `communicate_external`, `deploy`, `financial`,
`legal`, `security_sensitive`, and `delete_data`. Observe, analyze, draft,
propose, and test are explicitly separate from side effects. A draft is never a
sent message, and a proposal is never an executed action. Read-only argument
summaries containing side-effect intent are rejected.

## Task lifecycle

`AgentTask` is an immutable, bounded task proposal with a stable `atk_` ID,
closed task kind and lifecycle, exact capability, function and environment,
classification, risk, canonical timestamps, creator kind, idempotency key, concise objective,
privacy-safe evidence references, policy version, approval requirement, budget,
and revision number. It has no arbitrary prompt or hidden reasoning field.

Ordinary construction always creates the initial `proposed` state. Approved,
running, handoff, denial, expiry, cancellation, and terminal states can only be
created by the authoritative transition function. Approval-required proposals
therefore cannot be constructed directly as approved or running.
Completed task values carry no reusable transition permission; every later
state requires a newly validated `transition_task` operation.

The lifecycle is `proposed`, `awaiting_approval`, `approved`, `running`,
`needs_human_input`, `succeeded`, `failed`, `cancelled`, `expired`, or
`policy_denied`. Exact transitions are immutable:

- A proposal may become approved only when approval is not required.
- A proposal requiring a human moves to awaiting approval.
- Awaiting approval becomes approved only with a trusted receipt bound to the
  exact task and revision.
- Approved becomes running.
- Running becomes succeeded, failed, needs human input, or cancelled.
- Needs-human-input may return only to the approval boundary or a terminal
  denial, cancellation, or expiry.
- Succeeded, failed, cancelled, expired, and policy-denied tasks are terminal.

Expired tasks cannot start or receive an executable policy result. A denied task
requires a new task or revision; it cannot be revived.

## Trusted human approval

`TrustedHumanApproval` is a sealed immutable receipt with a stable `aap_` ID. It
contains only the task ID and revision, complete task-proposal fingerprint,
environment, exact approval scope, approved capabilities and risk ceiling,
ordered tool-intent fingerprints and bundle fingerprint, canonical approval and
expiry times, and policy version. It retains no email address,
password, browser cookie, session credential, profile, or approval prose.

Direct, dictionary, replacement, copy, subclass, duck-type, and pickle forgery
are rejected. Trust is bound to the exact instance created by the private test
issuer; copying approval fields, issuance stamps, or seals to another instance
does not transfer validity. Unissued or reconstructed approvals are denied as
`invalid_approval`. There is no public serialization and representations are
redacted. A string such as `yes` has no authority. A1 provides no normal-runtime approval
issuer; focused tests use a test-only issuer to exercise the boundary.

Validity is intrinsic and uses an immutable, issuer-attested instance binding
and a complete payload seal. The issuer capability is not stored on the
approval. No mutable set, dictionary, weak registry, object-ID table, issued
object list, or registration side effect participates in approval validity.

An approval cannot add an agent capability, lower risk, exceed its own risk
ceiling, apply to another task revision, authorize an unlisted intent
fingerprint, survive expiry, or remain valid after an execution-relevant intent
change. Intent order and cardinality are approval-relevant. The complete task
fingerprint covers environment, objective, evidence order, expiry, budget,
risk, classification, policy version, approval requirement, and revision, so a
change to any of them requires a new approval.

## Proposed tool intents

`AgentToolIntent` is an immutable proposal with a stable `ati_` ID. It records an
exact capability, closed tool kind, closed operation, environment and proposal
time, action category, classification, risk, idempotency key, sanitized argument
summary and argument fingerprint, expected side-effect class, reversibility and rollback summary,
privacy-safe evidence references, bounded cost and effect estimates, and a
timeout. It cannot hold a live client, callback, raw secret, or full confidential
payload.

Tool kinds are internal metrics, job inventory, crawler control, content
management, support drafts, B2B CRM, source control, test runner, deployment,
billing, finance, and communications. The taxonomy fixes which tool kinds and
side-effect class can represent each capability. One immutable operation
taxonomy also fixes the capability set, tool-kind set, category, effect class,
risk floor, classification maximum, reversibility, rollback requirement,
approval rule, A1 prohibition, and allowed environments. Crawler start and stop
are side-effectful operations and cannot be represented by a read-only source
health intent. Every execution-relevant field participates in the canonical
intent fingerprint.

The closed side-effect vocabulary distinguishes no effect, internal draft,
test execution, internal state change, publication, external message, source
control change, deployment, billing or pricing, financial transaction, legal
action, security-sensitive access, user-data deletion, and ownership mutation.
External-message, modified-record, and publication estimates must agree exactly
with the operation and effect class. Drafts and proposals cannot report
completed external effects.

## Deterministic policy decisions

`evaluate_agent_action` is a pure function returning one of
`allow_read_only`, `allow_approved_execution`, `require_human_approval`, `deny`,
or `prohibit`. One explicit canonical whole-second UTC `observed_at` value is
mandatory; policy never reads the host clock and never substitutes task or
budget time. It evaluates exact object type, lifecycle, environment and
function configuration, capability grant, data access, risk floor and ceiling,
task state and expiry, policy version, sealed approval and fingerprint binding,
and every budget dimension.

Agent, task, intent, operation, and approval environments must be coherent.
Production read-only observations are eligible only for operations whose
specification explicitly allows production; A1 side-effect execution is not.
Budget state, when supplied, must use the same observation time.

Low-risk reads, deterministic analysis and tests, internal drafts, proposals,
and briefing creation can be eligible for `allow_read_only`. Medium and high
side effects require an exact, current human receipt. Missing capability,
excessive classification, inactive agent, invalid lifecycle, mismatched or
expired receipt, and exhausted budget are denied. Critical and explicitly
prohibited A1 capabilities remain `prohibit` even when an ordinary approval is
present.

No decision contains secret material, raw evidence, private user data, a model
prompt, or hidden reasoning. A decision never calls a tool.

## Budgets, idempotency, and replay

Immutable budgets bound tool intents, attempts, concurrent tasks, cost units,
external messages, modified records, content publications, and runtime seconds.
Negative values, booleans used as integers, floats, nonfinite values, overflow,
and unbounded limits are rejected. Task budgets cannot expand the agent budget.
The default A1 budget permits no external message, modified record, or content
publication.

Task proposals, tool intents, approvals, execution attempts, and audit events
use compact canonical JSON, explicit domain separation and versioning, and
lowercase SHA-256. Generated IDs are excluded from replay identity. Replay
comparison uses constant-time digest comparison and distinguishes exact replay,
same-scope conflict, and a distinct request scope. A1 has no durable replay
repository.

## Evidence, audit, and human escalation

`EvidenceReference` stores only a closed evidence kind, closed source system,
classification, canonical capture and freshness times, a content fingerprint,
and a strict source-specific ASCII locator consisting only of a closed prefix
and bounded numeric ordinal. It cannot store a source document, raw message,
credential, private identity, database location, query, or token.

Evidence capture cannot postdate its containing task or intent proposal. At the
mandatory observation time, evidence must already exist and remain fresh;
freshness-boundary equality is stale. Approval cannot predate its task or any
approved intent.

`AgentAuditEvent` is an immutable privacy-safe event with a stable `aev_` ID,
task and agent binding, closed kind, canonical timestamp, policy version,
previous-event fingerprint, concise decision summary, capability, policy
decision, risk, and evidence references. Event fingerprints omit generated event
IDs and bind all semantic and chain fields. Pure validation checks the root,
ordering, task binding, canonical hash, and every previous-hash edge; gaps,
reordering, mutation, malformed hashes, and unsupported kinds fail closed.

The audit model explicitly has no `chain_of_thought`, `reasoning_trace`,
`scratchpad`, `hidden_prompt`, or `raw_model_context`, and it has no other
hidden-reasoning or raw-evidence content.
Canonical serialization recursively normalizes field names and rejects aliases
of chain of thought, reasoning trace, scratchpad, hidden prompt, raw model
context, internal monologue, private reasoning, raw evidence, evidence payload,
source payload, and raw content. Only a concise safe decision summary and cited
evidence references are permitted. Sanitized domain errors contain stable public
codes and messages and detach rejected input from cause and context.

`HumanEscalationRequest` carries a stable ID, task and agent binding, closed
reason and required-decision type, risk, concise safe context, evidence
references, bounded suggested options, and expiry. Reasons cover required
approval, evidence conflict, missing data, policy denial, budget exhaustion,
irreversibility, legal or financial action, security sensitivity, customer harm,
and unexpected system state. Escalations contain no hidden reasoning.

## Initial A1 safety policy

Potentially eligible without approval are privacy-safe read-only summaries,
deterministic analysis and tests, internal drafts, bounded proposals, and daily
briefings.

The taxonomy always requires human approval for metadata changes, archival,
publication, external communication, source merges, deployment, pricing, and
other side effects. In addition, A1 keeps external-message, deployment, and
billing-or-pricing execution prohibited until a later milestone supplies an
explicit bounded enablement. Their always-approval classification does not
override that A1 prohibition.

Financial transactions, billing mutation, tax filing, contract signature,
secret access, account ownership mutation, and private user-data deletion are
prohibited. Authentication or authorization bypass, disabled auditing,
self-modified permissions, broader-agent creation, approval-evidence alteration,
and self-modified budgets or risk ceilings have no capability representation and
cannot be proposed as executable intents. A prohibited action may be represented
for human awareness, but policy remains `prohibit` and produces no executable
authorization.

## Future milestones

A2 may add a durable task, approval, execution-attempt, idempotency, and audit
schema behind a separate integration boundary. A3 may add a read-only Daily
Company Briefing Agent using privacy-safe aggregate evidence. Later milestones
may add bounded data, content, support, sales, engineering, finance, and Chief of
Staff operations only with explicit tool adapters, approval issuers, runtime
activation, and defensive validation that are outside A1.
