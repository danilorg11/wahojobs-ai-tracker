# AI Profile Intake v1: document, model, and review-draft foundation

This boundary performs deterministic, in-memory text extraction for a
text-based resume/CV supplied as PDF or DOCX. It then exposes the text only as
ordered, bounded evidence blocks (`b001`, `b002`, and so on). A deterministic
minimizer creates a structurally distinct model-safe packet, which may be sent
through the isolated profile-extraction adapter and transformed into an
ephemeral review draft.

The browser runtime is available only inside the explicitly composed,
authenticated new-product staging application. It is not registered in the
legacy/public runtime and does **not** create a production cutover. It accepts a
bounded upload and retains a short-lived review draft, but does not persist a
profile, document, evidence, extraction, or durable draft, create durable
provenance, check an entitlement, or change matching. `Canonical Profile V2`
remains the sole durable profile authority.

## Supported and unsupported documents

- Supported: text-based PDF and standard macro-free DOCX.
- PDF extraction preserves page order and rejects encrypted, malformed,
  over-limit, and effectively text-free/image-only files.
- DOCX extraction includes headers, body paragraphs, tables, and footers after
  a hostile-ZIP preflight.
- Unsupported: OCR, scanned/image-only PDFs, legacy DOC, DOCM/macros, encrypted
  containers, external relationships, and other document formats.
- The original filename is neither required nor trusted as an identifier.

## V1 limits

The defaults are centralized in `DocumentLimits`:

- upload bytes: 10 MiB;
- PDF pages: 50;
- normalized extracted characters: 100,000;
- evidence blocks: 128, with 2,000 characters per block;
- DOCX ZIP members: 256;
- cumulative DOCX uncompressed data: 50 MiB;
- individual DOCX member: 20 MiB;
- DOCX compression ratio: 100:1;
- relationship XML: 1 MiB per part.

Limit violations and parser failures raise `ProfileIntakeError` with a stable,
content-free code. Diagnostics are restricted to safe structural values such
as byte, page, character, or member counts.

## Hostile-input and retention model

PDF and DOCX bytes are untrusted. Format magic and parser structure are checked
instead of trusting an extension or content type. DOCX preflight rejects path
traversal, duplicate/encrypted members, unsupported compression, excessive
member/count/size/ratio values, macros, malformed package structure, and any
external relationship or DTD/entity-bearing XML before `python-docx` parses the
package.

Extraction uses in-memory streams and creates no temporary files. The returned
document contains structural metadata and evidence blocks, not the original
binary or a caller filename. This package does not log document bytes, text,
evidence, email addresses, phone numbers, or model-shaped output.

Evidence remains untrusted data. Prompt-like instructions inside a resume are
preserved as evidence rather than executed or interpreted. Evidence must remain
ephemeral and must not be copied into durable Canonical V2 provenance.

## Raw evidence and model-safe evidence

`EvidencePacket` is the raw text-extraction result. It is not accepted by the
external-model adapter. `minimize_evidence_packet` produces the separate
`ModelEvidencePacket` type, and the real and fake profile adapters require that
exact type. Before a request, the real adapter also reruns the deterministic
check and rejects a manually mislabeled packet that still contains detectable
PII. No document binary can be represented at this boundary.

Minimization is deterministic and intentionally narrow. It removes detectable
email addresses, domestic and international phone numbers, clearly labeled or
street-shaped postal addresses, labeled dates of birth and age statements,
social/profile URLs, and obvious contact-only lines. Query parameters and
fragments are removed from retained professional/portfolio URLs. It preserves
job titles, employers in evidence text, universities, skills, professional
dates, languages, professional URLs, and useful city/region/country evidence.
It does not infer sensitive attributes and is not a general-purpose anonymizer;
unusual or ambiguous PII can remain and callers must treat minimized text as
sensitive ephemeral data.

Original server-issued block references are never renumbered. A block emptied
by minimization is omitted and recorded in `removed_block_references`, so a
packet can contain `b001`, `b003` without relabeling `b003`. The model-safe
packet enforces the same 128-block, 2,000-character-per-block, and 100,000-total-
character ceilings as raw evidence.

