from wahojobs.canonical.alignerr import canonicalize_job
from wahojobs.canonical.oneforma import canonicalize_job as canonicalize_oneforma_job
from wahojobs.canonical.welocalize import canonicalize_job as canonicalize_welocalize_job


def sync_alignerr_canonical_opportunities(conn, company_id):
    sync_canonical_opportunities(conn, company_id, canonicalize_alignerr_row)


def sync_oneforma_canonical_opportunities(conn, company_id):
    sync_canonical_opportunities(conn, company_id, canonicalize_oneforma_job)


def sync_welocalize_canonical_opportunities(conn, company_id):
    sync_canonical_opportunities(conn, company_id, canonicalize_welocalize_job)


def sync_canonical_opportunities(conn, company_id, canonicalizer):
    rows = conn.execute(
        """
        SELECT *
        FROM jobs
        WHERE company_id = ?
          AND title NOT LIKE '[SIMULATION]%'
        ORDER BY first_seen_at ASC, id ASC
        """,
        (company_id,),
    ).fetchall()

    for row in rows:
        canonical = canonicalizer(row)
        canonical_id = upsert_canonical_opportunity(conn, company_id, canonical, row)
        conn.execute(
            """
            UPDATE jobs
            SET canonical_opportunity_id = ?
            WHERE id = ?
            """,
            (canonical_id, row["id"]),
        )

    refresh_canonical_rollups(conn, company_id)


def canonicalize_alignerr_row(row):
    source_category = row["expertise"] or row["department"] or "Unknown"
    return canonicalize_job(row["title"], source_category)


def upsert_canonical_opportunity(conn, company_id, canonical, job):
    existing = conn.execute(
        """
        SELECT id
        FROM canonical_opportunities
        WHERE company_id = ?
          AND canonical_key = ?
        """,
        (company_id, canonical["canonical_key"]),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE canonical_opportunities
            SET canonical_title = ?,
                normalized_title = ?,
                source_category = ?,
                language = ?,
                language_locale = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                canonical["canonical_title"],
                canonical["normalized_title"],
                canonical["source_category"],
                canonical["language"],
                canonical["language_locale"],
                existing["id"],
            ),
        )
        return existing["id"]

    cursor = conn.execute(
        """
        INSERT INTO canonical_opportunities (
          company_id, canonical_key, canonical_title, normalized_title,
          source_category, language, language_locale, first_seen_at, last_seen_at,
          is_active, variant_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        """,
        (
            company_id,
            canonical["canonical_key"],
            canonical["canonical_title"],
            canonical["normalized_title"],
            canonical["source_category"],
            canonical["language"],
            canonical["language_locale"],
            job["first_seen_at"],
            job["last_seen_at"],
        ),
    )
    return cursor.lastrowid


def refresh_canonical_rollups(conn, company_id):
    conn.execute(
        """
        UPDATE canonical_opportunities
        SET first_seen_at = (
              SELECT MIN(j.first_seen_at)
              FROM jobs j
              WHERE j.canonical_opportunity_id = canonical_opportunities.id
            ),
            last_seen_at = (
              SELECT MAX(j.last_seen_at)
              FROM jobs j
              WHERE j.canonical_opportunity_id = canonical_opportunities.id
            ),
            is_active = CASE WHEN EXISTS (
              SELECT 1
              FROM jobs j
              WHERE j.canonical_opportunity_id = canonical_opportunities.id
                AND j.is_active = 1
            ) THEN 1 ELSE 0 END,
            variant_count = (
              SELECT COUNT(*)
              FROM jobs j
              WHERE j.canonical_opportunity_id = canonical_opportunities.id
                AND j.is_active = 1
            ),
            updated_at = CURRENT_TIMESTAMP
        WHERE company_id = ?
          AND EXISTS (
            SELECT 1
            FROM jobs j
            WHERE j.canonical_opportunity_id = canonical_opportunities.id
          )
        """,
        (company_id,),
    )
    conn.execute(
        """
        UPDATE canonical_opportunities
        SET is_active = 0,
            variant_count = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE company_id = ?
          AND NOT EXISTS (
            SELECT 1
            FROM jobs j
            WHERE j.canonical_opportunity_id = canonical_opportunities.id
          )
        """,
        (company_id,),
    )
