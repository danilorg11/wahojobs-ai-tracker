import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.config import DB_PATH
from wahojobs.db.repository import initialize_database


def main():
    initialize_database()
    print(f"Initialized SQLite database at {DB_PATH}")
    print("Seeded companies: Appen, Invisible Technologies, Meridial, Outlier")


if __name__ == "__main__":
    main()
