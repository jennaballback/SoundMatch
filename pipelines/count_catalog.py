"""Save today's catalog size (songs, estimated songs, artists) in catalog_counts.

Usage:
    python pipelines/count_catalog.py

The daily job (run_daily.bat) runs this last, after new chart songs are
added, so catalog_counts gets one row per day. The SQL is in
sql/008_catalog_counts.sql. Running it twice on the same day just
updates that day's row.
"""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
COUNT_SQL = ROOT / "sql" / "008_catalog_counts.sql"
DEFAULT_DSN = "postgresql://music:music@localhost:5432/music"


def main() -> None:
    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(COUNT_SQL.read_text(encoding="utf-8-sig"))
        cur.execute("SELECT count_date, songs, estimated, artists FROM catalog_counts "
                    "ORDER BY count_date DESC LIMIT 2")
        today, *before = cur.fetchall()
    change = f" ({today[1] - before[0][1]:+,} since {before[0][0]})" if before else ""
    print(f"Catalog on {today[0]}: {today[1]:,} songs{change}, "
          f"{today[2]:,} added from chart previews, {today[3]:,} artists")


if __name__ == "__main__":
    main()
