from pathlib import Path

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
from wahojobs.canonical.service import (
    sync_alignerr_canonical_opportunities,
    sync_dataforce_canonical_opportunities,
    sync_meridial_canonical_opportunities,
    sync_mindrift_canonical_opportunities,
    sync_oneforma_canonical_opportunities,
    sync_turing_canonical_opportunities,
    sync_welocalize_canonical_opportunities,
)
from wahojobs.db.connection import get_connection


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
    schema_path = Path(__file__).with_name("schema.sql")
    with get_connection(db_path) as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        ensure_company_classification_columns(conn)
        ensure_job_optional_columns(conn)
        ensure_job_classification_columns(conn)
        ensure_canonical_schema(conn)
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
        oneforma = get_company_by_slug(conn, "oneforma")
        if oneforma is not None:
            sync_oneforma_canonical_opportunities(conn, oneforma["id"])
        turing = get_company_by_slug(conn, "turing")
        if turing is not None:
            sync_turing_canonical_opportunities(conn, turing["id"])
        welocalize = get_company_by_slug(conn, "welocalize")
        if welocalize is not None:
            sync_welocalize_canonical_opportunities(conn, welocalize["id"])


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


def finish_crawl_run(conn, crawl_run_id, summary, finished_at):
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = 'success',
            finished_at = ?,
            jobs_found_count = ?,
            jobs_new_count = ?,
            jobs_reactivated_count = ?,
            jobs_updated_count = ?,
            jobs_removed_count = ?,
            used_sample_data = ?,
            error_message = NULL
        WHERE id = ?
        """,
        (
            finished_at,
            summary.jobs_found,
            summary.jobs_new,
            summary.jobs_reactivated,
            summary.jobs_updated,
            summary.jobs_removed,
            int(summary.used_sample_data),
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


def insert_job(conn, company_id, candidate, now):
    classification = resolve_job_classification(conn, company_id, candidate)
    cursor = conn.execute(
        """
        INSERT INTO jobs (
          company_id, external_id, title, location, department, expertise, commitment, url, source_hash,
          opportunity_kind, availability_basis, include_in_live_market_estimate,
          first_seen_at, last_seen_at, is_active, removed_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
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
            now,
            now,
            now,
        ),
    )
    return cursor.lastrowid


def update_seen_job(conn, job_id, candidate, now):
    existing = conn.execute(
        "SELECT company_id FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if existing is None:
        raise RuntimeError(f"Unknown job id: {job_id}")
    classification = resolve_job_classification(conn, existing["company_id"], candidate)
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
            last_seen_at = ?,
            is_active = 1,
            removed_at = NULL,
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
            now,
            now,
            job_id,
        ),
    )


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
