# AI Profile Intake v1: document and extraction foundation

This boundary performs deterministic, in-memory text extraction for a
text-based resume/CV supplied as PDF or DOCX. It then exposes the text only as
ordered, bounded evidence blocks (`b001`, `b002`, and so on) for a future
minimization and model-extraction step.

This slice does **not** call an external LLM, accept browser uploads, persist a
profile or evidence, create durable provenance, check an entitlement, or change
matching. `Canonical Profile V2` remains the sole durable profile authority.

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
preserved as evidence rather than executed or interpreted. A later PII
minimization step can replace or remove contact-bearing blocks before passing an
`EvidencePacket` across an external-model boundary. Evidence must remain
ephemeral and must not be copied into durable Canonical V2 provenance.

## Future adapter boundary

`ProfileExtractionAdapter` accepts only an `EvidencePacket` and returns a
validated `ai_profile_extraction_v1` contract. The contract is a bounded list of
facts addressed by existing Canonical V2 field paths. Each fact includes its
normalized value, source document reference, supporting evidence references,
numeric confidence, and explicit/inferred status.

The strict validator rejects unknown fields and keys, missing evidence,
non-finite values, invalid enums and types, oversized facts, inferred sensitive
location/preferences, and any attempt to supply account, principal, profile,
revision, durable-source, provenance, entitlement, or matcher-signal authority.

`DeterministicFakeProfileExtractionAdapter` is the only implementation in this
slice. It performs no I/O or network activity and sends its configured response
through the same strict validator that a future real adapter must use.
