"""Can a free AI model on your PC guess Spotify's mood scores?

Usage:
    python pipelines/llm_accuracy_check.py
    python pipelines/llm_accuracy_check.py --songs 300
    python pipelines/llm_accuracy_check.py --model gemma3:4b

What it does:
    1. PICK    a random set of songs from the catalog (same set every run).
    2. ASK     the local model (Ollama, on port 11434) to guess six mood
               scores from 0 to 1, giving it only the song name, artist,
               genre, tempo, key, major/minor and loudness.
    3. SCORE   compare its guesses with Spotify's real numbers, and with a
               "lazy guess" that answers the catalog average every time.
               If the model can't beat the lazy guess, it isn't helping.
    4. SAVE    every guess next to the real value in data/llm_check_results.csv
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DSN = "postgresql://music:music@localhost:5432/music"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
FEATURES = ["danceability", "energy", "valence", "acousticness",
            "speechiness", "instrumentalness"]
KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

SONGS_SQL = """
SELECT t.track_id, t.name, a.name AS artist,
       (SELECT string_agg(g.name, ', ' ORDER BY g.name)
          FROM track_genres tg JOIN genres g ON g.genre_id = tg.genre_id
         WHERE tg.track_id = t.track_id) AS genres,
       f.tempo, f.musical_key, f.mode, f.loudness,
       f.danceability, f.energy, f.valence, f.acousticness,
       f.speechiness, f.instrumentalness
FROM tracks t
JOIN audio_features f ON f.track_id = t.track_id
JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
JOIN artists a ON a.artist_id = ta.artist_id
-- Sorting by a scrambled version of the id gives a random-looking mix
-- that is the same every run, so every model is tested on the same songs.
ORDER BY md5(t.track_id)
LIMIT %s
"""

AVERAGES_SQL = "SELECT " + ", ".join(f"avg({f})" for f in FEATURES) + " FROM audio_features"

PROMPT = """You are a music analyst. Estimate Spotify-style audio features for this song.

Song: {name}
Artist: {artist}
Genre: {genres}
Tempo: {tempo:.0f} BPM
Key: {key} {mode}
Loudness: {loudness:.1f} dB

Give each value from 0 to 1:
- danceability: how suitable for dancing
- energy: intensity and activity
- valence: musical happiness (1 = happy, 0 = sad or angry)
- acousticness: how acoustic (vs electronic)
- speechiness: how much spoken word (rap is around 0.1 to 0.3, talk is near 1)
- instrumentalness: chance there are no vocals

Answer with JSON only, like:
{{"danceability": 0.5, "energy": 0.5, "valence": 0.5, "acousticness": 0.5, "speechiness": 0.05, "instrumentalness": 0.0}}"""


def ask_model(model: str, song: dict) -> dict:
    """Send one song to Ollama and return its six guesses as numbers."""
    k = song["musical_key"]
    key = KEYS[k] if k is not None and 0 <= k <= 11 else "unknown"
    text = PROMPT.format(
        name=song["name"], artist=song["artist"], genres=song["genres"] or "unknown",
        tempo=song["tempo"] or 0, key=key, mode="major" if song["mode"] == 1 else "minor",
        loudness=song["loudness"] or 0,
    )
    response = requests.post(OLLAMA_URL, timeout=300, json={
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "format": "json",              # forces the reply to be valid JSON
        "stream": False,
        "options": {"temperature": 0},  # same answer every time
    })
    response.raise_for_status()
    answer = json.loads(response.json()["message"]["content"])
    # Keep every guess between 0 and 1, even if the model wanders outside.
    return {f: min(1.0, max(0.0, float(answer[f]))) for f in FEATURES}


def grade(model_error: float, lazy_error: float) -> str:
    """Turn 'how much better than the lazy guess' into words."""
    gain = 1 - model_error / lazy_error if lazy_error else 0
    if gain >= 0.30:
        return "good"
    if gain >= 0.10:
        return "some help"
    if gain > 0:
        return "barely helps"
    return "no help"


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Check how well a local AI guesses Spotify's mood scores.")
    parser.add_argument("--songs", type=int, default=200, help="how many songs to test (default 200)")
    parser.add_argument("--model", default="llama3.2", help="Ollama model name (default llama3.2)")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
                        help="Postgres connection string (default: $DATABASE_URL)")
    args = parser.parse_args()

    try:
        requests.get("http://127.0.0.1:11434", timeout=5)
    except requests.ConnectionError:
        sys.exit("Can't reach Ollama on port 11434. Open the Ollama app and try again.")

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute(AVERAGES_SQL)
        averages = dict(zip(FEATURES, cur.fetchone()))
        cur.execute(SONGS_SQL, (args.songs,))
        columns = [c.name for c in cur.description]
        songs = [dict(zip(columns, row)) for row in cur.fetchall()]

    rows = []
    for i, song in enumerate(songs, start=1):
        try:
            guess = ask_model(args.model, song)
        except (requests.RequestException, ValueError, KeyError) as error:
            print(f"{i:>4}/{len(songs)}  skipped {song['name']} ({error})")
            continue
        rows.append((song, guess))
        print(f"{i:>4}/{len(songs)}  {song['name']} by {song['artist']}")

    if not rows:
        sys.exit("The model didn't answer any songs. Check that the model name is right with: ollama list")

    out_path = ROOT / "data" / "llm_check_results.csv"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["track_id", "name", "artist"]
                        + [f"{feat}_{kind}" for feat in FEATURES for kind in ("spotify", "model")])
        for song, guess in rows:
            writer.writerow([song["track_id"], song["name"], song["artist"]]
                            + [round(v, 3) for feat in FEATURES for v in (song[feat], guess[feat])])

    print(f"\nModel {args.model}, {len(rows)} songs. Average miss on a 0 to 1 scale:\n")
    print(f"  {'feature':<18}{'model':>8}{'lazy guess':>12}   verdict")
    model_total = lazy_total = 0.0
    for feat in FEATURES:
        model_error = sum(abs(g[feat] - s[feat]) for s, g in rows) / len(rows)
        lazy_error = sum(abs(averages[feat] - s[feat]) for s, _ in rows) / len(rows)
        model_total += model_error
        lazy_total += lazy_error
        print(f"  {feat:<18}{model_error:>8.3f}{lazy_error:>12.3f}   {grade(model_error, lazy_error)}")
    model_total /= len(FEATURES)
    lazy_total /= len(FEATURES)
    print(f"  {'overall':<18}{model_total:>8.3f}{lazy_total:>12.3f}   {grade(model_total, lazy_total)}")
    print(f"\nEvery guess is saved in {out_path}")


if __name__ == "__main__":
    main()
