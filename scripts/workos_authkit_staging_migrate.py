"""Explicit offline M008 command for one authorized external database."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from wahojobs.workos_authkit_staging import (
    WorkOSAuthKitStagingError,
    apply_m008_to_explicit_database,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Apply accepted M008 to one explicit authorized database."
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Absolute path to the existing external M007/M008 SQLite database.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_args(argv)
    try:
        result = apply_m008_to_explicit_database(arguments.database)
    except WorkOSAuthKitStagingError as exc:
        print(f"M008 operation failed: {exc.code}", file=sys.stderr)
        return 2
    action = "applied" if result["applied"] else "already installed"
    print(f"M008 {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
