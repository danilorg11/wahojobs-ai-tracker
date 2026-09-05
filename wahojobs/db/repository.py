import json
from pathlib import Path
from types import SimpleNamespace

from wahojobs.classification import (
    DEFAULT_AVAILABILITY_BASIS,
    DEFAULT_INCLUDE_IN_LIVE_MARKET_ESTIMATE,
    DEFAULT_INVENTORY_MODEL,
    DEFAULT_MARKET_COUNT_POLICY,
    DEFAULT_OPPORTUNITY_KIND,
    DEFAULT_SOURCE_TIER,
    INVENTORY_MODEL_CORPORATE_CAREERS,
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_MIXED,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    MARKET_COUNT_POLICY_COUNT_LIVE,
    MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
    SOURCE_TIER_EXPERIMENTAL,
    default_availability_basis_for_inventory_model,
    default_opportunity_kind_for_inventory_model,
    include_in_live_market_estimate_for_policy,
)
from wahojobs.config import DB_PATH
from wahojobs.crawler.types import MERCOR_RECORD_CONTRACT_ID, PROVIDER_DETAIL_RECORD_CONTRACT_ID
from wahojobs.canonical.service import (
    sync_alignerr_canonical_opportunities,
    sync_dataforce_canonical_opportunities,
    sync_fallback_canonical_opportunities,
    sync_meridial_canonical_opportunities,
    sync_micro1_canonical_opportunities,
    sync_mindrift_canonical_opportunities,
    sync_oneforma_canonical_opportunities,
    sync_turing_canonical_opportunities,
    sync_welocalize_canonical_opportunities,
)
from wahojobs.db.connection import get_connection
from wahojobs.opportunity_enrichment_schema import (
    OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS,
)
from wahojobs.source_capture import (
    EVIDENCE_STATE_ACCEPTED_CURRENT,
    EVIDENCE_STATE_DEGRADED_LATEST,
    EVIDENCE_STATE_LEGACY_ACCEPTED,
    EVIDENCE_STATE_MISSING,
    EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD,
    EVIDENCE_REASON_CAPTURE_CONTRACT_CHANGED,
    EVIDENCE_REASON_LATEST_CAPTURE_NOT_ACCEPTED,
    EVIDENCE_REASON_LATEST_SOURCE_ATTEMPT_FAILED,
    EVIDENCE_REASON_PROMOTION_POLICY_CHANGED,
    PROMOTION_DECISION_CONFIRMED,
    PROMOTION_DECISION_PROMOTED,
    PreparedRecordPromotionAttestation,
    SEMANTIC_AUTHORITY_LEGACY_ACCEPTED,
    SEMANTIC_AUTHORITY_PENDING,
    SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED,
    SOURCE_CAPTURE_CONTRACT_VERSION,
    SOURCE_CAPTURE_CONTRACT_PREPARERS,
    SOURCE_CAPTURE_EVIDENCE_VERSION,
    SOURCE_PROMOTION_POLICY_VERSION,
    MERCOR_PROMOTION_POLICY_VERSION,
    PROVIDER_DETAIL_PROMOTION_POLICY_VERSION,
    SOURCE_PROMOTION_POLICY_DECIDERS,
    SourceCapturePersistenceResult,
    SourceCaptureContext,
    SourcePromotionDecision,
    canonical_semantic_job_fields_json,
    canonical_source_metadata_json,
    decide_source_promotion,
    normalize_source_body,
    parse_source_timestamp,
    prepare_record_promotion_attestation,
    prepare_semantic_source_material,
    prepare_source_capture,
    prepare_stored_record_promotion_attestation,
    semantic_job_fields_from_row,
    semantic_source_material_sha256,
    source_material_content_sha256,
)


OUTLIER_SEED = {
    "name": "Outlier",
    "slug": "outlier",
    "careers_url": "https://app.outlier.ai/internal/experts/job-board/jobs",
}

ALIGNERR_SEED = {
    "name": "Alignerr",
    "slug": "alignerr",
    "careers_url": "https://www.alignerr.com/api/jobs",
}

APPEN_SEED = {
    "name": "Appen",
    "slug": "appen",
    "careers_url": "https://api.lever.co/v0/postings/appen?mode=json&expand=location",
}

DATAFORCE_SEED = {
    "name": "DataForce",
    "slug": "dataforce",
    "careers_url": "https://dataforcecommunity.transperfect.com/projects",
}

DATAANNOTATION_SEED = {
    "name": "DataAnnotation",
    "slug": "dataannotation",
    "careers_url": "https://www.dataannotation.tech",
    "inventory_model": INVENTORY_MODEL_EVERGREEN_APPLICATION,
    "market_count_policy": MARKET_COUNT_POLICY_REPORT_SEPARATELY,
}

HANDSHAKE_SEED = {
    "name": "Handshake AI",
    "slug": "handshake",
    "careers_url": "https://joinhandshake.com/ai/opportunities",
    "inventory_model": INVENTORY_MODEL_PUBLIC_INVENTORY,
    "market_count_policy": MARKET_COUNT_POLICY_REPORT_SEPARATELY,
}

INVISIBLE_SEED = {
    "name": "Invisible Technologies",
    "slug": "invisible",
    "careers_url": "https://boards-api.greenhouse.io/v1/boards/invisibletech/jobs",
    "source_tier": SOURCE_TIER_EXPERIMENTAL,
    "inventory_model": INVENTORY_MODEL_CORPORATE_CAREERS,
    "market_count_policy": MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
}

MERIDIAL_SEED = {
    "name": "Meridial",
    "slug": "meridial",
    "careers_url": "https://boards-api.greenhouse.io/v1/boards/agency/departments/4012485101?render_as=tree",
}

MERCOR_SEED = {
    "name": "Mercor",
    "slug": "mercor",
    "careers_url": "https://aws.api.mercor.com/work/listings-explore-page",
}

MICRO1_SEED = {
    "name": "micro1",
    "slug": "micro1",
    "careers_url": "https://prod-api.micro1.ai/api/v1/job/portal",
}

MINDRIFT_SEED = {
    "name": "Mindrift",
    "slug": "mindrift",
    "careers_url": "https://apply.workable.com/api/v3/accounts/toloka-ai/jobs",
}

ONEFORMA_SEED = {
    "name": "OneForma",
    "slug": "oneforma",
    "careers_url": "https://www.oneforma.com/wp-json/wp/v2/job?per_page=100&_embed=wp:term",
}

