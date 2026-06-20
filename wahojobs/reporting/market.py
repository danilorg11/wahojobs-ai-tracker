from wahojobs.classification import (
    MARKET_COUNT_POLICY_COUNT_LIVE,
    SOURCE_TIER_EXPERIMENTAL,
)


CANONICALIZED_SLUGS = (
    "alignerr",
    "dataforce",
    "meridial",
    "micro1",
    "mindrift",
    "oneforma",
    "turing",
    "welocalize",
)


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
    mindrift_raw_postings = count_company_raw_postings(
        conn,
        "mindrift",
        include_simulation=include_simulation,
    )
    mindrift_canonical_opportunities = count_company_canonical_opportunities(
        conn,
        "mindrift",
    )
    micro1_raw_postings = count_company_raw_postings(
        conn,
        "micro1",
        include_simulation=include_simulation,
    )
    micro1_canonical_opportunities = count_company_canonical_opportunities(
        conn,
        "micro1",
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
    turing_raw_postings = count_company_raw_postings(
        conn,
        "turing",
        include_simulation=include_simulation,
    )
    turing_canonical_opportunities = count_company_canonical_opportunities(
        conn,
        "turing",
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
    canonical_opportunities = count_canonical_opportunities(
        conn,
        include_experimental=include_experimental,
    )
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
        "mindrift_raw_postings": mindrift_raw_postings,
        "mindrift_canonical_opportunities": mindrift_canonical_opportunities,
        "mindrift_posting_variants": (
            mindrift_raw_postings - mindrift_canonical_opportunities
        ),
        "micro1_raw_postings": micro1_raw_postings,
        "micro1_canonical_opportunities": micro1_canonical_opportunities,
        "micro1_posting_variants": (
            micro1_raw_postings - micro1_canonical_opportunities
        ),
        "oneforma_raw_variants": oneforma_raw_postings,
        "oneforma_canonical_opportunities": oneforma_canonical_opportunities,
        "oneforma_posting_variants": (
            oneforma_raw_postings - oneforma_canonical_opportunities
        ),
        "turing_raw_postings": turing_raw_postings,
        "turing_canonical_opportunities": turing_canonical_opportunities,
        "turing_posting_variants": (
            turing_raw_postings - turing_canonical_opportunities
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
          {live_market_filter("c", "j")}
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


def count_canonical_opportunities(conn, include_experimental=False):
    slugs = ",".join(f"'{slug}'" for slug in CANONICALIZED_SLUGS)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE c.slug IN ({slugs})
          AND co.is_active = 1
          {experimental_filter("c", include_experimental)}
          AND c.market_count_policy = '{MARKET_COUNT_POLICY_COUNT_LIVE}'
          AND EXISTS (
            SELECT 1
            FROM jobs j
            WHERE j.canonical_opportunity_id = co.id
              AND j.is_active = 1
              AND j.include_in_live_market_estimate = 1
          )
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
          {live_market_filter("c", "j")}
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
    return f"AND {alias}.source_tier != '{SOURCE_TIER_EXPERIMENTAL}'"


def live_market_filter(company_alias, job_alias):
    return (
        f"AND {company_alias}.market_count_policy = '{MARKET_COUNT_POLICY_COUNT_LIVE}' "
        f"AND {job_alias}.include_in_live_market_estimate = 1"
    )


def company_label(alias):
    return (
        f"CASE WHEN {alias}.source_tier = '{SOURCE_TIER_EXPERIMENTAL}' "
        f"THEN {alias}.name || ' [EXPERIMENTAL]' ELSE {alias}.name END"
    )


def experimental_sources_status(include_experimental):
    if include_experimental:
        return "visible, excluded from live estimate by policy"
    return "excluded"


def get_classification_summary(
    conn,
    include_experimental=False,
    include_simulation=False,
):
    return {
        "source_tiers": group_sources_by_classification(
            conn,
            "source_tier",
            include_experimental,
        ),
        "inventory_models": group_active_jobs_by_source_classification(
            conn,
            "inventory_model",
            include_experimental,
            include_simulation,
        ),
        "market_count_policies": group_active_jobs_by_source_classification(
            conn,
            "market_count_policy",
            include_experimental,
            include_simulation,
        ),
        "opportunity_kinds": group_active_jobs_by_job_classification(
            conn,
            "opportunity_kind",
            include_experimental,
            include_simulation,
        ),
        "availability_basis": group_active_jobs_by_job_classification(
            conn,
            "availability_basis",
            include_experimental,
            include_simulation,
        ),
    }


def group_sources_by_classification(conn, field, include_experimental):
    return conn.execute(
        f"""
        SELECT {field} AS label, COUNT(*) AS count
        FROM companies c
        WHERE 1 = 1
          {experimental_filter("c", include_experimental)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def group_active_jobs_by_source_classification(
    conn,
    field,
    include_experimental,
    include_simulation,
):
    return conn.execute(
        f"""
        SELECT c.{field} AS label, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {simulation_filter("j", include_simulation)}
          {experimental_filter("c", include_experimental)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def group_active_jobs_by_job_classification(
    conn,
    field,
    include_experimental,
    include_simulation,
):
    return conn.execute(
        f"""
        SELECT j.{field} AS label, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {simulation_filter("j", include_simulation)}
          {experimental_filter("c", include_experimental)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def quote_list(values):
    return ",".join(f"'{value}'" for value in values)
