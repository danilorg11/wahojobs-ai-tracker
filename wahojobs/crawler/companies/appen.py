from wahojobs.crawler.providers.lever import fetch_lever_jobs, complete_board


def crawl_appen(api_url):
    jobs = fetch_lever_jobs(api_url)
    return complete_board(jobs, len(jobs), f"Observed public Appen Lever board: {api_url}")