## External-model boundary

`OpenAIProfileExtractionAdapter` follows the repository's existing Responses
API convention using the existing `requests` dependency. The model is
configurable with `WAHOJOBS_OPENAI_PROFILE_MODEL` (default `gpt-5-mini`), and
the API key uses `OPENAI_API_KEY`. The request has a bounded timeout, no tools,
no retrieval, no retry loop, and provider storage is explicitly disabled with
`store: false`. Prompts, model-safe evidence, response bodies, and resume text
are never logged.

Only a compact JSON serialization of `ModelEvidencePacket` crosses the model
boundary. The profile prompt and schema are isolated from opportunity
enrichment. They are versioned as `ai_profile_extraction_prompt_v2`,
`model_evidence_packet_v1`, and `ai_profile_extraction_v1`.

Resume text is declared untrusted data. Instructions inside it—including
requests to ignore instructions, reveal secrets, browse, use tools, access
files, or call a network—remain data. The prompt forbids sensitive-attribute
inference; inference of language from name/location/nationality; inference of
current residence from old jobs; inference of missing credentials from
omission; and conversion of historical behavior into present preferences.
Every fact must cite supplied evidence. Taxonomy, normalization, and calculated
facts must be marked inferred rather than explicit. The prompt's enum values and
explicitness requirements are generated from the authoritative local field
specifications so model instructions cannot drift from local validation.

## Structured output and local validation

The OpenAI request uses strict Structured Outputs with a compact, provider-
supported structural schema. It closes object shapes and constrains document
references, evidence aliases, Canonical V2 field paths, and JSON value shapes
without duplicating the detailed business contract in the provider schema.
Structured Output is not considered a trust boundary: every response also
passes the local `ai_profile_extraction_v1` validator, which remains authoritative
for field/path coupling, bounds, cardinality, uniqueness, and policy rules.

The strict validator rejects unknown fields and keys, missing evidence,
non-finite values, invalid enums and types, oversized facts, inferred sensitive
location/preferences, and any attempt to supply account, principal, profile,
revision, durable-source, provenance, entitlement, or matcher-signal authority.
Classification paths are required to use `explicit: false`. Provider failures,
refusals, timeouts, malformed output, and local contract rejection map to stable
content-free intake error codes. Before validation, the OpenAI adapter
conservatively demotes `explicit: true` to `false` only for inference-only paths
derived from the authoritative field specifications. It never promotes an
explicit-only field or rewrites fields whose explicitness may legitimately vary.

Content-free diagnostics remain separate from profile data. They may include
the configured model, prompt/schema versions, input/output token counts,
request duration, a validated provider request ID, HTTP status, success/failure
code, and a cost estimate only for known model pricing. This slice creates no
usage table.

## Ephemeral review draft and confirmation policy

`build_profile_review_draft` is a pure in-memory transformation of a validated
extraction. It reuses Canonical V2 paths and current review-form field names; it
does not create another canonical schema. The draft separates document-explicit
prefills from suggestions, lists missing user-only fields, retains evidence
references, and surfaces low-confidence ambiguity or conflicting language
details. It has no profile/revision/source IDs, durable provenance, entitlement,
repository dependency, matcher signals, or write path.

Literal titles, degrees, institutions, explicitly listed skills and languages,
and explicitly stated current location may be prefilled. Seniority, domain and
occupation taxonomies, inferred specialties or skill normalization, and total
experience calculations are suggestions requiring confirmation. All preference
facts are review suggestions even when explicit. Availability, schedule, phone,
employment and task preferences, work authorization/eligible countries, and
hard/soft/avoid constraints remain user-input fields. An explicitly stated
present remote/flexible preference can be shown for confirmation; old remote
work cannot silently become a present preference.

Canonical V2 currently has no employer fact path, so employer names are
preserved in minimized evidence but are not invented as a new profile field in
this slice. A later canonical-model decision would be required to add one.

## Authenticated browser flow

