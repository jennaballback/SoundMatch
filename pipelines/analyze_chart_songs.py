"""Add chart songs that aren't in the catalog, with estimated mood scores.

Usage:
    python pipelines/analyze_chart_songs.py
    python pipelines/analyze_chart_songs.py --limit 5     # try a few first
    python pipelines/analyze_chart_songs.py --check       # accuracy test, saves nothing

Most of today's hits came out after the Kaggle file (2022), so they have no
audio features and can't be searched. This fills that gap. For each chart
song with no match in the catalog:

    1. FIND     the song on Deezer (free, no key) to get its length and a
                30-second preview clip.
    2. GENRE    ask Last.fm for the song's tags (pop, hip-hop, ...) and keep
                the first one your model knows.
    3. MEASURE  librosa listens to the clip and measures tempo, loudness,
                key and major/minor.
    4. ESTIMATE your trained model (models/mood_model.joblib, made by
                train_mood_model.py) turns those into the six mood scores.
    5. SAVE     the song goes into tracks, artists, genres and
                audio_features, marked source = 'estimated', and the chart
                rows get linked to it.

Songs Deezer can't find are skipped and tried again on the next run.

--check runs steps 1 to 4 on chart songs that ARE in the catalog and
compares the estimates with Spotify's real numbers. That's the honest test
of the whole chain: a 30-second clip measured by librosa, not Spotify's data.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import joblib
import librosa
import numpy as np
import psycopg
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DSN = "postgresql://music:music@localhost:5432/music"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
MOODS = ["danceability", "energy", "valence", "acousticness",
         "speechiness", "instrumentalness"]

# Last.fm tags are free text. These turn common ones into the Kaggle genre
# names your model learned. Anything else is tried as-is ("k pop" -> "k-pop").
TAG_TO_GENRE = {
    "rap": "hip-hop", "hip hop": "hip-hop", "trap": "hip-hop",
    "rnb": "r-n-b", "r&b": "r-n-b", "rhythm and blues": "r-n-b",
    "electronic": "electronic", "dance": "dance", "edm": "edm",
    "country": "country", "rock": "rock", "indie": "indie", "latin": "latin",
    "reggaeton": "reggaeton", "alternative": "alternative", "soul": "soul",
}
FALLBACK_GENRE = "pop"

# The schema has no place to say where a song's numbers came from yet.
# IF NOT EXISTS makes this safe to run every time.
ADD_SOURCE_SQL = """
ALTER TABLE audio_features
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'spotify'
"""

MISSING_SQL = """
SELECT DISTINCT lastfm_track_name, lastfm_artist_name
FROM chart_entries
WHERE track_id IS NULL
ORDER BY 1, 2
"""

# Chart songs that already have Spotify's numbers, for --check.
MATCHED_SQL = """
SELECT DISTINCT c.lastfm_track_name, c.lastfm_artist_name, f.tempo, f.loudness,
       f.danceability, f.energy, f.valence, f.acousticness,
       f.speechiness, f.instrumentalness
FROM chart_entries c
JOIN audio_features f ON f.track_id = c.track_id
WHERE f.source = 'spotify'
"""

AVERAGES_SQL = ("SELECT " + ", ".join(f"avg({m})" for m in MOODS)
                + " FROM audio_features WHERE source = 'spotify'")

# Musical keys follow Krumhansl's profiles: how strongly each of the 12 notes
# is felt in a major or minor key. The best-matching rotation is the key.
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def find_on_deezer(name: str, artist: str) -> dict | None:
    """Return Deezer's best match for a song, or None."""
    for query in (f'artist:"{artist}" track:"{name}"', f"{name} {artist}"):
        response = requests.get(DEEZER_SEARCH_URL, params={"q": query, "limit": 1}, timeout=30)
        response.raise_for_status()
        results = response.json().get("data", [])
        if results and results[0].get("preview"):
            return results[0]
    return None


def find_genre(name: str, artist: str, api_key: str, known: set[str]) -> str:
    """First Last.fm tag that matches a genre the model knows, else pop."""
    for method, params in (("track.getTopTags", {"track": name, "artist": artist}),
                           ("artist.getTopTags", {"artist": artist})):
        params.update(method=method, api_key=api_key, format="json", autocorrect=1)
        try:
            response = requests.get(LASTFM_URL, params=params, timeout=30)
            tags = response.json().get("toptags", {}).get("tag", [])
        except (requests.RequestException, ValueError):
            continue
        for tag in tags[:10]:
            text = tag["name"].strip().lower()
            genre = TAG_TO_GENRE.get(text, text.replace(" ", "-"))
            if genre in known:
                return genre
    return FALLBACK_GENRE


