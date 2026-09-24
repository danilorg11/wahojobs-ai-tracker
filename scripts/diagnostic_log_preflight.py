"""Strict online diagnostic housekeeping before a controlled beta deployment."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wahojobs.diagnostic_archive import archive_preflight


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", required=True)
    args = parser.parse_args(argv)
    result = archive_preflight(args.logs)
    print("diagnostic_preflight_ok archived={} live={} archive_total={} bytes={} free={}".format(
        len(result["archived"]), result["live"], result["archived_total"],
        result["bytes"], result["free"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
