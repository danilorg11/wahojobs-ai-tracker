from dataclasses import replace

from wahojobs.crawler.providers.lever import fetch_lever_jobs
from wahojobs.crawler.types import CompanyCrawlResult


AI_WORK_KEYWORDS = (
    "trainai",
    "ai data",
    "data specialist",
    "data annotator",
    "language annotator",
    "annotation",
    "annotator",
    "linguistic ai",
    "speech ai",
    "ai evaluation",
    "evaluation specialist",
    "ai auditor",
    "ai dubber",
    "llm",
    "subject matter expert",
    "sme",
)


def crawl_rws(api_url):
    jobs = []
    for job in fetch_lever_jobs(api_url):
        if not (job.external_id and job.title and job.url):
            continue
        if not is_ai_work_role(job.title):
            continue
        jobs.append(enrich_rws_job(job))

    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="lever",
        source_message=f"Fetched live RWS TrainAI jobs from Lever API: {api_url}",
    )


def is_ai_work_role(title):
    title = (title or "").lower()
    return any(keyword in title for keyword in AI_WORK_KEYWORDS)


def enrich_rws_job(job):
    category = infer_rws_category(job.title)
    department = job.department
    if not department or department.lower() == "rws":
        department = category

    return replace(
        job,
        department=department,
        expertise=job.expertise or category,
    )


def infer_rws_category(title):
    title_lower = (title or "").lower()

    if "future opportunities" in title_lower or "trainai talent pool" in title_lower:
        return "TrainAI Talent Pool"
    if "speech ai evaluation" in title_lower:
        return "Speech AI Evaluation"
    if "linguistic ai auditor" in title_lower:
        return "Linguistic AI Auditing"
    if "ai dubber" in title_lower:
        return "AI Dubbing"
    if "language data annotator" in title_lower or "language annotator" in title_lower:
        return "Language Annotation"
    if "subject matter expert" in title_lower or "sme" in title_lower:
        return "Subject Matter Expert"
    if "ai data specialist" in title_lower or "data specialist" in title_lower:
        return "AI Data Specialist"
    if "llm" in title_lower:
        return "LLM Evaluation"
    if "annotation" in title_lower or "annotator" in title_lower:
        return "Data Annotation"
    if "evaluation" in title_lower or "evaluator" in title_lower:
        return "AI Evaluation"
    return "TrainAI"