def measure(audio_path: str) -> dict:
    """Tempo, loudness, key and mode from an audio file."""
    signal, rate = librosa.load(audio_path, sr=22050, mono=True)
    tempo, _ = librosa.beat.beat_track(y=signal, sr=rate)
    # Loudness: the average strength of the wave in decibels (0 = maximum).
    rms = librosa.feature.rms(y=signal)[0]
    loudness = float(20 * np.log10(max(rms.mean(), 1e-6))) + 6.2  # Spotify reads 6.2 dB louder (found with --check)
    # Key: how much of each of the 12 notes we hear, compared to every key.
    notes = librosa.feature.chroma_cqt(y=signal, sr=rate).mean(axis=1)
    best = max(((np.corrcoef(notes, np.roll(profile, k))[0, 1], k, mode)
                for k in range(12)
                for profile, mode in ((MAJOR_PROFILE, 1), (MINOR_PROFILE, 0))))
    tempo = float(np.atleast_1d(tempo)[0])
    # 0 means librosa couldn't find a beat; NaN tells the model "unknown".
    return {"tempo": tempo if tempo > 0 else float("nan"), "loudness": loudness,
            "musical_key": best[1], "mode": best[2]}


def estimate(bundle: dict, genre: str, sound: dict, duration_ms: int) -> dict:
    """Run the trained models. Inputs must be in the same order as training."""
    genre_number = bundle["genres"].index(genre) if genre in bundle["genres"] else np.nan
    row = np.array([[genre_number, sound["tempo"], sound["loudness"],
                     sound["musical_key"], sound["mode"], duration_ms]], dtype=float)
    return {mood: float(np.clip(bundle["models"][mood].predict(row)[0], 0, 1)) for mood in MOODS}


def analyze(name: str, artist: str, api_key: str, bundle: dict) -> tuple[dict, str, dict, dict] | None:
    """Steps 1 to 4 for one song. None if Deezer doesn't have it."""
    found = find_on_deezer(name, artist)
    if found is None:
        return None
    genre = find_genre(name, artist, api_key, set(bundle["genres"])) if api_key else FALLBACK_GENRE
    clip = requests.get(found["preview"], timeout=60)
    clip.raise_for_status()
    with tempfile.TemporaryDirectory() as folder:
        clip_path = os.path.join(folder, "clip.mp3")
        with open(clip_path, "wb") as f:
            f.write(clip.content)
        sound = measure(clip_path)
    moods = estimate(bundle, genre, sound, int(found["duration"]) * 1000)
    return found, genre, sound, moods


def run_check(cur: psycopg.Cursor, api_key: str, bundle: dict, limit: int | None) -> None:
    """Compare estimates with Spotify's real numbers on matched chart songs."""
    cur.execute(AVERAGES_SQL)
    averages = np.array(cur.fetchone(), dtype=float)
    cur.execute(MATCHED_SQL)
    songs = cur.fetchall()[:limit]
    print(f"Testing on {len(songs)} chart songs that have Spotify's numbers.\n")
    truth, guesses, tempo_gaps, loud_gaps = [], [], [], []
    for i, (name, artist, tempo, loudness, *real) in enumerate(songs, start=1):
        try:
            result = analyze(name, artist, api_key, bundle)
        except Exception as error:
            print(f"{i:>3}/{len(songs)}  {name}: skipped ({error})")
            continue
        if result is None:
            print(f"{i:>3}/{len(songs)}  {name}: not on Deezer, skipped")
            continue
        _, genre, sound, moods = result
        truth.append(real)
        guesses.append([moods[m] for m in MOODS])
        if loudness is not None:
            loud_gaps.append(sound["loudness"] - loudness)
        if tempo and not np.isnan(sound["tempo"]):
            tempo_gaps.append(abs(sound["tempo"] - tempo))
        print(f"{i:>3}/{len(songs)}  {name} by {artist}: {genre}")
        time.sleep(0.2)
    if not truth:
        sys.exit("Nothing to compare.")
    truth, guesses = np.array(truth, dtype=float), np.array(guesses)
    model_errors = np.nanmean(np.abs(guesses - truth), axis=0)
    lazy_errors = np.nanmean(np.abs(averages - truth), axis=0)
    print(f"\n{len(truth)} songs. Average miss on a 0 to 1 scale:\n")
    print(f"  {'feature':<18}{'model':>8}{'lazy guess':>12}")
    for mood, m, l in zip(MOODS, model_errors, lazy_errors):
        print(f"  {mood:<18}{m:>8.3f}{l:>12.3f}")
    print(f"  {'overall':<18}{model_errors.mean():>8.3f}{lazy_errors.mean():>12.3f}")
    if tempo_gaps:
        print(f"\nTempo: librosa is off from Spotify by {np.median(tempo_gaps):.0f} BPM (median)")
    if loud_gaps:
        print(f"Loudness: librosa reads {np.mean(loud_gaps):+.1f} dB compared with Spotify (average)")