RWS_SEED = {
    "name": "RWS TrainAI",
    "slug": "rws",
    "careers_url": "https://api.lever.co/v0/postings/rws?mode=json&expand=location",
}

SURGE_SEED = {
    "name": "Surge AI",
    "slug": "surge",
    "careers_url": "https://surgehq.ai",
    "inventory_model": INVENTORY_MODEL_MIXED,
    "market_count_policy": MARKET_COUNT_POLICY_REPORT_SEPARATELY,
}

TURING_SEED = {
    "name": "Turing",
    "slug": "turing",
    "careers_url": "https://work.turing.com/api/jobs/all",
}

WELOCALIZE_SEED = {
    "name": "Welocalize",
    "slug": "welocalize",
    "careers_url": "https://api.lever.co/v0/postings/weloglobal?mode=json&expand=location",
}


def initialize_database(db_path=DB_PATH):
    with get_connection(db_path) as conn:
        install_base_schema(conn)
        for seed in (
            ALIGNERR_SEED,
            APPEN_SEED,
            DATAFORCE_SEED,
            DATAANNOTATION_SEED,
            HANDSHAKE_SEED,
            INVISIBLE_SEED,
            MERIDIAL_SEED,
            MERCOR_SEED,
            MICRO1_SEED,
            MINDRIFT_SEED,
            ONEFORMA_SEED,
            OUTLIER_SEED,
            RWS_SEED,
            SURGE_SEED,
            TURING_SEED,
            WELOCALIZE_SEED,
        ):
            seed = with_source_classification_defaults(seed)
            conn.execute(
                """
                INSERT INTO companies (
                  name, slug, careers_url,
                  source_tier, inventory_model, market_count_policy
                )
                VALUES (
                  :name, :slug, :careers_url,
                  :source_tier, :inventory_model, :market_count_policy
                )
                ON CONFLICT(slug) DO UPDATE SET
                  name = excluded.name,
                  careers_url = excluded.careers_url,
                  source_tier = excluded.source_tier,
                  inventory_model = excluded.inventory_model,
                  market_count_policy = excluded.market_count_policy,
                  updated_at = CURRENT_TIMESTAMP
                """,
                seed,
            )
        refresh_jobs_from_source_classification_defaults(conn)
        alignerr = get_company_by_slug(conn, "alignerr")
        if alignerr is not None:
            sync_alignerr_canonical_opportunities(conn, alignerr["id"])
        dataforce = get_company_by_slug(conn, "dataforce")
        if dataforce is not None:
            sync_dataforce_canonical_opportunities(conn, dataforce["id"])
        meridial = get_company_by_slug(conn, "meridial")
        if meridial is not None:
            sync_meridial_canonical_opportunities(conn, meridial["id"])
        mindrift = get_company_by_slug(conn, "mindrift")
        if mindrift is not None:
            sync_mindrift_canonical_opportunities(conn, mindrift["id"])
        micro1 = get_company_by_slug(conn, "micro1")
        if micro1 is not None:
            sync_micro1_canonical_opportunities(conn, micro1["id"])
        oneforma = get_company_by_slug(conn, "oneforma")
        if oneforma is not None:
            sync_oneforma_canonical_opportunities(conn, oneforma["id"])
        turing = get_company_by_slug(conn, "turing")
        if turing is not None:
            sync_turing_canonical_opportunities(conn, turing["id"])
        welocalize = get_company_by_slug(conn, "welocalize")
        if welocalize is not None:
            sync_welocalize_canonical_opportunities(conn, welocalize["id"])
        for company in conn.execute("SELECT id FROM companies ORDER BY id").fetchall():
            sync_fallback_canonical_opportunities(conn, company["id"])


def install_base_schema(conn):
    """Install only the complete current base schema on an open connection."""

    schema_path = Path(__file__).with_name("schema.sql")
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    ensure_company_classification_columns(conn)
    ensure_job_optional_columns(conn)
    ensure_job_classification_columns(conn)
    ensure_canonical_schema(conn)
    ensure_opportunity_enrichment_schema(conn)


def with_source_classification_defaults(seed):
    classified = {
        "source_tier": DEFAULT_SOURCE_TIER,
        "inventory_model": DEFAULT_INVENTORY_MODEL,
        "market_count_policy": DEFAULT_MARKET_COUNT_POLICY,
    }
    classified.update(seed)
    return classified


def ensure_company_classification_columns(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(companies)").fetchall()
    }
    if "source_tier" not in columns:
        conn.execute(
            f"ALTER TABLE companies ADD COLUMN source_tier TEXT NOT NULL DEFAULT '{DEFAULT_SOURCE_TIER}'"
        )
    if "inventory_model" not in columns:
        conn.execute(
            f"ALTER TABLE companies ADD COLUMN inventory_model TEXT NOT NULL DEFAULT '{DEFAULT_INVENTORY_MODEL}'"
        )
    if "market_count_policy" not in columns:
        conn.execute(
            f"ALTER TABLE companies ADD COLUMN market_count_policy TEXT NOT NULL DEFAULT '{DEFAULT_MARKET_COUNT_POLICY}'"
        )


def ensure_job_optional_columns(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "department" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN department TEXT")
    if "expertise" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN expertise TEXT")
    if "commitment" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN commitment TEXT")
    if "canonical_opportunity_id" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN canonical_opportunity_id INTEGER")
    ensure_job_semantic_authority_column(conn)


def ensure_job_semantic_authority_column(conn):
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone() is None:
        return
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "semantic_authority_state" not in columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN semantic_authority_state TEXT NOT NULL "
            "DEFAULT 'legacy_accepted' CHECK (semantic_authority_state IN "
            "('legacy_accepted', 'pending', 'versioned_accepted'))"
        )


