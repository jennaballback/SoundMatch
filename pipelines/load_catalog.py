"""Load the Kaggle Spotify tracks CSV into Postgres.

Usage:
    python pipelines/load_catalog.py data/dataset.csv
    python pipelines/load_catalog.py data/sample_tracks.csv   # quick test

What it does, in one transaction:
    1. Creates the tables if they don't exist yet    (sql/001_schema.sql)
    2. Empties the staging table and COPYs the CSV into it, as raw text
    3. Transforms staging into the core tables       (sql/002_transform_catalog.sql)
    4. Prints row counts so you can sanity-check the result

Safe to run as many times as you like: step 3 upserts, so nothing is duplicated.
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "sql" / "001_schema.sql"
TRANSFORM_SQL = ROOT / "sql" / "002_transform_catalog.sql"

DEFAULT_DSN = "postgresql://music:music@localhost:5432/music"

# The columns we expect in the CSV header, in staging-table names.
# The CSV's first column has no name (it's a pandas index), so we call it row_index.
EXPECTED_COLUMNS = [
    "row_index", "track_id", "artists", "album_name", "track_name", "popularity",
    "duration_ms", "explicit", "danceability", "energy", "key", "loudness", "mode",
    "speechiness", "acousticness", "instrumentalness", "liveness", "valence",
    "tempo", "time_signature", "track_genre",
]


def read_header(csv_path: Path) -> list[str]:
    """Return the CSV's column names, renaming the blank first one to row_index."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    return ["row_index" if (i == 0 and name.strip() in ("", "Unnamed: 0")) else name.strip()
            for i, name in enumerate(header)]


def check_header(columns: list[str]) -> None:
    """Fail early with a clear message if this isn't the file we expect."""
    missing = set(EXPECTED_COLUMNS) - set(columns)
    extra = set(columns) - set(EXPECTED_COLUMNS)
    if missing or extra:
        sys.exit(
            "This CSV doesn't look like the Kaggle Spotify tracks dataset.\n"
            f"  missing columns: {sorted(missing) or 'none'}\n"
            f"  unexpected columns: {sorted(extra) or 'none'}"
        )


def copy_csv_to_staging(cur: psycopg.Cursor, csv_path: Path, columns: list[str]) -> None:
    """Stream the file into staging with COPY, Postgres's bulk-load command.

    COPY is far faster than one INSERT per row (100k rows in a second or two
    instead of minutes). We name the columns in header order, so the CSV's
    column order doesn't have to match the table's.
    """
    column_list = ", ".join(f'"{c}"' for c in columns)
    sql = f"COPY staging.kaggle_tracks ({column_list}) FROM STDIN WITH (FORMAT csv, HEADER true)"
    with csv_path.open("rb") as f, cur.copy(sql) as copy:
        while chunk := f.read(1024 * 1024):
            copy.write(chunk)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path, help="path to the Kaggle dataset.csv")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
                        help="Postgres connection string (default: $DATABASE_URL)")
    args = parser.parse_args()

    if not args.csv_path.exists():
        sys.exit(f"File not found: {args.csv_path}")

    columns = read_header(args.csv_path)
    check_header(columns)

    started = time.perf_counter()
    # "with connect(...)" commits when the block finishes and rolls back if
    # anything raises, so a failed load leaves the database exactly as it was.
    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.read_text())

        cur.execute("TRUNCATE staging.kaggle_tracks")
        copy_csv_to_staging(cur, args.csv_path, columns)
        cur.execute("SELECT count(*) FROM staging.kaggle_tracks")
        staged = cur.fetchone()[0]

        cur.execute("""
            SELECT count(*) FROM staging.kaggle_tracks
            WHERE track_id IS NULL OR track_name IS NULL OR artists IS NULL
        """)
        rejected = cur.fetchone()[0]

        cur.execute(TRANSFORM_SQL.read_text())

        counts = {}
        for table in ("tracks", "artists", "track_artists", "genres", "track_genres", "audio_features"):
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]

    elapsed = time.perf_counter() - started
    print(f"Staged {staged:,} CSV rows ({rejected:,} rejected for missing id/name/artist) in {elapsed:.1f}s")
    print("Rows now in each table:")
    for table, n in counts.items():
        print(f"  {table:<15} {n:>9,}")


if __name__ == "__main__":
    main()
