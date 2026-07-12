from wahojobs.crawler.providers.greenhouse import (
    GreenhouseBoardConfig,
    fetch_greenhouse_snapshot,
)


MERIDIAL_GREENHOUSE_CONFIG = GreenhouseBoardConfig(
    source_name="Meridial",
    board_token="agency",
    api_host="https://boards-api.greenhouse.io",
    root_department_id=4012485101,
)


def crawl_meridial(api_url):
    return fetch_greenhouse_snapshot(
        MERIDIAL_GREENHOUSE_CONFIG,
        configured_url=api_url,
    )
