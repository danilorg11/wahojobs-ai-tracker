import sys
import argparse
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.crawler.pipeline import run_crawl
from wahojobs.reporting.terminal import print_crawl_summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh explicitly selected sources; --inspect performs no requests or writes.")
    parser.add_argument("sources", nargs="*", default=["appen"])
    parser.add_argument("--db", type=Path, help="Existing absolute local-development database outside Git checkouts")
    parser.add_argument("--inspect", action="store_true", help="Print the target, request scope and detail-reuse policy without refreshing")
    parser.add_argument("--details", choices=("needed", "all"), help="Recover Alignerr/micro1 details after catalog ingestion; requires --db")
    args = parser.parse_args(argv)
    if (args.inspect or args.details) and args.db is None:
        parser.error("--inspect and --details require an explicit --db")
    if args.inspect:
        from wahojobs.crawler.local_inventory import inspect_refresh
        print(json.dumps(inspect_refresh(args.db, args.sources, details=args.details), indent=2))
        return
    for company_slug in dict.fromkeys(args.sources):
        options = {} if args.db is None else {"db_path": args.db, "details": args.details}
        company, summary = run_crawl(company_slug, **options)
        print_crawl_summary(company, summary)


if __name__ == "__main__":
    main()
