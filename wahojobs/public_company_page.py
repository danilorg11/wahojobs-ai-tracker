"""Candidate-facing public company pages backed by trusted job inventory."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode

from wahojobs import public_job_page, public_jobs_catalog


PUBLIC_COMPANY_PATH = re.compile(
    r"^/company/(?P<company>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
PAGE_VALUE = re.compile(r"^[1-9][0-9]{0,3}$")


def parse_public_company_path(path):
    if not isinstance(path, str):
        return None
    match = PUBLIC_COMPANY_PATH.fullmatch(path)
    return match.group("company") if match is not None else None


def parse_company_query(query):
    if not public_jobs_catalog.valid_query_encoding(query):
        return None
    try:
        raw = (
            parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=1,
            )
            if query
            else {}
        )
    except (UnicodeError, ValueError):
        return None
    if set(raw) - {"page"} or any(len(values) != 1 for values in raw.values()):
        return None
    if not raw:
        return {"page": 1, "query_present": False, "raw_query": ""}
    page = raw["page"][0]
    if PAGE_VALUE.fullmatch(page) is None:
        return None
    return {"page": int(page), "query_present": True, "raw_query": query}


def load_public_company_identity(connection, path):
    """Load a company only when repository history proves public eligibility."""

    slug = parse_public_company_path(path)
    if slug is None:
        return None
    rows = connection.execute(
        """
        SELECT
          c.id AS company_id,
          c.name AS company_name,
          c.slug AS company_slug,
          c.careers_url,
          c.source_tier,
          c.inventory_model,
          c.market_count_policy,
          j.canonical_opportunity_id,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate
        FROM companies c
        JOIN jobs j ON j.company_id = c.id
        JOIN canonical_opportunities co
          ON co.id = j.canonical_opportunity_id
        WHERE c.slug = ?
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY j.id
        """,
        (slug,),
    ).fetchall()
    eligible = next(
        (
            dict(row)
            for row in rows
            if public_job_page.public_historical_inventory_is_eligible(dict(row))
        ),
        None,
    )
    if eligible is None:
        return None
    return {
        key: eligible[key]
        for key in (
            "company_id",
            "company_name",
            "company_slug",
            "careers_url",
            "source_tier",
            "inventory_model",
            "market_count_policy",
        )
    }


def build_public_company(jobs, path, *, page=1, known_company=None):
    """Build one current or historically public-eligible company view."""

    slug = parse_public_company_path(path)
    if slug is None:
        return None
    company_jobs = tuple(job for job in jobs if job.get("company_slug") == slug)
    if not company_jobs and not isinstance(known_company, dict):
        return None

    catalog = public_jobs_catalog.build_catalog(company_jobs, {"page": page})
    first = company_jobs[0] if company_jobs else known_company
    return {
        "path": path,
        "slug": slug,
        "name": public_job_page.clean(first.get("company_name")),
        "official_url": public_job_page.human_facing_company_url(
            first.get("careers_url")
        ),
        "opportunity_count": len(company_jobs),
        "has_current_jobs": bool(company_jobs),
        "catalog": catalog,
    }


def render_public_company_page(
    company,
    *,
    public_origin,
    navigation="",
    authenticated=False,
    query_present=False,
):
    name = company["name"]
    path = company["path"]
    catalog = company["catalog"]
    count = company["opportunity_count"]
    has_current_jobs = company["has_current_jobs"]
    count_label = f"{count} current {'opportunity' if count == 1 else 'opportunities'}"
    canonical_path = path if catalog["page"] == 1 else company_page_target(
        path,
        catalog["page"],
    )
    canonical_url = public_origin.rstrip("/") + canonical_path
    robots = (
        "<meta name='robots' content='noindex,follow'>"
        if not has_current_jobs
        else ""
    )
    official_link = (
        f"<a class='button button-secondary' href='{public_job_page.e(company['official_url'])}' "
        "target='_blank' rel='noopener noreferrer nofollow'>Company website or careers</a>"
        if company["official_url"]
        else ""
    )
    cards = "".join(
        public_jobs_catalog.render_job_card(job, return_to=None)
        for job in catalog["jobs"]
    ) or (
        "<section class='empty-results'><h3>No current opportunities</h3>"
        "<p>This company does not currently have a trusted public opportunity "
        "on Wahojobs. Check back later or browse all jobs.</p></section>"
    )
    pagination = render_company_pagination(company)
    workflow = (
        "<a class='button button-primary' href='/find-matches'>See your matches</a>"
        "<a class='button button-secondary' href='/tracker'>Open My Jobs</a>"
        if authenticated
        else "<a class='button button-primary' href='/find-matches'>Find jobs that fit you</a>"
        "<a class='button button-secondary' href='/login'>Create a profile or sign in</a>"
    )
    first_number = catalog["first_result_number"]
    last_number = catalog["last_result_number"]
    visible_label = (
        count_label if has_current_jobs else "No current opportunities"
    ) if catalog["total_pages"] == 1 else (
        f"Showing {first_number}–{last_number} of {count_label}"
    )
    page_suffix = f" — Page {catalog['page']}" if catalog["page"] > 1 else ""
    meta_description = (
        f"Browse current {name} opportunities available on Wahojobs."
        if has_current_jobs
        else f"View {name} on Wahojobs and check for current opportunities."
    )
    hero_description = (
        f"Explore current {name} opportunities available through Wahojobs."
        if has_current_jobs
        else f"View {name} on Wahojobs and check back for future opportunities."
    )

    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{public_job_page.e(name)} jobs{public_job_page.e(page_suffix)} | Wahojobs</title>
  <meta name='description' content='{public_job_page.e(meta_description)}'>
  {robots}
  <link rel='canonical' href='{public_job_page.e(canonical_url)}'>
  <style>{public_job_page.PUBLIC_JOB_CSS}{public_jobs_catalog.PUBLIC_JOBS_CSS}{PUBLIC_COMPANY_CSS}</style>
</head>
<body>
  <header class='site-header'>
    <a class='brand' href='/jobs'>Wahojobs</a>
    {navigation}
  </header>
  <main class='company-main'>
    <p class='back-to-jobs'><a href='/jobs'>← Browse all jobs</a></p>
    <header class='company-hero'>
      <p class='eyebrow'>Company</p>
      <h1>{public_job_page.e(name)}</h1>
      <p>{public_job_page.e(hero_description)}</p>
      <div class='company-actions'>{official_link}</div>
    </header>
    <section class='company-opportunities' aria-labelledby='current-opportunities'>
      <div class='section-heading'>
        <div>
          <p class='eyebrow'>Current on Wahojobs</p>
          <h2 id='current-opportunities'>Current opportunities</h2>
        </div>
        <p><strong>{public_job_page.e(visible_label)}</strong></p>
      </div>
      <div class='jobs-list'>{cards}</div>
      {pagination}
    </section>
    <aside class='company-workflow'>
      <div>
        <p class='eyebrow'>Make discovery personal</p>
        <h2>Find the opportunities that fit you</h2>
        <p>Use your Wahojobs profile to compare roles and keep promising jobs organized.</p>
      </div>
      <div class='company-actions'>{workflow}</div>
    </aside>
  </main>
</body>
</html>"""