def ensure_job_classification_columns(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "opportunity_kind" not in columns:
        conn.execute(
            f"ALTER TABLE jobs ADD COLUMN opportunity_kind TEXT NOT NULL DEFAULT '{DEFAULT_OPPORTUNITY_KIND}'"
        )
    if "availability_basis" not in columns:
        conn.execute(
            f"ALTER TABLE jobs ADD COLUMN availability_basis TEXT NOT NULL DEFAULT '{DEFAULT_AVAILABILITY_BASIS}'"
        )
    if "include_in_live_market_estimate" not in columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN include_in_live_market_estimate "
            f"INTEGER NOT NULL DEFAULT {DEFAULT_INCLUDE_IN_LIVE_MARKET_ESTIMATE}"
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_live_market
        ON jobs(include_in_live_market_estimate, is_active)
        """
    )


def refresh_jobs_from_source_classification_defaults(conn):
    rows = conn.execute(
        """
        SELECT id, inventory_model, market_count_policy
        FROM companies
        WHERE market_count_policy != ?
           OR inventory_model != ?
        """,
        (MARKET_COUNT_POLICY_COUNT_LIVE, DEFAULT_INVENTORY_MODEL),
    ).fetchall()
    for row in rows:
        opportunity_kind = default_opportunity_kind_for_inventory_model(
            row["inventory_model"]
        )
        availability_basis = default_availability_basis_for_inventory_model(
            row["inventory_model"]
        )
        include_in_live_market_estimate = include_in_live_market_estimate_for_policy(
            row["market_count_policy"]
        )
        conn.execute(
            """
            UPDATE jobs
            SET opportunity_kind = ?,
                availability_basis = ?,
                include_in_live_market_estimate = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE company_id = ?
              AND opportunity_kind = ?
              AND availability_basis = ?
              AND include_in_live_market_estimate = ?
            """,
            (
                opportunity_kind,
                availability_basis,
                include_in_live_market_estimate,
                row["id"],
                DEFAULT_OPPORTUNITY_KIND,
                DEFAULT_AVAILABILITY_BASIS,
                DEFAULT_INCLUDE_IN_LIVE_MARKET_ESTIMATE,
            ),
        )


def ensure_canonical_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS canonical_opportunities (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          company_id INTEGER NOT NULL,
          canonical_key TEXT NOT NULL,
          canonical_title TEXT NOT NULL,
          normalized_title TEXT NOT NULL,
          source_category TEXT NOT NULL,
          language TEXT,
          language_locale TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          variant_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

          FOREIGN KEY (company_id) REFERENCES companies(id),
          UNIQUE (company_id, canonical_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_canonical_opportunity
        ON jobs(canonical_opportunity_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_canonical_opportunities_company_active
        ON canonical_opportunities(company_id, is_active)
        """
    )


def ensure_opportunity_enrichment_schema(conn):
    ensure_job_semantic_authority_column(conn)
    for statement in OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS:
        conn.execute(statement)
    capture_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(job_source_content_captures)"
        ).fetchall()
    }
    record_attestation_columns = (
        (
            "record_promotion_contract_id",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "body_observation",
            "TEXT NOT NULL DEFAULT 'not_observed' CHECK ("
            "body_observation IN ('present', 'explicitly_empty', 'not_observed'))",
        ),
        (
            "authority_evidence_json",
            "TEXT NOT NULL DEFAULT '{}'",
        ),
    )
    for column, declaration in record_attestation_columns:
        if column not in capture_columns:
            conn.execute(
                "ALTER TABLE job_source_content_captures ADD COLUMN "
                f"{column} {declaration}"
            )
    for table in ("opportunity_enrichments", "opportunity_enrichment_runs"):
        columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column in ("semantic_input_version", "derivation_fingerprint"):
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def get_company_by_slug(conn, slug):
    return conn.execute(
        "SELECT * FROM companies WHERE slug = ?",
        (slug,),
    ).fetchone()


def create_crawl_run(conn, company_id, started_at):
    cursor = conn.execute(
        """
        INSERT INTO crawl_runs (company_id, status, started_at)
        VALUES (?, 'running', ?)
        """,
        (company_id, started_at),
    )
    return cursor.lastrowid


def finish_crawl_run(
    conn,
    crawl_run_id,
    summary,
    finished_at,
    status="success",
    error_message=None,
):
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = ?,
            finished_at = ?,
            jobs_found_count = ?,
            jobs_new_count = ?,
            jobs_reactivated_count = ?,
            jobs_updated_count = ?,
            jobs_removed_count = ?,
            used_sample_data = ?,
            error_message = ?
        WHERE id = ?
        """,
        (
            status,
            finished_at,
            summary.jobs_found,
            summary.jobs_new,
            summary.jobs_reactivated,
            summary.jobs_updated,
            summary.jobs_removed,
            int(summary.used_sample_data),
            error_message,
            crawl_run_id,
        ),
    )


def fail_crawl_run(conn, crawl_run_id, error_message, finished_at):
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = 'failed',
            finished_at = ?,
            error_message = ?
        WHERE id = ?
        """,
        (finished_at, error_message, crawl_run_id),
    )


def get_job_by_hash(conn, company_id, source_hash):
    return conn.execute(
        """
        SELECT * FROM jobs
        WHERE company_id = ? AND source_hash = ?
        """,
        (company_id, source_hash),
    ).fetchone()


def insert_job(
    conn,
    company_id,
    candidate,
    now,
    *,
    semantic_authority_state=SEMANTIC_AUTHORITY_LEGACY_ACCEPTED,
):
    if semantic_authority_state not in {
        SEMANTIC_AUTHORITY_LEGACY_ACCEPTED,
        SEMANTIC_AUTHORITY_PENDING,
    }:
        raise ValueError("New job semantic authority state is invalid.")
    classification = resolve_job_classification(conn, company_id, candidate)
    cursor = conn.execute(
        """
        INSERT INTO jobs (
          company_id, external_id, title, location, department, expertise, commitment, url, source_hash,
          opportunity_kind, availability_basis, include_in_live_market_estimate,
          semantic_authority_state,
          first_seen_at, last_seen_at, is_active, removed_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
        """,
        (
            company_id,
            candidate.external_id,
            candidate.title,
            candidate.location,
            candidate.department,
            candidate.expertise,
            candidate.commitment,
            candidate.url,
            candidate.source_hash,
            classification["opportunity_kind"],
            classification["availability_basis"],
            classification["include_in_live_market_estimate"],
            semantic_authority_state,
            now,
            now,
            now,
        ),
    )
    return cursor.lastrowid


