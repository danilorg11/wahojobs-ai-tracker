from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import sqlite3

from scripts.ai_profile_import_migration import apply_ai_profile_import_migration
from scripts.resumable_ai_profile_intake_migration import (
    apply_resumable_ai_profile_intake_migration,
)
from scripts.public_job_identity_migration import apply_public_job_identity_migration
from tests.browser_session_authentication_test_support import REQUEST_AT, seed_browser_session
from tests.workos_authkit_test_support import build_m008
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.profile_intake.contracts import (
    AI_EXTRACTION_SCHEMA_VERSION,
    DocumentFormat,
    DocumentKind,
    ModelEvidenceBlock,
    ModelEvidencePacket,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.review_draft import (
    ValidatedProfileSource,
    reconcile_profile_extractions,
)
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_REVIEW_ROUTE,
    ProfileIntakeAuthorityService,
    SafeDocumentBundleMetadata,
    SafeDocumentMetadata,
    SafeModelDiagnostics,
    editable_profile_review,
    review_value_for_form,
    update_editable_review,
)
from wahojobs.ai_profile_import import AIProfileImportSourceMetadata


NOW = REQUEST_AT


def import_source_metadata(origins=("resume",), *, model="fake-profile-v1"):
    documents = []
    diagnostics = []
    for index, requested_origin in enumerate(origins, start=1):
        is_docx = requested_origin == "resume_docx"
        origin = "resume" if is_docx else requested_origin
        document_format = "docx" if is_docx else "pdf"
        parser = "python-docx" if is_docx else "pypdf"
        parser_version = "1.2.0" if is_docx else "5.0.0"
        documents.append(
            SafeDocumentMetadata(
                document_reference=f"doc_{index:032x}",
                origin=origin,
                format=document_format,
                byte_size=1024,
                page_count=None if is_docx else 1,
                parser=parser,
                parser_version=parser_version,
            )
        )
        diagnostics.append(
            SafeModelDiagnostics(
                document_kind=origin,
                model=model,
                prompt_version="ai_profile_extraction_prompt_v1",
                schema_version="ai_profile_extraction_v1",
                input_tokens=100,
                output_tokens=50,
                duration_ms=10,
                provider_request_id=None,
                success=True,
                failure_code=None,
            )
        )
    return AIProfileImportSourceMetadata.from_runtime(
        SafeDocumentBundleMetadata(tuple(documents)),
        tuple(diagnostics),
    )


def install_ai_profile_import_database(path, *, suffix="94"):
    connection = build_m008(Path(path))
    apply_public_job_identity_migration(connection)
    apply_ai_profile_import_migration(connection)
    apply_resumable_ai_profile_intake_migration(connection)
    session = seed_browser_session(
        connection,
        suffix=suffix,
        idle_ttl=timedelta(hours=3),
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection, session


@contextmanager
def _read_connection(path):
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def intake_grant(path, session, *, now=NOW):
    service = ProfileIntakeAuthorityService(
        authentication_gateway=DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=session["environment"],
            clock=lambda: now,
        ),
        authorization_gateway=DurablePersistentProfileReadAuthorizationGateway(),
        read_connection_provider=lambda: _read_connection(path),
        clock=lambda: now,
    )
    headers = (
        ("Host", "localhost:8443"),
        (
            "Cookie",
            f"wahojobs_session={session['session_token']}; "
            f"__Host-wahojobs_session_csrf={session['csrf_secret']}",
        ),
    )
    outcome = service.authorize(
        method="GET",
        route=PROFILE_INTAKE_REVIEW_ROUTE,
        authentication_input=headers,
        session_token=session["session_token"],
        csrf_secret=session["csrf_secret"],
    )
    if outcome.state != "authorized":
        raise AssertionError(f"grant unavailable: {outcome.state}")
    return outcome.grant_for_service()


def confirmed_review(
    *,
    origins=(DocumentKind.RESUME,),
    conflict=False,
    preference_model=None,
):
    sources = []
    for index, kind in enumerate(origins, start=1):
        reference = f"doc_{index:032x}"
        packet = ModelEvidencePacket(
            document_reference=reference,
            document_kind=kind,
            document_format=DocumentFormat.PDF,
            blocks=(ModelEvidenceBlock("b001", "Synthetic professional evidence"),),
        )
        city = "Lisbon" if index == 1 or not conflict else "Porto"
        facts = [
            {
                "field_path": "identity.display_name",
                "value": "Synthetic Candidate",
                "source_document_reference": reference,
                "evidence_block_references": ["b001"],
                "confidence": 0.99,
                "explicit": True,
            },
            {
                "field_path": "location.city",
                "value": city,
                "source_document_reference": reference,
                "evidence_block_references": ["b001"],
                "confidence": 0.95,
                "explicit": True,
            },
            {
                "field_path": "skills.normalized",
                "value": "Python" if index == 1 else "SQL",
                "source_document_reference": reference,
                "evidence_block_references": ["b001"],
                "confidence": 0.9,
                "explicit": True,
            },
            {
                "field_path": "experience.seniority",
                "value": "senior",
                "source_document_reference": reference,
                "evidence_block_references": ["b001"],
                "confidence": 0.8,
                "explicit": False,
            },
        ]
        extraction = validate_ai_profile_extraction(
            {
                "schema_version": AI_EXTRACTION_SCHEMA_VERSION,
                "document_reference": reference,
                "facts": facts,
            },
            packet,
        )
        sources.append(ValidatedProfileSource(kind, extraction))
    review = editable_profile_review(reconcile_profile_extractions(tuple(sources)))
    values = tuple(review_value_for_form(fact.value) for fact in review.facts)
    accepted_conflicts = set()
    decisions = []
    for fact in review.facts:
        if not fact.suggested:
            decisions.append("keep")
        elif fact.conflict_group is None:
            decisions.append("accept")
        elif fact.conflict_group not in accepted_conflicts:
            accepted_conflicts.add(fact.conflict_group)
            decisions.append("accept")
        else:
            decisions.append("reject")
    user_inputs = {name: "" for name in review.missing_user_fields}
    return update_editable_review(
        review,
        values,
        tuple(decisions),
        user_inputs,
        preference_model,
    )


def database_counts(connection):
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "product_profiles",
            "product_profile_revisions",
            "product_profile_sources",
            "ai_profile_import_entitlements",
            "ai_profile_import_attempts",
        )
    }