def render_company_pagination(company):
    catalog = company["catalog"]
    if catalog["total_pages"] <= 1:
        return ""
    page = catalog["page"]
    previous = (
        company_page_link(company["path"], page - 1, "Previous")
        if page > 1
        else ""
    )
    following = (
        company_page_link(company["path"], page + 1, "Next")
        if page < catalog["total_pages"]
        else ""
    )
    return f"""
    <nav class='pagination' aria-label='Company jobs pages'>
      <span class='pagination-side previous'>{previous}</span>
      <span>Page {page} of {catalog['total_pages']}</span>
      <span class='pagination-side next'>{following}</span>
    </nav>
    """


def company_page_link(path, page, label):
    target = company_page_target(path, page)
    return f"<a href='{public_job_page.e(target)}'>{public_job_page.e(label)}</a>"


def company_page_target(path, page):
    return path if page == 1 else path + "?" + urlencode({"page": page})


PUBLIC_COMPANY_CSS = """
.company-main { padding-top: 4px; }
.company-hero { border: 1px solid #d9e2dd; border-radius: 18px; background: #fff; padding: clamp(28px, 5vw, 52px); box-shadow: 0 14px 40px rgba(28, 53, 42, .06); }
.company-hero > p:not(.eyebrow) { max-width: 720px; margin: 18px 0 0; color: #40564c; font-size: 1.06rem; }
.company-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
.company-opportunities { margin-top: 20px; }
.section-heading { display: flex; justify-content: space-between; gap: 24px; align-items: end; padding: 22px 2px 14px; }
.section-heading p { margin-bottom: 0; color: #40564c; }
.company-workflow { display: grid; grid-template-columns: minmax(0, 1.4fr) auto; gap: 24px; align-items: center; margin-top: 22px; border: 1px solid #cbdad2; border-radius: 14px; background: #f5faf7; padding: 24px; }
.company-workflow h2, .company-workflow p:last-child { margin-bottom: 0; }
.company-workflow .company-actions { justify-content: flex-end; margin-top: 0; }
@media (max-width: 720px) { .section-heading, .company-workflow { display: grid; grid-template-columns: 1fr; align-items: start; } .company-workflow .company-actions { justify-content: flex-start; } }
"""


__all__ = [
    "PUBLIC_COMPANY_PATH",
    "build_public_company",
    "company_page_target",
    "load_public_company_identity",
    "parse_company_query",
    "parse_public_company_path",
    "render_public_company_page",
]
