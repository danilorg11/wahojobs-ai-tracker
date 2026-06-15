import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.crawler.pipeline import run_crawl
from wahojobs.reporting.terminal import print_crawl_summary


def main():
    company_slug = sys.argv[1] if len(sys.argv) > 1 else "appen"
    company, summary = run_crawl(company_slug)
    print_crawl_summary(company, summary)


if __name__ == "__main__":
    main()
