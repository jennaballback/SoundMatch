"""Teach a small model to guess mood scores from your own catalog.

Usage:
    python pipelines/train_mood_model.py

The free AI models guessed worse than "just say the average". This takes a
different route: instead of asking a model that has read the internet, it
learns straight from your 89,000 songs. It looks at things you can measure
from any audio file (tempo, loudness, key, major/minor, length) plus the
genre, and learns how they line up with Spotify's six mood scores.

What it does:
    1. LOAD    every catalog song with its genre and audio features.
    2. SPLIT   hold back 5,000 songs the model never sees while learning.
               They use the same scrambled order as llm_accuracy_check.py,
               so the first 20 are the same 20 songs the AI models got.
    3. TRAIN   one model per mood score on the other ~85,000 songs.
    4. SCORE   check its guesses on the held-back songs, against the same
               "lazy guess" (the average) as before.
    5. SAVE    the trained model to models/mood_model.joblib, ready for an
               "analyze your own song" page later.
"""

import argparse
import os
from pathlib import Path

import joblib
import numpy as np
import psycopg
from dotenv import load_dotenv
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DSN = "postgresql://music:music@localhost:5432/music"
MOODS = ["danceability", "energy", "valence", "acousticness",
         "speechiness", "instrumentalness"]
# What the model gets to look at. Genre comes first and is handled as a
# category (pop, rock, ...), the rest are plain numbers.
INPUTS = ["genre", "tempo", "loudness", "musical_key", "mode", "duration_ms"]
TEST_SIZE = 5000

# One genre per song: a song filed under several gets the first one A to Z.
SONGS_SQL = """
SELECT (SELECT min(g.name)
          FROM track_genres tg JOIN genres g ON g.genre_id = tg.genre_id
         WHERE tg.track_id = t.track_id) AS genre,
       f.tempo, f.loudness, f.musical_key, f.mode, t.duration_ms,
       f.danceability, f.energy, f.valence, f.acousticness,
       f.speechiness, f.instrumentalness
FROM tracks t
JOIN audio_features f ON f.track_id = t.track_id
ORDER BY md5(t.track_id)
"""


def grade(model_error: float, lazy_error: float) -> str:
    """Same wording as llm_accuracy_check.py, so the tables compare."""
    gain = 1 - model_error / lazy_error if lazy_error else 0
    if gain >= 0.30:
        return "good"
    if gain >= 0.10:
        return "some help"
    if gain > 0:
        return "barely helps"
    return "no help"


def print_table(title: str, guesses: np.ndarray, truth: np.ndarray, averages: np.ndarray) -> None:
    print(f"\n{title}. Average miss on a 0 to 1 scale:\n")
    print(f"  {'feature':<18}{'model':>8}{'lazy guess':>12}   verdict")
    model_errors = np.nanmean(np.abs(guesses - truth), axis=0)
    lazy_errors = np.nanmean(np.abs(averages - truth), axis=0)
    for name, m, l in zip(MOODS, model_errors, lazy_errors):
        print(f"  {name:<18}{m:>8.3f}{l:>12.3f}   {grade(m, l)}")
    m, l = model_errors.mean(), lazy_errors.mean()
    print(f"  {'overall':<18}{m:>8.3f}{l:>12.3f}   {grade(m, l)}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Train a mood-score model on your catalog.")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
                        help="Postgres connection string (default: $DATABASE_URL)")
    args = parser.parse_args()

    print("Loading songs from the database...")
    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute(SONGS_SQL)
        rows = cur.fetchall()
    print(f"Loaded {len(rows):,} songs.")

    # Models only understand numbers, so each genre name gets a number.
    genres = sorted({r[0] for r in rows if r[0]})
    genre_number = {name: i for i, name in enumerate(genres)}
    X = np.array([[genre_number.get(r[0], np.nan)] + [np.nan if v is None else float(v) for v in r[1:6]]
                  for r in rows])
    y = np.array([[np.nan if v is None else float(v) for v in r[6:]] for r in rows])

    X_test, y_test = X[:TEST_SIZE], y[:TEST_SIZE]
    X_train, y_train = X[TEST_SIZE:], y[TEST_SIZE:]
    # The lazy guess only gets to see the training songs too, to be fair.
    averages = np.nanmean(y_train, axis=0)

    models = {}
    guesses = np.zeros_like(y_test)
    for i, mood in enumerate(MOODS):
        print(f"Training {mood}...")
        known = ~np.isnan(y_train[:, i])
        model = HistGradientBoostingRegressor(categorical_features=[0], random_state=0)
        model.fit(X_train[known], y_train[known, i])
        models[mood] = model
        guesses[:, i] = np.clip(model.predict(X_test), 0, 1)

    print_table("The same 20 songs the AI models got", guesses[:20], y_test[:20], averages)
    print_table(f"All {len(X_test):,} held-back songs", guesses, y_test, averages)

    out_path = ROOT / "models" / "mood_model.joblib"
    out_path.parent.mkdir(exist_ok=True)
    joblib.dump({"models": models, "genres": genres, "inputs": INPUTS}, out_path)
    print(f"\nSaved the model to {out_path}")


if __name__ == "__main__":
    main()
