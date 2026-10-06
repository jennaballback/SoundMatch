"""Type in a song, get back songs that sound like it.

Usage:
    python pipelines/similar_songs.py "blinding lights"
    python pipelines/similar_songs.py "blinding lights" --artist "the weeknd"
    python pipelines/similar_songs.py "levitating" --count 20

What it does:
    1. FIND    look up the song by name in the catalog. If several songs
               match, it picks the most popular one (narrow it with --artist).
    2. SEARCH  call the similar_songs() function from sql/005_similar_songs.sql
    3. Print the results, closest first.
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DSN = "postgresql://music:music@localhost:5432/music"

# Same lookup as step 1 in psql. %s is a placeholder: psycopg fills in the
# values safely, so a song name with a quote in it can't break the SQL.
FIND_SONG_SQL = """
SELECT t.track_id, t.name, a.name AS artist
FROM tracks t
JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
JOIN artists a ON a.artist_id = ta.artist_id
WHERE t.name ILIKE %s
  AND a.name ILIKE %s
ORDER BY t.popularity DESC
LIMIT 1
"""

SIMILAR_SQL = "SELECT name, artist, distance FROM similar_songs(%s, %s)"


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Find songs that sound like a song you name.")
    parser.add_argument("song", help="song name, or part of it")
    parser.add_argument("--artist", default="", help="artist name, or part of it")
    parser.add_argument("--count", type=int, default=10, help="how many results (default 10)")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
                        help="Postgres connection string (default: $DATABASE_URL)")
    args = parser.parse_args()

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute(FIND_SONG_SQL, (f"%{args.song}%", f"%{args.artist}%"))
        found = cur.fetchone()
        if found is None:
            sys.exit(f'No song matching "{args.song}" in the catalog. '
                     "Try part of the name, or a song from before 2022.")
        track_id, name, artist = found

        cur.execute(SIMILAR_SQL, (track_id, args.count))
        rows = cur.fetchall()

    print(f"\nSongs that sound like {name} by {artist}:\n")
    for i, (other_name, other_artist, distance) in enumerate(rows, start=1):
        print(f"{i:>3}. {other_name} by {other_artist}  ({distance})")
    print()


if __name__ == "__main__":
    main()