def update_seen_job(conn, job_id, now):
    """Advance lifecycle state without changing semantic authority."""

    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise RuntimeError(f"Unknown job id: {job_id}")
    conn.execute(
        """
        UPDATE jobs
        SET last_seen_at = ?,
            is_active = 1,
            removed_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (now, now, job_id),
    )


def _promote_job_semantic_material(
    conn,
    job_id,
    candidate,
    classification,
    now,
):
    """Materialize only semantic fields covered by an accepted capture."""

    conn.execute(
        """
        UPDATE jobs
        SET external_id = ?,
            title = ?,
            location = ?,
            department = ?,
            expertise = ?,
            commitment = ?,
            url = ?,
            opportunity_kind = ?,
            availability_basis = ?,
            include_in_live_market_estimate = ?,
            semantic_authority_state = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            candidate.external_id,
            candidate.title,
            candidate.location,
            candidate.department,
            candidate.expertise,
            candidate.commitment,
            candidate.url,
            classification["opportunity_kind"],
            classification["availability_basis"],
            classification["include_in_live_market_estimate"],
            SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED,
            now,
            job_id,
        ),
    )


def upsert_job_source_content(
    conn,
    job_id,
    provider,
    source_type,
    candidate,
    now,
    *,
    capture_context,
):
    """Append one observation and promote it only when the policy permits."""

    if not isinstance(capture_context, SourceCaptureContext):
        raise TypeError("capture_context must be a SourceCaptureContext.")
    provider = str(provider or "").strip()
    source_type = str(source_type or "").strip()
    prepared = prepare_source_capture(candidate)
    savepoint = "job_source_capture_promotion"
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        job = _validate_capture_identity_and_crawl_run(
            conn,
            job_id,
            candidate,
            capture_context.crawl_run_id,
            provider,
        )
        classification = resolve_job_classification(
            conn,
            job["company_id"],
            candidate,
        )
        prepared_semantic = prepare_semantic_source_material(
            candidate,
            classification,
            prepared,
            provider=provider,
            source_type=source_type,
        )
        prepared_attestation = prepare_record_promotion_attestation(
            candidate,
            prepared,
            capture_context,
            provider=provider,
            source_type=source_type,
        )
        promotion_policy_version = (
            PROVIDER_DETAIL_PROMOTION_POLICY_VERSION
            if prepared_attestation.contract_id == PROVIDER_DETAIL_RECORD_CONTRACT_ID
            else MERCOR_PROMOTION_POLICY_VERSION
            if prepared_attestation.contract_id == MERCOR_RECORD_CONTRACT_ID
            else SOURCE_PROMOTION_POLICY_VERSION
        )
        accepted = conn.execute(
            "SELECT * FROM job_source_contents WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        existing_acceptance = conn.execute(
            """
            SELECT accepted_capture_id, promotion_policy_version,
                   accepted_at, last_confirmed_at
            FROM job_source_content_acceptances
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        accepted_capture, accepted_semantic_hash = _verify_source_acceptance(
            conn,
            job_id,
            accepted,
            existing_acceptance,
            job=job,
        )
        accepted_attestation = (
            _prepared_record_attestation_from_row(accepted_capture)
            if accepted_capture is not None
            else None
        )
        if (accepted_attestation is not None
                and accepted_attestation.contract_id == PROVIDER_DETAIL_RECORD_CONTRACT_ID
                and provider in {"alignerr", "micro1"}):
            # Later catalog teasers/failures must not overwrite accepted details.
            promotion_policy_version = PROVIDER_DETAIL_PROMOTION_POLICY_VERSION
        policy_decider = (
            SOURCE_PROMOTION_POLICY_DECIDERS[promotion_policy_version]
            if promotion_policy_version in {MERCOR_PROMOTION_POLICY_VERSION, PROVIDER_DETAIL_PROMOTION_POLICY_VERSION}
            else decide_source_promotion
        )
        decision = policy_decider(
            prepared,
            capture_context,
            accepted,
            same_accepted_semantic_material=(
                accepted_semantic_hash
                == prepared_semantic.semantic_material_sha256
            ),
            record_attestation=prepared_attestation,
            accepted_record_attestation=accepted_attestation,
        )
        if (
            existing_acceptance is None
            and accepted is not None
            and decision.decision == PROMOTION_DECISION_CONFIRMED
        ):
            # The material is unchanged, but a legacy row has no replayable
            # accepted predecessor.  Establish explicit provenance as a
            # promotion instead of inventing a historical confirmation.
            decision = SourcePromotionDecision(
                PROMOTION_DECISION_PROMOTED,
                decision.reasons,
                decision.accepted_source_updated_at,
            )
        reasons_json = json.dumps(
            list(decision.reasons),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        capture_id = conn.execute(
            """
            INSERT INTO job_source_content_captures (
              job_id, crawl_run_id, provider, source_type, source_url,
              external_id, body, body_format, metadata_json,
              material_content_sha256, semantic_job_fields_json,
              semantic_material_sha256, source_updated_at,
              source_timestamp_status, capture_quality, provider_outcome,
              used_sample_data, snapshot_complete, pagination_complete,
              empty_snapshot_validated, raw_record_count,
              normalized_record_count, candidate_count,
              rejected_record_count, payload_shape, schema_fingerprint,
              record_promotion_contract_id, body_observation,
              authority_evidence_json,
              capture_contract_version, promotion_policy_version,
              promotion_decision, decision_reasons_json, observed_at
            )
            VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                job_id,
                capture_context.crawl_run_id,
                provider,
                source_type,
                candidate.url,
                candidate.external_id,
                prepared.body,
                prepared.body_format,
                prepared.metadata_json,
                prepared.material_content_sha256,
                prepared_semantic.job_fields_json,
                prepared_semantic.semantic_material_sha256,
                prepared.source_updated_at,
                prepared.source_timestamp_status,
                prepared.quality,
                capture_context.provider_outcome,
                int(capture_context.used_sample_data),
                int(capture_context.snapshot_complete),
                int(capture_context.pagination_complete),
                int(capture_context.empty_snapshot_validated),
                capture_context.raw_record_count,
                capture_context.normalized_record_count,
                capture_context.candidate_count,
                capture_context.rejected_record_count,
                capture_context.payload_shape,
                capture_context.schema_fingerprint,
                prepared_attestation.contract_id,
                prepared_attestation.body_observation,
                prepared_attestation.authority_evidence_json,
                SOURCE_CAPTURE_CONTRACT_VERSION,
                promotion_policy_version,
                decision.decision,
                reasons_json,
                now,
            ),
        ).lastrowid

        if decision.decision == PROMOTION_DECISION_PROMOTED:
            conn.execute(
                """
                INSERT INTO job_source_contents (
                  job_id, provider, source_type, source_url, external_id,
                  body, body_format, metadata_json, material_content_sha256,
                  source_updated_at, first_captured_at, last_captured_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  provider = excluded.provider,
                  source_type = excluded.source_type,
                  source_url = excluded.source_url,
                  external_id = excluded.external_id,
                  body = excluded.body,
                  body_format = excluded.body_format,
                  metadata_json = excluded.metadata_json,
                  material_content_sha256 = excluded.material_content_sha256,
                  source_updated_at = excluded.source_updated_at,
                  last_captured_at = excluded.last_captured_at,
                  updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    provider,
                    source_type,
                    candidate.url,
                    candidate.external_id,
                    prepared.body,
                    prepared.body_format,
                    prepared.metadata_json,
                    prepared.material_content_sha256,
                    decision.accepted_source_updated_at,
                    now,
                    now,
                    now,
                ),
            )
        elif decision.decision == PROMOTION_DECISION_CONFIRMED:
            conn.execute(
                """
                UPDATE job_source_contents
                SET source_updated_at = ?,
                    last_captured_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    decision.accepted_source_updated_at,
                    now,
                    now,
                    job_id,
                ),
            )

        if decision.accepted:
            _promote_job_semantic_material(
                conn,
                job_id,
                candidate,
                classification,
                now,
            )
            accepted_at = (
                existing_acceptance["accepted_at"]
                if (
                    decision.decision == PROMOTION_DECISION_CONFIRMED
                    and existing_acceptance is not None
                )
                else now
            )
            conn.execute(
                """
                INSERT INTO job_source_content_acceptances (
                  job_id, accepted_capture_id, promotion_policy_version,
                  accepted_at, last_confirmed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  accepted_capture_id = excluded.accepted_capture_id,
                  promotion_policy_version = excluded.promotion_policy_version,
                  accepted_at = excluded.accepted_at,
                  last_confirmed_at = excluded.last_confirmed_at,
                  updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    capture_id,
                    promotion_policy_version,
                    accepted_at,
                    now,
                    now,
                ),
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_transaction:
            conn.rollback()
        raise
    return SourceCapturePersistenceResult(
        capture_id=int(capture_id),
        material_content_sha256=prepared.material_content_sha256,
        semantic_material_sha256=prepared_semantic.semantic_material_sha256,
        promotion_decision=decision.decision,
    )


def get_job_source_capture_evidence(conn, job_id):
    """Return capture/LKG state without changing semantic-input material."""

    job = conn.execute(
        """
        SELECT j.*, c.slug AS company_slug
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if job is None:
        raise RuntimeError(f"Unknown job id: {job_id}")
    accepted = conn.execute(
        "SELECT * FROM job_source_contents WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    acceptance = conn.execute(
        """
        SELECT accepted_capture_id, promotion_policy_version,
               accepted_at, last_confirmed_at
        FROM job_source_content_acceptances
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    latest = conn.execute(
        """
        SELECT id, material_content_sha256, semantic_material_sha256,
               capture_quality,
               promotion_decision, decision_reasons_json, observed_at,
               capture_contract_version, promotion_policy_version
        FROM job_source_content_captures
        WHERE job_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()

    accepted_capture, accepted_semantic_hash = _verify_source_acceptance(
        conn,
        job_id,
        accepted,
        acceptance,
        job=job,
    )

    latest_crawl_run = conn.execute(
        """
        SELECT cr.id, cr.status, cr.used_sample_data
        FROM jobs j
        JOIN crawl_runs cr ON cr.company_id = j.company_id
        WHERE j.id = ?
        ORDER BY cr.id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    later_failed_observation = bool(
        accepted_capture is not None
        and accepted_capture["crawl_run_id"] is not None
        and latest_crawl_run is not None
        and latest_crawl_run["id"] > accepted_capture["crawl_run_id"]
        and (
            latest_crawl_run["status"] in {"failed", "partial", "contract_drift"}
            or bool(latest_crawl_run["used_sample_data"])
        )
    )
    evidence_stale_reasons = []
    current_promotion_policy_version = (
        PROVIDER_DETAIL_PROMOTION_POLICY_VERSION
        if accepted_capture is not None
        and accepted_capture["record_promotion_contract_id"] == PROVIDER_DETAIL_RECORD_CONTRACT_ID
        else MERCOR_PROMOTION_POLICY_VERSION
        if accepted_capture is not None
        and accepted_capture["record_promotion_contract_id"] == MERCOR_RECORD_CONTRACT_ID
        else SOURCE_PROMOTION_POLICY_VERSION
    )
    if acceptance is not None and accepted_capture is not None:
        if accepted_capture["capture_contract_version"] != SOURCE_CAPTURE_CONTRACT_VERSION:
            evidence_stale_reasons.append(EVIDENCE_REASON_CAPTURE_CONTRACT_CHANGED)
        if (
            acceptance["promotion_policy_version"]
            != current_promotion_policy_version
            or accepted_capture["promotion_policy_version"]
            != current_promotion_policy_version
        ):
            evidence_stale_reasons.append(EVIDENCE_REASON_PROMOTION_POLICY_CHANGED)
        if latest is None or latest["id"] != acceptance["accepted_capture_id"]:
            evidence_stale_reasons.append(EVIDENCE_REASON_LATEST_CAPTURE_NOT_ACCEPTED)
        if later_failed_observation:
            evidence_stale_reasons.append(EVIDENCE_REASON_LATEST_SOURCE_ATTEMPT_FAILED)
    elif accepted is not None and latest is not None:
        evidence_stale_reasons.append(EVIDENCE_REASON_LATEST_CAPTURE_NOT_ACCEPTED)

    if accepted is None:
        state = (
            EVIDENCE_STATE_DEGRADED_LATEST
            if latest is not None
            else EVIDENCE_STATE_MISSING
        )
        accepted_kind = "missing"
    elif acceptance is None:
        state = (
            EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD
            if latest is not None
            else EVIDENCE_STATE_LEGACY_ACCEPTED
        )
        accepted_kind = "legacy"
    else:
        state = (
            EVIDENCE_STATE_ACCEPTED_CURRENT
            if not evidence_stale_reasons
            else EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD
        )
        accepted_kind = "versioned"

    return {
        "evidence_version": SOURCE_CAPTURE_EVIDENCE_VERSION,
        "job_id": int(job_id),
        "state": state,
        "accepted_kind": accepted_kind,
        "accepted_capture_id": (
            acceptance["accepted_capture_id"] if acceptance is not None else None
        ),
        "accepted_material_content_sha256": (
            accepted["material_content_sha256"] if accepted is not None else None
        ),
        "accepted_semantic_material_sha256": accepted_semantic_hash,
        "accepted_at": (
            acceptance["accepted_at"] if acceptance is not None else None
        ),
        "stored_capture_contract_version": (
            accepted_capture["capture_contract_version"]
            if accepted_capture is not None
            else None
        ),
        "stored_promotion_policy_version": (
            acceptance["promotion_policy_version"]
            if acceptance is not None
            else None
        ),
        "current_capture_contract_version": SOURCE_CAPTURE_CONTRACT_VERSION,
        "current_promotion_policy_version": current_promotion_policy_version,
        "stale_reasons": evidence_stale_reasons,
        "last_confirmed_at": (
            acceptance["last_confirmed_at"] if acceptance is not None else None
        ),
        "latest_capture_id": latest["id"] if latest is not None else None,
        "latest_material_content_sha256": (
            latest["material_content_sha256"] if latest is not None else None
        ),
        "latest_semantic_material_sha256": (
            latest["semantic_material_sha256"] if latest is not None else None
        ),
        "latest_capture_quality": (
            latest["capture_quality"] if latest is not None else None
        ),
        "latest_promotion_decision": (
            latest["promotion_decision"] if latest is not None else None
        ),
        "latest_decision_reasons": (
            json.loads(latest["decision_reasons_json"])
            if latest is not None
            else []
        ),
        "latest_observed_at": latest["observed_at"] if latest is not None else None,
        "latest_crawl_run_id": (
            latest_crawl_run["id"] if latest_crawl_run is not None else None
        ),
        "latest_crawl_run_status": (
            latest_crawl_run["status"] if latest_crawl_run is not None else None
        ),
    }


def _verify_stored_source_material(row, label):
    try:
        metadata = json.loads(row["metadata_json"])
        if type(metadata) is not dict:
            raise ValueError("metadata is not an object")
        canonical_metadata_json = canonical_source_metadata_json(metadata)
        normalized_body = normalize_source_body(row["body"])
        if row["body"] != normalized_body:
            raise ValueError("body is not canonically normalized")
        if row["metadata_json"] != canonical_metadata_json:
            raise ValueError("metadata is not canonically serialized")
        if (normalized_body is None) != (row["body_format"] is None):
            raise ValueError("body format does not match body presence")
        recomputed_hash = source_material_content_sha256(
            normalized_body,
            row["body_format"],
            metadata,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is inconsistent.") from exc
    if recomputed_hash != row["material_content_sha256"]:
        raise RuntimeError(f"{label} is inconsistent.")
    return recomputed_hash, metadata


def _semantic_material_hash_from_rows(job, source, metadata):
    return semantic_source_material_sha256(
        semantic_job_fields_from_row(job),
        provider=source["provider"],
        source_type=source["source_type"],
        source_url=source["source_url"],
        source_external_id=source["external_id"],
        body=source["body"],
        body_format=source["body_format"],
        metadata=metadata,
    )


def _capture_context_from_row(row):
    boolean_fields = (
        "used_sample_data",
        "snapshot_complete",
        "pagination_complete",
        "empty_snapshot_validated",
    )
    count_fields = (
        "raw_record_count",
        "normalized_record_count",
        "candidate_count",
        "rejected_record_count",
    )
    if any(
        type(row[name]) is not int or row[name] not in {0, 1}
        for name in boolean_fields
    ):
        raise RuntimeError("Accepted source capture provenance is inconsistent.")
    if any(type(row[name]) is not int or row[name] < 0 for name in count_fields):
        raise RuntimeError("Accepted source capture provenance is inconsistent.")
    return SourceCaptureContext(
        crawl_run_id=row["crawl_run_id"],
        provider_outcome=row["provider_outcome"],
        used_sample_data=bool(row["used_sample_data"]),
        snapshot_complete=bool(row["snapshot_complete"]),
        pagination_complete=bool(row["pagination_complete"]),
        empty_snapshot_validated=bool(row["empty_snapshot_validated"]),
        raw_record_count=row["raw_record_count"],
        normalized_record_count=row["normalized_record_count"],
        candidate_count=row["candidate_count"],
        rejected_record_count=row["rejected_record_count"],
        payload_shape=row["payload_shape"],
        schema_fingerprint=row["schema_fingerprint"],
    )


def _prepared_record_attestation_from_row(row):
    return PreparedRecordPromotionAttestation(
        contract_id=row["record_promotion_contract_id"],
        body_observation=row["body_observation"],
        authority_evidence_json=row["authority_evidence_json"],
    )


def _expected_crawl_run_status(context):
    if context.provider_outcome == "contract_drift":
        return "contract_drift"
    if context.used_sample_data:
        return "success"
    if not context.non_authoritative_reasons() and (
        context.candidate_count > 0 or context.empty_snapshot_validated
    ):
        return "success"
    return "partial"


def _verify_capture_crawl_provenance(conn, job, row, context):
    if row["crawl_run_id"] is None:
        return
    crawl_run = conn.execute(
        """
        SELECT company_id, status, started_at, finished_at, used_sample_data
        FROM crawl_runs
        WHERE id = ?
        """,
        (row["crawl_run_id"],),
    ).fetchone()
    if crawl_run is None or crawl_run["company_id"] != job["company_id"]:
        raise RuntimeError("Accepted source capture provenance is inconsistent.")

    started_status, started_at = parse_source_timestamp(crawl_run["started_at"])
    observed_status, observed_at = parse_source_timestamp(row["observed_at"])
    if (
        started_status != "valid"
        or observed_status != "valid"
        or observed_at < started_at
    ):
        raise RuntimeError("Accepted source capture provenance is inconsistent.")

    if crawl_run["status"] == "running":
        if crawl_run["finished_at"] is not None:
            raise RuntimeError("Accepted source capture provenance is inconsistent.")
        return

    finished_status, finished_at = parse_source_timestamp(crawl_run["finished_at"])
    if (
        finished_status != "valid"
        or observed_at > finished_at
        or crawl_run["status"] != _expected_crawl_run_status(context)
        or bool(crawl_run["used_sample_data"]) != context.used_sample_data
    ):
        raise RuntimeError("Accepted source capture provenance is inconsistent.")


def _prepared_capture_from_row(row, metadata):
    preparer = SOURCE_CAPTURE_CONTRACT_PREPARERS.get(
        row["capture_contract_version"]
    )
    if preparer is None:
        raise RuntimeError("Accepted source capture provenance is inconsistent.")
    try:
        prepared = preparer(
            SimpleNamespace(
                source_body=row["body"],
                source_body_format=row["body_format"],
                source_metadata=metadata,
                source_updated_at=row["source_updated_at"],
            )
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Accepted source capture provenance is inconsistent."
        ) from exc
    if (
        prepared.body != row["body"]
        or prepared.body_format != row["body_format"]
        or prepared.metadata_json != row["metadata_json"]
        or prepared.material_content_sha256 != row["material_content_sha256"]
        or prepared.source_updated_at != row["source_updated_at"]
        or prepared.source_timestamp_status != row["source_timestamp_status"]
        or prepared.quality != row["capture_quality"]
    ):
        raise RuntimeError("Accepted source capture provenance is inconsistent.")
    return prepared


def _validated_record_attestation_from_row(row, prepared, context):
    try:
        captured_job_fields = json.loads(row["semantic_job_fields_json"])
        return prepare_stored_record_promotion_attestation(
            contract_id=row["record_promotion_contract_id"],
            body_observation=row["body_observation"],
            authority_evidence_json=row["authority_evidence_json"],
            candidate=SimpleNamespace(
                external_id=row["external_id"],
                title=captured_job_fields["title"],
                url=row["source_url"],
            ),
            prepared=prepared,
            context=context,
            provider=row["provider"],
            source_type=row["source_type"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Accepted source capture provenance is inconsistent."
        ) from exc


def _captured_semantic_material(job, row, metadata):
    try:
        captured_job_fields = json.loads(row["semantic_job_fields_json"])
        if (
            canonical_semantic_job_fields_json(captured_job_fields)
            != row["semantic_job_fields_json"]
            or row["provider"] != job["company_slug"]
            or row["external_id"] != captured_job_fields["external_id"]
            or row["source_url"] != captured_job_fields["url"]
            or captured_job_fields["external_id"] != job["external_id"]
            or captured_job_fields["source_hash"] != job["source_hash"]
        ):
            raise ValueError("capture identity fields disagree")
        semantic_hash = semantic_source_material_sha256(
            captured_job_fields,
            provider=row["provider"],
            source_type=row["source_type"],
            source_url=row["source_url"],
            source_external_id=row["external_id"],
            body=row["body"],
            body_format=row["body_format"],
            metadata=metadata,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Accepted source capture provenance is inconsistent."
        ) from exc
    if semantic_hash != row["semantic_material_sha256"]:
        raise RuntimeError("Accepted source capture provenance is inconsistent.")
    return semantic_hash


def _stored_decision_reasons(row):
    try:
        reasons = json.loads(row["decision_reasons_json"])
        if type(reasons) is not list or any(
            type(reason) is not str for reason in reasons
        ):
            raise ValueError("decision reasons are not a string list")
        canonical = json.dumps(reasons, ensure_ascii=False, separators=(",", ":"))
        if canonical != row["decision_reasons_json"]:
            raise ValueError("decision reasons are not canonical")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Accepted source capture provenance is inconsistent."
        ) from exc
    return tuple(reasons)


def _replay_source_capture_history(conn, job, accepted_capture_id):
    captures = conn.execute(
        """
        SELECT *
        FROM job_source_content_captures
        WHERE job_id = ? AND id <= ?
        ORDER BY id
        """,
        (job["id"], accepted_capture_id),
    ).fetchall()
    if not captures or captures[-1]["id"] != accepted_capture_id:
        raise RuntimeError("Accepted source capture provenance is inconsistent.")

    accepted_state = None
    for row in captures:
        _material_hash, metadata = _verify_stored_source_material(
            row,
            "Accepted source capture history",
        )
        prepared = _prepared_capture_from_row(row, metadata)
        semantic_hash = _captured_semantic_material(job, row, metadata)
        context = _capture_context_from_row(row)
        record_attestation = _validated_record_attestation_from_row(
            row,
            prepared,
            context,
        )
        _verify_capture_crawl_provenance(conn, job, row, context)
        decider = SOURCE_PROMOTION_POLICY_DECIDERS.get(
            row["promotion_policy_version"]
        )
        if decider is None:
            raise RuntimeError("Accepted source capture provenance is inconsistent.")
        accepted_row = None
        if accepted_state is not None:
            accepted_row = {
                "body": accepted_state["body"],
                "material_content_sha256": accepted_state[
                    "material_content_sha256"
                ],
                "source_updated_at": accepted_state["source_updated_at"],
            }
        decision = decider(
            prepared,
            context,
            accepted_row,
            same_accepted_semantic_material=(
                accepted_state is not None
                and accepted_state["semantic_material_sha256"] == semantic_hash
            ),
            record_attestation=record_attestation,
            accepted_record_attestation=(
                accepted_state["record_attestation"]
                if accepted_state is not None
                else None
            ),
        )
        if (
            decision.decision != row["promotion_decision"]
            or decision.reasons != _stored_decision_reasons(row)
        ):
            raise RuntimeError("Accepted source capture provenance is inconsistent.")
        if decision.accepted:
            if decision.decision == PROMOTION_DECISION_CONFIRMED:
                if accepted_state is None:
                    raise RuntimeError(
                        "Accepted source capture provenance is inconsistent."
                    )
                accepted_at = accepted_state["accepted_at"]
            else:
                accepted_at = row["observed_at"]
            accepted_state = {
                "capture": row,
                "body": prepared.body,
                "material_content_sha256": prepared.material_content_sha256,
                "semantic_material_sha256": semantic_hash,
                "source_updated_at": decision.accepted_source_updated_at,
                "accepted_at": accepted_at,
                "last_confirmed_at": row["observed_at"],
                "record_attestation": record_attestation,
            }

    if (
        accepted_state is None
        or accepted_state["capture"]["id"] != accepted_capture_id
    ):
        raise RuntimeError("Accepted source capture provenance is inconsistent.")
    return accepted_state


def verify_job_source_acceptance_integrity(conn, job_id):
    """Fail closed if versioned accepted semantic material has diverged."""

    job = conn.execute(
        """
        SELECT j.*, c.slug AS company_slug
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if job is None:
        raise RuntimeError(f"Unknown job id: {job_id}")
    accepted = conn.execute(
        "SELECT * FROM job_source_contents WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    acceptance = conn.execute(
        """
        SELECT accepted_capture_id, promotion_policy_version,
               accepted_at, last_confirmed_at
        FROM job_source_content_acceptances
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    return _verify_source_acceptance(
        conn,
        job_id,
        accepted,
        acceptance,
        job=job,
    )


def _verify_source_acceptance(conn, job_id, accepted, acceptance, *, job):
    if accepted is None:
        if acceptance is not None:
            raise RuntimeError("Source acceptance exists without accepted content.")
        if job["semantic_authority_state"] == SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED:
            raise RuntimeError("Versioned semantic authority lacks accepted content.")
        return None, None
    if job["semantic_authority_state"] == SEMANTIC_AUTHORITY_PENDING:
        raise RuntimeError("Pending semantic authority has accepted content.")

    _accepted_material_hash, accepted_metadata = _verify_stored_source_material(
        accepted,
        "Accepted source materialization",
    )
    accepted_semantic_hash = _semantic_material_hash_from_rows(
        job,
        accepted,
        accepted_metadata,
    )
    if (
        accepted["provider"] != job["company_slug"]
        or accepted["external_id"] != job["external_id"]
        or accepted["source_url"] != job["url"]
    ):
        raise RuntimeError("Accepted source materialization is inconsistent.")
    if acceptance is None:
        if job["semantic_authority_state"] != SEMANTIC_AUTHORITY_LEGACY_ACCEPTED:
            raise RuntimeError("Accepted source provenance is inconsistent.")
        return None, accepted_semantic_hash

    replayed = _replay_source_capture_history(
        conn,
        job,
        acceptance["accepted_capture_id"],
    )
    accepted_capture = replayed["capture"]
    captured_semantic_hash = replayed["semantic_material_sha256"]
    if (
        job["semantic_authority_state"]
        != SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED
        or accepted_semantic_hash != captured_semantic_hash
        or accepted["source_updated_at"] != replayed["source_updated_at"]
        or accepted["last_captured_at"] != accepted_capture["observed_at"]
        or accepted_capture["promotion_policy_version"]
        != acceptance["promotion_policy_version"]
        or acceptance["accepted_at"] != replayed["accepted_at"]
        or acceptance["last_confirmed_at"] != replayed["last_confirmed_at"]
    ):
        raise RuntimeError("Accepted source capture provenance is inconsistent.")
    return accepted_capture, accepted_semantic_hash


def _validate_capture_identity_and_crawl_run(
    conn,
    job_id,
    candidate,
    crawl_run_id,
    provider,
):
    job = conn.execute(
        """
        SELECT j.*, c.slug AS company_slug
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if job is None:
        raise RuntimeError(f"Unknown job id: {job_id}")
    if (
        candidate.source_hash != job["source_hash"]
        or candidate.external_id != job["external_id"]
        or provider != job["company_slug"]
    ):
        raise ValueError("Capture candidate identity does not match the job.")
    if crawl_run_id is None:
        return job
    crawl_run = conn.execute(
        "SELECT company_id FROM crawl_runs WHERE id = ?",
        (crawl_run_id,),
    ).fetchone()
    if crawl_run is None or crawl_run["company_id"] != job["company_id"]:
        raise ValueError("Capture crawl run does not belong to the job's company.")
    return job


def resolve_job_classification(conn, company_id, candidate):
    company = conn.execute(
        """
        SELECT inventory_model, market_count_policy
        FROM companies
        WHERE id = ?
        """,
        (company_id,),
    ).fetchone()
    if company is None:
        raise RuntimeError(f"Unknown company id: {company_id}")

    opportunity_kind = (
        candidate.opportunity_kind
        or default_opportunity_kind_for_inventory_model(company["inventory_model"])
    )
    availability_basis = (
        candidate.availability_basis
        or default_availability_basis_for_inventory_model(company["inventory_model"])
    )
    if candidate.include_in_live_market_estimate is None:
        include_in_live_market_estimate = include_in_live_market_estimate_for_policy(
            company["market_count_policy"]
        )
    else:
        include_in_live_market_estimate = int(candidate.include_in_live_market_estimate)

    return {
        "opportunity_kind": opportunity_kind,
        "availability_basis": availability_basis,
        "include_in_live_market_estimate": include_in_live_market_estimate,
    }


def create_job_event(conn, job_id, crawl_run_id, event_type, created_at):
    conn.execute(
        """
        INSERT INTO job_events (job_id, crawl_run_id, event_type, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (job_id, crawl_run_id, event_type, created_at),
    )


def mark_missing_jobs_inactive(conn, company_id, seen_hashes, now):
    jobs_to_remove = get_missing_active_jobs(conn, company_id, seen_hashes)
    if not jobs_to_remove:
        return []

    job_ids = [row["id"] for row in jobs_to_remove]
    placeholders = ",".join("?" for _ in job_ids)
    conn.execute(
        f"""
        UPDATE jobs
        SET is_active = 0,
            removed_at = ?,
            updated_at = ?
        WHERE id IN ({placeholders})
        """,
        [now, now, *job_ids],
    )
    return job_ids


def get_missing_active_jobs(conn, company_id, seen_hashes):
    if seen_hashes:
        placeholders = ",".join("?" for _ in seen_hashes)
        return conn.execute(
            f"""
            SELECT id
            FROM jobs
            WHERE company_id = ?
              AND is_active = 1
              AND source_hash NOT IN ({placeholders})
            """,
            [company_id, *seen_hashes],
        ).fetchall()

    return conn.execute(
        """
        SELECT id
        FROM jobs
        WHERE company_id = ?
          AND is_active = 1
        """,
        (company_id,),
    ).fetchall()


def count_active_jobs(conn, company_id):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE company_id = ? AND is_active = 1
        """,
        (company_id,),
    ).fetchone()
    return row["count"]


def get_last_successful_crawl(conn, company_id):
    return conn.execute(
        """
        SELECT *
        FROM crawl_runs
        WHERE company_id = ? AND status = 'success'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
