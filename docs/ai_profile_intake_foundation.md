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
enrichment. They are versioned as `ai_profile_extraction_prompt_v1`,
`model_evidence_packet_v1`, and `ai_profile_extraction_v1`.

Resume text is declared untrusted data. Instructions inside it—including
requests to ignore instructions, reveal secrets, browse, use tools, access
files, or call a network—remain data. The prompt forbids sensitive-attribute
inference; inference of language from name/location/nationality; inference of
current residence from old jobs; inference of missing credentials from
omission; and conversion of historical behavior into present preferences.
Every fact must cite supplied evidence. Taxonomy, normalization, and calculated
facts must be marked inferred rather than explicit.

## Structured output and local validation

The OpenAI request uses strict Structured Outputs with a profile-specific JSON
schema. Document and evidence references are request-defined constants, fields
use the existing Canonical V2 paths and enums, and additional properties are
closed. Structured Output is not considered a trust boundary: every response
also passes the local `ai_profile_extraction_v1` validator.

The strict validator rejects unknown fields and keys, missing evidence,
non-finite values, invalid enums and types, oversized facts, inferred sensitive
location/preferences, and any attempt to supply account, principal, profile,
revision, durable-source, provenance, entitlement, or matcher-signal authority.
Classification paths are required to use `explicit: false`. Provider failures,
refusals, timeouts, malformed output, and local contract rejection map to stable
content-free intake error codes.

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
randomness. A new generation replaces the previous active draft for the same
browser lineage, and a process-local in-flight guard prevents concurrent
generation for that lineage.

Every record is bound to account, browser session, environment, account-native
principal, exact ownership binding/version/latest-event/lineage digest, and
the dedicated `ai_profile_intake_review_v1` purpose. Unknown, malformed,
expired, cross-account, cross-session, cross-principal, cross-environment, or
lineage-mismatched access returns no record. Review mutations use an expected
version and purpose-specific CSRF proof, so stale or replayed edits fail.

The vault retains one entry per bundle containing only the editable reconciled
review draft, safe metadata for at most two documents (opaque reference, origin,
format, byte/page counts, parser name/version), content-free per-request model
diagnostics when available, timestamps, and the mutation version. It does not
retain upload bytes, an `EvidencePacket`, a `ModelEvidencePacket` or its text,
unminimized contact PII, prompt text, raw provider output, original filename,
durable profile/revision/source IDs, entitlement state, or matcher signals.

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

Edits update only the process-local draft. There is intentionally no “Save
profile” operation, repository call, Canonical V2 revision, matcher-signal
generation, entitlement consumption, or fake persisted profile. Cancellation
removes the bound draft and returns to profile creation. Expired drafts show a
clear start-again response.

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
synthetic, non-personal data. This slice adds no profile/revision/entitlement
write, migration, matching behavior, public route, deployment change, DNS,
proxy, Vercel, DigitalOcean, WorkOS authority change, or old-site cutover.

Future entitlement accounting must count the importer bundle—resume only,
LinkedIn only, or resume plus LinkedIn—as exactly one import attempt. Internal
adapter request count and advisory usage/cost diagnostics must never determine
authorization, entitlement consumption, billing, or profile correctness.
