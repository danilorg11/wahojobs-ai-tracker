import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.crawler.pipeline import run_crawl
from wahojobs.reporting.terminal import print_crawl_summary


def main():
    company, summary = run_crawl("outlier")
    print_crawl_summary(company, summary)


if __name__ == "__main__":
    main()