The optional “Create your profile faster” entry appears beside the existing
manual create-profile path only when `ProfileIntakeBrowserIntegration` is
explicitly attached. The accepted flow is:

1. revalidate the durable session, exact account, account-native principal,
   current PB-OWN-1 ownership lineage, and environment namespace;
2. validate trusted host and same-origin headers plus a purpose-specific CSRF
   proof;
3. parse one resume/CV, one LinkedIn export, or one of each;
4. independently extract evidence, minimize PII, invoke the configured adapter,
   and locally validate each supplied source outside any database transaction;
5. deterministically reconcile all validated sources into one atomic review
   draft;
6. issue one opaque server-generated draft handle and redirect with HTTP 303;
7. revalidate the same authorities for every review read, update, or cancel.

Account, principal, session, ownership-event, profile, revision, or source
authority is never accepted from a form or query string. The runtime reuses the
existing durable browser authentication and read-authorization gateways and
captures the same exact PB-OWN-1 lineage used by create-once profile authority.

## Multipart and document-origin boundary

The maintained `python-multipart` parser is used as the bounded
multipart mechanism; the application does not implement a general parser. An
available `Content-Length` above two 10 MiB document limits plus 64 KiB of
strict multipart overhead is rejected before reading. Each document remains
independently limited to 10 MiB. The body is then read in
bounded 64 KiB chunks with an independent hard ceiling. Transfer encoding,
malformed boundaries or headers, duplicate or unexpected parts, duplicate
headers, oversized metadata, more than one document in either role, more than
two documents overall, and incomplete bodies are rejected. The original
filename is ignored and never retained or logged.

Declared MIME type is not trusted: it must agree with the detected PDF or DOCX
container before extraction. `resume` accepts PDF or DOCX.
`linkedin_profile_export` records only honest ephemeral importer metadata and
accepts PDF only. It is not a Canonical V2 field, does not accept a LinkedIn
URL, and performs no scraping or LinkedIn API call. Valid bundles are resume
only, LinkedIn PDF only, or resume plus LinkedIn PDF. Zero-file submissions,
duplicate roles, and unknown roles fail closed.

## Bundle processing and deterministic reconciliation

One browser generation is one importer bundle regardless of whether it contains
one source or two. Each source receives a separate server-generated opaque
`doc_*` reference and independently passes deterministic parsing, raw
`EvidencePacket` creation, PII minimization, model-safe `ModelEvidencePacket`
creation, adapter extraction, and strict local response validation. The model
never receives a concatenated bundle and is never asked to choose which source
is more trustworthy. A combined import may therefore make two bounded adapter
requests while remaining one user-visible import attempt.

Only after every supplied source succeeds does the pure reconciliation layer
create a review draft. Any parse, minimization, adapter, validation, or
reconciliation failure aborts the bundle and creates no partial draft. Neither
upload order nor document kind gives a fact precedence.

Reconciliation groups facts by existing Canonical V2 review concepts. Equal
normalized singleton or list values are deduplicated, with scoped source and
evidence support merged. Explicit evidence wins only the presentation policy
for the same value: an explicit fact plus the same inferred fact becomes one
explicitly supported review fact. Complementary list values are unioned.
Different singleton values become confirmation-required conflict alternatives;
different qualifiers for the same language become a language-detail conflict.
The review user may accept at most one alternative in a conflict group, edit
the chosen value, or reject all alternatives. Historical behavior still cannot
create a present preference, and sensitive-trait inference remains forbidden.

Evidence block references are scoped by their source document reference, so
`b001` in a resume and `b001` in a LinkedIn PDF are distinct internally. The UI
renders only user-friendly source labels—resume, LinkedIn profile, both, or
sources disagree—and never exposes opaque document references. No evidence text
is retained for review. This source-aware ephemeral shape is versioned as
`ai_profile_review_draft_v2`; the independent model-output contract remains
`ai_profile_extraction_v1`.

## Process-local draft vault

