# SoundMatch

SoundMatch is a music data app I built on Postgres. It does two things:

1. **Tracks the US top 100 every day.** A scheduled job pulls the Last.fm US chart each morning and stores it as a dated snapshot, so the history builds up over time instead of being overwritten.
2. **Finds songs that sound alike.** Type in a song and it returns the closest matches by audio features (energy, danceability, valence, tempo and so on) from a catalog of about 90,000 Spotify tracks. Every result links to Spotify, and the song you searched plays in an embedded player.

## Architecture

Raw data lands untouched in a `staging` schema (the Kaggle CSV via `COPY`, each day's Last.fm response as `jsonb`), and SQL scripts transform it into a normalized model: tracks, artists, genres, audio features and dated chart entries. A Python job run daily by Windows Task Scheduler extracts the chart, loads it and runs the transform and matching SQL in one transaction, while the similarity search lives in the database as a SQL function. A Streamlit front end queries Postgres directly for the search page and the daily chart page.

```
Kaggle CSV   --COPY-->  staging.kaggle_tracks    --002-->  tracks, artists, track_artists,
                                                           genres, track_genres, audio_features
                                                                    |
Last.fm API  --daily--> staging.lastfm_chart_raw --003-->  chart_entries --004--> matched to tracks
                                                                    |
                                       similar_songs() (005) + language (006) + ranks (007)
                                                                    |
                                                          Streamlit app (app.py)
```

Stack: Postgres 16 in Docker, Python 3 with psycopg 3, Streamlit.

## Design decisions

**Where the data comes from.** The Spotify API would have been the obvious source, but in late 2024 Spotify closed audio features, recommendations and editorial playlists to new apps. So the catalog and audio features come from the [Kaggle Spotify Tracks dataset](https://www.kaggle.com/datasets/maharshipandya/-spotify-tracks-dataset), and the chart comes from Last.fm's `geo.getTopTracks`. The trade-off is that the Kaggle data stops around 2022, which shows up in the chart matching below.

**Staging first, all text (ELT).** The CSV lands in a table where every column is text, so one bad value can't break the load. Types are cast in SQL afterwards. Last.fm responses are kept as raw `jsonb` for the same reason: if the API changes shape or my transform has a bug, I can fix the SQL and re-run it without calling the API again.

**Idempotent loads.** Every insert uses `ON CONFLICT`, and each load runs in a single transaction. Running the catalog load or the daily chart job twice gives the same result as running it once, and a failure halfway rolls everything back. That matters for a scheduled job: if it runs twice on the same day, or I re-run it by hand after a failure, nothing gets duplicated.

**Natural vs. surrogate keys.** Tracks use Spotify's own track id as the primary key, since it's stable and it lets every song link straight to Spotify. Artists have no id in the data, so Postgres generates one and the artist name is treated as unique (a known simplification: two real artists can share a name). Chart entries are keyed on `(chart_date, rank)`, which is what makes re-loading a day safe.

**Bridge tables for many-to-many.** The CSV stores artists as `"A;B;C"` in one cell, and a song can appear under several genres. Those are split into `track_artists` and `track_genres`, so "every song by B" is a simple join.

**Unmatched chart songs are kept, not dropped.** Last.fm and Spotify share no id, so chart songs are matched to the catalog on name and main artist. When the catalog has several versions of a song (single, album, deluxe), I pick the most popular one with `DISTINCT ON`. `chart_entries.track_id` stays `NULL` when there's no match, so no chart data is lost. Right now about 29% of the chart matches, because most current hits were released after the catalog ends.

**Similarity as a SQL function.** `similar_songs()` computes Euclidean distance over eight audio features. The raw features are spread very unevenly (most songs sit between 0.5 and 0.9 on energy, while valence covers the whole 0 to 1 range), so `sql/007_ranked_features.sql` replaces six of them with their percentile rank among all songs, stored in a materialized view. That way a step of 0.1 means the same thing for every feature. Speechiness and instrumentalness keep their raw values, because most songs score near 0 on both and ranking would blow tiny differences up. A few rules came out of testing real searches:
- Songs that share no genre with the seed get a penalty. I started at 0.1, but it was too weak (Kid Rock still came up for Dua Lipa), so I raised it to 0.2, and then to 0.3 once ranking spread the songs further apart.
- Results are deduplicated by name and artist, and anything at distance 0 is hidden, since that's the same recording under another title.
- There's an optional "same language only" filter.

Keeping it in the database means psql, the command-line script and the web app all run the same query.

**Inferring language.** The dataset has no language column, so `sql/006_track_language.sql` makes a best guess in order of confidence: the title's script (Korean, Japanese, Cyrillic, Devanagari and so on), instrumentalness for instrumental tracks, language-specific genres (k-pop, sertanejo, pop-film for Indian film songs), then the artist's usual language, and finally English as a fallback. It's a heuristic with known misses: a Punjabi or Italian song filed under plain "pop" can still be guessed as English.

## How I built this with Claude

I used Claude as a pair programmer throughout. I directed the project: what to build, what to leave out, and which trade-offs to make when the data didn't cooperate. Claude drafted the code and explained the options. I reviewed each piece, ran everything on my own machine (Docker, the daily scheduled job, the app) and tested the results against real searches. Several design decisions came out of that testing, like raising the genre penalty, hiding zero-distance duplicates and adding the language filter.

## Project layout

```
docker-compose.yml              Postgres 16 in Docker
sql/001_schema.sql              the data model, with comments on each table
sql/002_transform_catalog.sql   staging CSV -> clean catalog tables
sql/003_transform_chart.sql     raw Last.fm JSON -> chart_entries
sql/004_match_chart.sql         link chart songs to catalog tracks
sql/005_similar_songs.sql       the similar_songs() search function
sql/006_track_language.sql      best guess at each song's language (run before 005)
sql/007_ranked_features.sql     features as percentile ranks, a materialized view (run before 005)
pipelines/load_catalog.py       catalog loader (runs 001 and 002)
pipelines/fetch_chart.py        daily chart job (runs 001, 003 and 004)
pipelines/run_daily.bat         the script Windows Task Scheduler runs each morning
pipelines/similar_songs.py      command-line version of the search
app.py, style.css, .streamlit/  the Streamlit web app
data/                           small sample files for testing without the real data
```

## Run it locally

You need Docker and Python 3.10 or newer. [SETUP.md](SETUP.md) has a step-by-step version for Windows and Mac.

```bash
docker compose up -d                       # start Postgres
python -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                       # add a free Last.fm API key

# Catalog: download dataset.csv from the Kaggle link above into data/
python pipelines/load_catalog.py data/dataset.csv

# Language guesses and the search function
docker compose exec -T db psql -U music < sql/006_track_language.sql
docker compose exec -T db psql -U music < sql/007_ranked_features.sql
docker compose exec -T db psql -U music < sql/005_similar_songs.sql

# Today's chart (or --file data/sample_lastfm_chart.json to test offline)
python pipelines/fetch_chart.py

streamlit run app.py
```

To try it without downloading anything, `python pipelines/load_catalog.py data/sample_tracks.csv` loads a small made-up sample.
