from wahojobs.crawler.providers.greenhouse import (
    GreenhouseBoardConfig,
    fetch_greenhouse_snapshot,
)
from wahojobs.crawler.types import MERIDIAL_GREENHOUSE_RECORD_CONTRACT_ID


MERIDIAL_GREENHOUSE_CONFIG = GreenhouseBoardConfig(
    source_name="Meridial",
    company_id="meridial",
    board_token="agency",
    allowed_job_hosts=(
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    ),
    api_host="https://boards-api.greenhouse.io",
    root_department_id=4012485101,
    record_promotion_contract_id=MERIDIAL_GREENHOUSE_RECORD_CONTRACT_ID,
)


def crawl_meridial(api_url):
    return fetch_greenhouse_snapshot(
        MERIDIAL_GREENHOUSE_CONFIG,
        configured_url=api_url,
    )
