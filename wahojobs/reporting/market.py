EXPERIMENTAL_SLUGS = ("invisible",)


def get_market_size_summary(conn, include_experimental=False, include_simulation=False):
    raw_active_postings = count_raw_active_postings(
        conn,
        include_experimental=include_experimental,
        include_simulation=include_simulation,
    )
    alignerr_raw_postings = count_alignerr_raw_postings(
        conn,
        include_simulation=include_simulation,
    )
    alignerr_canonical_opportunities = count_alignerr_canonical_opportunities(conn)
    non_canonical_raw_jobs = count_non_canonical_raw_jobs(
        conn,
        include_experimental=include_experimental,
        include_simulation=include_simulation,
    )

    return {
        "raw_active_postings": raw_active_postings,
        "estimated_market_opportunities": (
            alignerr_canonical_opportunities + non_canonical_raw_jobs
        ),
        "alignerr_raw_postings": alignerr_raw_postings,
        "alignerr_canonical_opportunities": alignerr_canonical_opportunities,
        "alignerr_posting_variants": (
            alignerr_raw_postings - alignerr_canonical_opportunities
        ),
    }


def count_raw_active_postings(conn, include_experimental=False, include_simulation=False):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {simulation_filter("j", include_simulation)}
          {experimental_filter("c", include_experimental)}
        """
    ).fetchone()
    return row["count"]


def count_alignerr_raw_postings(conn, include_simulation=False):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE c.slug = 'alignerr'
          AND j.is_active = 1
          {simulation_filter("j", include_simulation)}
        """
    ).fetchone()
    return row["count"]


def count_alignerr_canonical_opportunities(conn):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE c.slug = 'alignerr'
          AND co.is_active = 1
        """
    ).fetchone()
    return row["count"]


def count_non_canonical_raw_jobs(conn, include_experimental=False, include_simulation=False):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND c.slug != 'alignerr'
          {simulation_filter("j", include_simulation)}
          {experimental_filter("c", include_experimental)}
        """
    ).fetchone()
    return row["count"]


def simulation_filter(alias, include_simulation):
    if include_simulation:
        return ""
    return f"AND {alias}.title NOT LIKE '[SIMULATION]%'"


def experimental_filter(alias, include_experimental):
    if include_experimental:
        return ""
    slugs = ",".join(f"'{slug}'" for slug in EXPERIMENTAL_SLUGS)
    return f"AND {alias}.slug NOT IN ({slugs})"
