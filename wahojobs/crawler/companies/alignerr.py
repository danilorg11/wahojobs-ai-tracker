from wahojobs.crawler.providers.alignerr import fetch_alignerr_snapshot


def crawl_alignerr(api_url):
    return fetch_alignerr_snapshot(api_url)