`IntakeDraftVault` is non-durable, process-local, and bounded to 64 live drafts
with a ten-minute TTL. Opaque identifiers contain 256 bits of server-generated
randomness. A process-local in-flight guard prevents concurrent generation for
one lineage, while the durable reservation prevents a second M010-backed flow
for the same account from replacing the first.

Every record is bound to account, browser session, environment, account-native
principal, exact ownership binding/version/latest-event/lineage digest, and
the dedicated `ai_profile_intake_review_v1` purpose. Unknown, malformed,
expired, cross-account, cross-session, cross-principal, cross-environment, or
lineage-mismatched access returns no record. Review mutations use an expected
version and purpose-specific CSRF proof, so stale or replayed edits fail.

The vault retains one entry per bundle containing only the editable reconciled
review draft, safe metadata for at most two documents (opaque reference, origin,
format, byte/page counts, parser name/version), content-free per-request model
diagnostics when available, timestamps, the mutation version, and—in the Slice
4B composition—the sealed server-only attempt/reservation capability plus safe
source metadata needed by the atomic core. It does not retain upload bytes, an
`EvidencePacket`, a `ModelEvidencePacket` or its text, unminimized contact PII,
prompt text, raw provider output, original filename, durable
profile/revision/source IDs, editable entitlement state, or matcher signals.
After acknowledged success, the review and document metadata are discarded and
only a short-lived content-free request digest remains for safe PRG replay.

## Review and browser behavior

The private review page distinguishes document-supported prefills,
confirmation-required suggestions, source disagreements, and fields the user
still needs to provide. Facts may be labeled as found in the resume, LinkedIn
profile, or both without displaying internal source IDs.
It does not display internal confidence numbers or turn old job behavior into
present preferences. The user can edit or remove prefills, accept/reject or
edit suggestions, and enter bounded values for existing review fields. All
values, decisions, field counts, enum values, types, lengths, expected version,
and form shape are checked server-side. Browser indexes select only fields
already present in the bound draft; they cannot select authority or arbitrary
Canonical paths.

Ordinary edits update only the process-local draft. In an M010-capable composed
runtime, the explicit final Save described under Slice 4B is the sole durable
transition; it revalidates the complete review and calls the Slice 4A atomic
authority. Cancellation removes the bound draft, releases its active
reservation where practical, and returns to profile creation. Expired drafts
show a clear start-again response and do not consume the entitlement.

Private responses use `Cache-Control: no-store`, noindex/nofollow, the existing
closed CSP, `nosniff`, and a no-referrer or same-origin referrer policy. Stable
user-facing messages distinguish malformed/oversized/unsupported/encrypted or
text-free documents, unavailable extraction, expiry, and stale review state
without exposing provider responses, document snippets, filenames, credentials,
or authority identifiers.

## Adapter activation and test guarantees

The staging composition keeps extraction disabled unless
`WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED=1` is explicitly present. When enabled,
it uses the existing Slice 2 `OPENAI_API_KEY` and
`WAHOJOBS_OPENAI_PROFILE_MODEL` configuration. Missing or failed real extraction
never falls back to fake output; the browser presents a safe actionable error
and keeps the manual path available.

Ordinary tests inject a deterministic adapter and make zero real OpenAI or
network calls. No API key is required and committed fixtures contain only
synthetic, non-personal data. Slice 4B exercises durable Save only against
ephemeral M010 test databases. No matching behavior, public route, deployment
change, DNS, proxy, Vercel, DigitalOcean, WorkOS authority change, or old-site
cutover is introduced.

Entitlement accounting counts the importer bundle—resume only, LinkedIn only,
or resume plus LinkedIn—as exactly one import attempt. Internal
adapter request count and advisory usage/cost diagnostics must never determine
authorization, entitlement consumption, billing, or profile correctness.

## Slice 4A durable commit authority

Migration `010_ai_profile_import` adds only the dormant durable core needed by
a later final-confirmation browser slice. It widens the existing profile-source
vocabulary with `user_confirmed_ai_import`, then adds one entitlement table and
one content-free attempt table. It creates no durable draft, changes no
Canonical V2 field or matching rule, and adds no route. The callable migration
requires an exact M009 prerequisite, runs as one SQLite transaction, preserves
existing profile/source rows, verifies foreign keys and exact closed-schema
attestation before commit, and is not run automatically by an import or app
startup.

