EXPERIMENTAL_SLUGS = ("invisible",)
CANONICALIZED_SLUGS = ("alignerr", "dataforce", "meridial", "oneforma", "welocalize")


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
    dataforce_raw_postings = count_company_raw_postings(
        conn,
        "dataforce",
        include_simulation=include_simulation,
    )
    dataforce_canonical_opportunities = count_company_canonical_opportunities(
        conn,
        "dataforce",
    )
    meridial_raw_postings = count_company_raw_postings(
        conn,
        "meridial",
        include_simulation=include_simulation,
    )
    meridial_canonical_opportunities = count_company_canonical_opportunities(
        conn,
        "meridial",
    )
    oneforma_raw_postings = count_company_raw_postings(
        conn,
        "oneforma",
        include_simulation=include_simulation,
    )
    oneforma_canonical_opportunities = count_company_canonical_opportunities(
        conn,
        "oneforma",
    )
    welocalize_raw_postings = count_company_raw_postings(
        conn,
        "welocalize",
        include_simulation=include_simulation,
    )
    welocalize_canonical_opportunities = count_company_canonical_opportunities(
        conn,
        "welocalize",
    )
    canonical_opportunities = count_canonical_opportunities(conn)
    non_canonical_raw_jobs = count_non_canonical_raw_jobs(
        conn,
        include_experimental=include_experimental,
        include_simulation=include_simulation,
    )

    return {
        "raw_active_postings": raw_active_postings,
        "estimated_market_opportunities": (
            canonical_opportunities + non_canonical_raw_jobs
        ),
        "alignerr_raw_postings": alignerr_raw_postings,
        "alignerr_canonical_opportunities": alignerr_canonical_opportunities,
        "alignerr_posting_variants": (
            alignerr_raw_postings - alignerr_canonical_opportunities
        ),
        "dataforce_raw_postings": dataforce_raw_postings,
        "dataforce_canonical_opportunities": dataforce_canonical_opportunities,
        "dataforce_posting_variants": (
            dataforce_raw_postings - dataforce_canonical_opportunities
        ),
        "meridial_raw_postings": meridial_raw_postings,
        "meridial_canonical_opportunities": meridial_canonical_opportunities,
        "meridial_posting_variants": (
            meridial_raw_postings - meridial_canonical_opportunities
        ),
        "oneforma_raw_variants": oneforma_raw_postings,
        "oneforma_canonical_opportunities": oneforma_canonical_opportunities,
        "oneforma_posting_variants": (
            oneforma_raw_postings - oneforma_canonical_opportunities
        ),
        "welocalize_raw_postings": welocalize_raw_postings,
        "welocalize_canonical_opportunities": welocalize_canonical_opportunities,
        "welocalize_posting_variants": (
            welocalize_raw_postings - welocalize_canonical_opportunities
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
    return count_company_raw_postings(conn, "alignerr", include_simulation)


def count_company_raw_postings(conn, slug, include_simulation=False):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE c.slug = ?
          AND j.is_active = 1
          {simulation_filter("j", include_simulation)}
        """,
        (slug,),
    ).fetchone()
    return row["count"]


def count_alignerr_canonical_opportunities(conn):
    return count_company_canonical_opportunities(conn, "alignerr")


def count_company_canonical_opportunities(conn, slug):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE c.slug = ?
          AND co.is_active = 1
        """,
        (slug,),
    ).fetchone()
    return row["count"]


def count_canonical_opportunities(conn):
    slugs = ",".join(f"'{slug}'" for slug in CANONICALIZED_SLUGS)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE c.slug IN ({slugs})
          AND co.is_active = 1
        """
    ).fetchone()
    return row["count"]


def count_non_canonical_raw_jobs(conn, include_experimental=False, include_simulation=False):
    canonical_slugs = ",".join(f"'{slug}'" for slug in CANONICALIZED_SLUGS)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND c.slug NOT IN ({canonical_slugs})
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
