from wahojobs.crawler.providers.mercor import fetch_mercor_observations


def crawl_mercor(api_url):
    return fetch_mercor_observations(api_url)