The durable AI source is still an existing `product_profile_sources` record,
not a parallel provenance system. Its strict JSON content has exactly eight
fields: source schema version, ordered bundle origins, document count, ordered
parser versions, model identifier, prompt version, extraction schema version,
and review schema version. The source records user-confirmed AI-assisted import
provenance; it does not claim that Wahojobs independently verified a fact. Raw
documents, filenames, text, evidence, contact data, prompts, raw model output,
confidence, model usage, and ordinary document hashes are prohibited. Both the
domain constructor and database trigger close the object to this exact bounded
shape.

### One-free-import entitlement and attempt

The stable V1 entitlement code is `ai_profile_import_v1`. Its sole owner key is
`(environment_namespace, canonical account_id, entitlement_code)`. It is not
keyed by email, provider subject, session, principal, profile, document count,
or model-call count. The entitlement has three states: `available`, `reserved`,
and `consumed`. It is established lazily on the first eligible reservation.
Only a successful user-confirmed initial profile commit consumes it; manual
profile creation never reads or creates this entitlement.

Each reservation creates one durable attempt and one cryptographically random
`aip_*` attempt ID plus `air_*` reservation ID. Resume-only, LinkedIn-only, and
resume-plus-LinkedIn bundles each create exactly one attempt. The combined
bundle can have two internal adapter requests, but model calls and tokens are
absent from entitlement accounting. The attempt retains only hashed
idempotency authority, a request fingerprint, safe bundle/version metadata,
authorized principal and exact PB-OWN-1 lineage facts, state/timestamps, and a
content-free result code. A successful attempt additionally retains its result
profile/revision IDs for exact replay. Those receipt identifiers are bounded,
non-owning references, so they do not block the existing profile privacy-purge
path; the consumed account entitlement remains durable.

The centralized reservation lease is twelve minutes, modestly longer than the
ten-minute process-local review TTL. `BEGIN IMMEDIATE` serializes competing
reservations. An exact retry reuses the same attempt/reservation. A different
active attempt receives a stable reserved response. Expiry deterministically
marks the old attempt expired and restores availability before a new operation
can reserve it. Explicit processing-failure, cancellation, draft-expiry, and
abandonment release paths restore availability without consuming the import.
No database transaction remains open while documents are parsed or a model is
called.

### Confirmed review mapping and atomic creation

`prepare_confirmed_ai_profile_import` is the pure boundary from an already
edited `ai_profile_review_draft_v2` to identity-free, user-confirmed Canonical
V1 material used by the repository's existing deterministic V1-to-V2 path. It
revalidates review values and decisions server-side, excludes removed/rejected
facts, rejects pending decisions and multiply accepted conflicts, and requires
the source origins to match the sealed bundle metadata. User-only fields come
from the confirmed review inputs. The adapter cannot provide durable IDs,
source authority, or matcher signals. Normalized skills/domains and all derived
matcher signals are recomputed by the existing server normalizer and Canonical
V2 conversion.

`commit_confirmed_ai_profile_import` accepts only a sealed current intake grant,
sealed reservation, and sealed confirmed artifact. Immediately before writing,
it revalidates the active browser session, canonical account, environment,
account-native principal, and exact current PB-OWN-1 lineage. In one outer
`BEGIN IMMEDIATE` transaction it validates the live reservation, calls the
existing account-native create-once repository through its nested-savepoint
path, marks the attempt successful, transitions the entitlement to consumed,
and records the replay result. Thus profile creation and entitlement
consumption commit or roll back together.

An exact post-success retry returns the original profile/revision result and
creates no row. A different attempt after consumption fails. If manual
create-once wins first, the AI attempt becomes a content-free failed attempt,
the entitlement returns to available, and no AI source is written. If AI
creation wins, the unchanged manual create-once path rejects a second profile.
Fault boundaries before, during, and after profile creation, during attempt
result update, after entitlement transition, and immediately before commit are
tested to roll back the whole final transaction.