def save(cur: psycopg.Cursor, song: dict) -> None:
    """Insert one song across the catalog tables and link its chart rows."""
    cur.execute("""
        INSERT INTO tracks (track_id, name, duration_ms)
        VALUES (%(track_id)s, %(name)s, %(duration_ms)s)
        ON CONFLICT (track_id) DO NOTHING""", song)
    cur.execute("INSERT INTO artists (name) VALUES (%(artist)s) ON CONFLICT (name) DO NOTHING", song)
    cur.execute("""
        INSERT INTO track_artists (track_id, artist_id, position)
        SELECT %(track_id)s, artist_id, 1 FROM artists WHERE name = %(artist)s
        ON CONFLICT DO NOTHING""", song)
    cur.execute("""
        INSERT INTO track_genres (track_id, genre_id)
        SELECT %(track_id)s, genre_id FROM genres WHERE name = %(genre)s
        ON CONFLICT DO NOTHING""", song)
    cur.execute("""
        INSERT INTO audio_features (track_id, danceability, energy, valence, acousticness,
                                    speechiness, instrumentalness, loudness, tempo,
                                    musical_key, mode, source)
        VALUES (%(track_id)s, %(danceability)s, %(energy)s, %(valence)s, %(acousticness)s,
                %(speechiness)s, %(instrumentalness)s, %(loudness)s, %(tempo)s,
                %(musical_key)s, %(mode)s, 'estimated')
        ON CONFLICT (track_id) DO NOTHING""", song)
    # sql/006 added a language column; give new songs the artist's usual
    # language from the catalog, or English if the artist is new.
    cur.execute("""
        UPDATE tracks t SET language = COALESCE((
            SELECT mode() WITHIN GROUP (ORDER BY o.language)
            FROM track_artists ta
            JOIN artists a ON a.artist_id = ta.artist_id AND a.name = %(artist)s
            JOIN tracks o ON o.track_id = ta.track_id
            WHERE o.track_id <> t.track_id AND o.language IS NOT NULL), 'English')
        WHERE t.track_id = %(track_id)s AND t.language IS NULL""", song)
    cur.execute("""
        UPDATE chart_entries SET track_id = %(track_id)s
        WHERE track_id IS NULL
          AND lastfm_track_name = %(chart_name)s
          AND lastfm_artist_name = %(chart_artist)s""", song)


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Estimate mood scores for chart songs missing from the catalog.")
    parser.add_argument("--check", action="store_true",
                        help="test the estimates on chart songs that have Spotify's numbers; saves nothing")
    parser.add_argument("--limit", type=int, default=None, help="only do this many songs")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
                        help="Postgres connection string (default: $DATABASE_URL)")
    args = parser.parse_args()

    model_path = ROOT / "models" / "mood_model.joblib"
    if not model_path.exists():
        sys.exit("No trained model yet. Run first: python pipelines/train_mood_model.py")
    bundle = joblib.load(model_path)
    api_key = os.environ.get("LASTFM_API_KEY", "").strip()

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute(ADD_SOURCE_SQL)
        conn.commit()
        if args.check:
            run_check(cur, api_key, bundle, args.limit)
            return
        cur.execute(MISSING_SQL)
        missing = cur.fetchall()[:args.limit]
        print(f"{len(missing)} chart songs aren't in the catalog yet.\n")

        added = 0
        for i, (chart_name, chart_artist) in enumerate(missing, start=1):
            label = f"{i:>3}/{len(missing)}  {chart_name} by {chart_artist}"
            try:
                result = analyze(chart_name, chart_artist, api_key, bundle)
            except Exception as error:  # one bad song shouldn't stop the rest
                print(f"{label}: skipped ({error})")
                continue
            if result is None:
                print(f"{label}: not on Deezer, skipped")
                continue
            found, genre, sound, moods = result
            duration_ms = int(found["duration"]) * 1000
            if np.isnan(sound["tempo"]):
                sound["tempo"] = None  # saved as NULL, "unknown"
            # Deezer ids are numbers, Spotify ids are 22 letters, so the
            # prefix keeps them from ever clashing.
            save(cur, {"track_id": f"deezer:{found['id']}", "name": chart_name,
                       "artist": chart_artist, "genre": genre, "duration_ms": duration_ms,
                       "chart_name": chart_name, "chart_artist": chart_artist,
                       **sound, **moods})
            conn.commit()
            added += 1
            print(f"{label}: {genre}, {sound['tempo'] or 0:.0f} BPM, "
                  f"energy {moods['energy']:.2f}, happiness {moods['valence']:.2f}")
            time.sleep(0.2)  # be polite to the free APIs

        # The ranked search view is a saved snapshot; refresh it so the new
        # songs show up in similar-song searches.
        cur.execute("SELECT to_regclass('features_ranked') IS NOT NULL")
        if added and cur.fetchone()[0]:
            cur.execute("REFRESH MATERIALIZED VIEW features_ranked")
        conn.commit()

    print(f"\nAdded {added} songs to the catalog with estimated mood scores.")


if __name__ == "__main__":
    main()
