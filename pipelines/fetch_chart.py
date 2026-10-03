"""Save today's Last.fm US top tracks as a dated snapshot in Postgres.

Usage:
    python pipelines/fetch_chart.py --file data/sample_lastfm_chart.json   # offline test, no API key needed
    python pipelines/fetch_chart.py                                         # the real thing (needs LASTFM_API_KEY in .env)
    python pipelines/fetch_chart.py --date 2026-09-20 --file data/sample_lastfm_chart.json
                                                                            # pretend the sample is another day

What it does, in one transaction:
    1. EXTRACT    call the Last.fm API, or read a saved JSON file with --file
    2. LOAD       store the raw JSON, untouched, in staging.lastfm_chart_raw (one row per day)
    3. TRANSFORM  run sql/003_transform_chart.sql: raw JSON -> chart_entries rows   <- you write this
    4. MATCH      run sql/004_match_chart.sql: link chart rows to catalog tracks    <- you write this
    5. Print what's in chart_entries for that day

Safe to run as many times as you like: every step upserts, so a second run
on the same day replaces that day's snapshot instead of duplicating it.
Steps 3 and 4 are skipped with a message until you've written those files,
so you can run this right away and watch the raw JSON land first.
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "sql" / "001_schema.sql"
TRANSFORM_SQL = ROOT / "sql" / "003_transform_chart.sql"
MATCH_SQL = ROOT / "sql" / "004_match_chart.sql"

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
DEFAULT_DSN = "postgresql://music:music@localhost:5432/music"


def fetch_from_lastfm(api_key: str) -> dict:
    """Call geo.getTopTracks and return the parsed JSON.

    Docs: https://www.last.fm/api/show/geo.getTopTracks
    """
    # TODO (you): fill in the query parameters. Last.fm reads them from the
    # URL, like ?method=...&country=...  requests builds that URL for you
    # from this dict. You need five keys:
    #   method   the API method, in lowercase: "geo.gettoptracks"
    #   country  the country's full English name, as Last.fm spells it
    #   api_key  the key passed into this function
    #   format   "json" (the default is XML)
    #   limit    how many tracks to get; 50 is the default, 100 is a nice round chart
    params = {
        "method": "geo.gettoptracks",
        "country": "United States",
        "api_key": api_key,
        "format": "json",
        "limit": 100,
    }

    try:
        response = requests.get(LASTFM_URL, params=params, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"Couldn't reach Last.fm. Check your internet connection. Details: {e}")
    try:
        data = response.json()
    except ValueError:
        sys.exit(f"Last.fm sent back something that isn't JSON (HTTP {response.status_code}). Try again in a minute.")

    # Last.fm reports problems (bad key, missing parameter...) as JSON with
    # an "error" number and a "message", so check for that before anything else.
    if "error" in data:
        sys.exit(f"Last.fm said no (error {data['error']}): {data.get('message')}")
    response.raise_for_status()
    return data


def has_sql(path: Path) -> bool:
    """True if the file contains something besides comments and blank lines."""
    if not path.exists():
        return False
    without_comments = re.sub(r"--[^\n]*", "", path.read_text())
    return without_comments.strip() != ""


def run_sql_file(cur: psycopg.Cursor, path: Path, step: str) -> None:
    if has_sql(path):
        cur.execute(path.read_text())
        print(f"{step}: ran {path.name}")
    else:
        print(f"{step}: skipped, {path.name} has no SQL yet (see WEEK2.md)")


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, help="read the chart from a saved JSON file instead of calling the API")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(),
                        help="which chart_date to store it under, YYYY-MM-DD (default: today)")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
                        help="Postgres connection string (default: $DATABASE_URL)")
    args = parser.parse_args()

    # 1. EXTRACT
    if args.file:
        payload = json.loads(args.file.read_text(encoding="utf-8"))
        print(f"Extract: read {args.file}")
    else:
        api_key = os.environ.get("LASTFM_API_KEY", "").strip()
        if not api_key:
            sys.exit("LASTFM_API_KEY is empty. Put your key in the .env file (see WEEK2.md, step 2).")
        payload = fetch_from_lastfm(api_key)
        print("Extract: called the Last.fm API")

    n_tracks = len(payload.get("tracks", {}).get("track", []))
    if n_tracks == 0:
        sys.exit("The response has no tracks in it, so nothing was saved. Check the params in fetch_from_lastfm().")
    print(f"         got {n_tracks} tracks for {args.date}")

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.read_text())

        # 2. LOAD: one row per day. Re-running the same day overwrites it.
        cur.execute(
            """
            INSERT INTO staging.lastfm_chart_raw (chart_date, payload)
            VALUES (%s, %s)
            ON CONFLICT (chart_date) DO UPDATE
                SET payload = EXCLUDED.payload,
                    fetched_at = now()
            """,
            (args.date, Jsonb(payload)),
        )
        print("Load: saved the raw JSON in staging.lastfm_chart_raw")

        # 3. TRANSFORM and 4. MATCH: your SQL
        run_sql_file(cur, TRANSFORM_SQL, "Transform")
        run_sql_file(cur, MATCH_SQL, "Match")

        # 5. Summary
        cur.execute(
            "SELECT count(*), count(track_id) FROM chart_entries WHERE chart_date = %s",
            (args.date,),
        )
        total, matched = cur.fetchone()
        cur.execute(
            """
            SELECT rank, lastfm_track_name, lastfm_artist_name, track_id IS NOT NULL
            FROM chart_entries
            WHERE chart_date = %s
            ORDER BY rank
            LIMIT 10
            """,
            (args.date,),
        )
        top = cur.fetchall()

    print()
    if total == 0:
        print(f"chart_entries has no rows for {args.date} yet.")
        return
    print(f"chart_entries for {args.date}: {total} rows, {matched} matched to the catalog ({matched / total:.0%})")
    for rank, track, artist, is_matched in top:
        print(f"  {rank:>3}. {track} - {artist}{'' if is_matched else '   (no match)'}")


if __name__ == "__main__":
    main()