Slice 4A deliberately did not connect the review page to reservation or final
save. Slice 4B supplies that browser binding as described below. Neither slice
performs a migration against a persistent environment, makes a real OpenAI
request during ordinary validation, implements billing or paid re-import, or
adds a durable AI correction/update path.

## Slice 4B authenticated final confirmation

The authenticated intake page now performs a read-only durable eligibility
preflight before document parsing or model work. The preflight attests M010,
revalidates the trusted session/account/principal/PB-OWN-1 grant, checks for an
existing profile, and reads any existing free-import state. It creates neither
an entitlement nor an attempt and is only advisory: the later serialized
reservation remains authoritative. An M009 or otherwise unsupported database
returns a safe unavailable result. No browser request or application startup
installs M010.

Documents still pass independently through extraction, PII minimization,
adapter extraction, strict local validation, and deterministic reconciliation
with no database transaction held. Only after a complete valid review draft
exists does the runtime acquire one Slice 4A attempt/reservation for the whole
bundle. Resume-only, LinkedIn-only, and resume-plus-LinkedIn therefore each use
one entitlement attempt regardless of internal adapter calls or tokens. A
generation or reconciliation failure creates no reservation. If another
session wins the reservation race, the losing generated draft is discarded and
never becomes browser-accessible.

The process-local vault retains the opaque durable reservation capability and
the already-approved safe source metadata entirely server-side. Attempt and
reservation IDs never enter a URL, form, HTML, editable field, or user-visible
error. Effective review lifetime is the smaller of the normal ten-minute vault
TTL and the actual twelve-minute reservation lease minus a conservative safety
margin; a draft with less than one minute of safe review time is never issued.
Cancellation releases the reservation without consumption. A targeted expired
draft is released where practical, while correctness never depends on cleanup
because the durable lease is independently reclaimable. Process loss creates
no durable draft recovery and cannot consume the entitlement.

The review form's final action is **Save profile and find matches**. It uses a
save-specific CSRF proof, same-origin enforcement, the opaque draft handle, and
the exact optimistic review version. All facts, suggestions, conflicts, and
user-only inputs are revalidated server-side. Suggestions initially remain
`pending`; Save cannot silently accept them. Rejected/removed facts are
excluded, conflicting alternatives must be explicitly resolved or rejected,
and matcher signals are recomputed by the existing canonical normalization
path. Browser fields cannot choose account, environment, principal, ownership
lineage, attempt, reservation, entitlement, profile/revision/source IDs, or
matcher signals.

Finalization calls `prepare_confirmed_ai_profile_import` and the existing Slice
4A `commit_confirmed_ai_profile_import` authority. One outer transaction owns
the existing Canonical V2 create-once write, `user_confirmed_ai_import` source,
successful attempt receipt, and entitlement consumption. A successful POST
returns HTTP 303 to the ordinary authenticated `/find-matches` route; it does
not invoke a separate AI matcher.

If the database commit succeeds but the response is lost, the vault keeps the
same sealed review and confirmation fingerprint long enough for an exact retry
to invoke Slice 4A's durable replay. Changed review content cannot reuse that
success. Once success is acknowledged, the full review is replaced with a
short-lived content-free request-digest receipt so refresh/retry remains safe
without retaining profile content. Loss of all process-local state after a
durable success does not damage the profile; ordinary account/profile and
matches navigation recover from the database.

An existing Canonical V2 profile makes initial AI import ineligible. A manual
profile that wins after an AI draft is generated is preserved, the AI source is
not written, the free entitlement remains available, and the browser continues
to the existing profile/matches flow. Successful AI create consumes exactly one
free import; failed generation, cancellation, expiry, abandonment, ownership
failure, and a manual-create race consume zero. Manual creation remains entirely
entitlement-free. Paid re-import, a second AI import, AI correction of an
existing profile, and public/legacy rollout remain unimplemented.
