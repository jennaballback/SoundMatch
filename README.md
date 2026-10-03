# SoundMatch

A small data project for learning SQL, pipelines and data modeling with two features:

1. **US top tracks**: pull the Last.fm US chart every day and keep every day's snapshot, so you can ask questions like "biggest climbers this week".
2. **Similar songs**: give it a song, get back songs that sound alike, based on Spotify audio features (energy, valence, tempo and so on) from a Kaggle dataset.

## Project layout

```
docker-compose.yml          Postgres 16 in Docker
.env.example                settings template (copy to .env)
requirements.txt            Python packages
sql/001_schema.sql          the data model: every table, with comments explaining why
sql/002_transform_catalog.sql   turns the raw CSV into clean tables
sql/003_transform_chart.sql     week 2, you write it: raw Last.fm JSON -> chart_entries
sql/004_match_chart.sql         week 2, you write it: link chart songs to catalog tracks
sql/005_similar_songs.sql       week 3: the similar_songs() search function
sql/006_track_language.sql      best guess at each song's language (run before 005)
pipelines/load_catalog.py   the catalog loader (runs 001 and 002)
pipelines/fetch_chart.py    the daily chart job (runs 001, 003 and 004)
pipelines/similar_songs.py  type a song, get similar-sounding songs
app.py                      the Streamlit web app (streamlit run app.py)
style.css                   the look of the app (colors, track lists)
data/sample_tracks.csv      14 made-up rows for testing the loader
data/sample_lastfm_chart.json   a made-up 10-song chart for testing the chart job
exercises/week1.sql         your week 1 SQL exercises
WEEK2.md                    week 2 step-by-step guide
```

## Setup (one time)

New to Docker or Python? Follow [SETUP.md](SETUP.md), which goes step by step from zero. The short version:

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) and Python 3.10 or newer.

```bash
# 1. Start Postgres
docker compose up -d

# 2. Install the Python packages in a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Test the loader on the small sample
python pipelines/load_catalog.py data/sample_tracks.csv
```

You should see 11 tracks, 6 artists and 8 genres. Run it again and the numbers stay the same, which shows the load is safe to repeat.

## Get the real data

1. Make a free Kaggle account and open the **Spotify Tracks Dataset** by Maharshi Pandya: https://www.kaggle.com/datasets/maharshipandya/-spotify-tracks-dataset (if the link moves, search Kaggle for "spotify tracks dataset maharshipandya"; it has about 114,000 rows across 114 genres).
2. Click **Download**, unzip it, and put `dataset.csv` in the `data/` folder.
3. Load it:

   ```bash
   python pipelines/load_catalog.py data/dataset.csv
   ```

   Expect roughly 89,000 tracks, because the raw file repeats a song once for every genre it appears in.

## Poke around in SQL

```bash
docker compose exec db psql -U music
```

Useful psql commands: `\dt` lists tables, `\d tracks` describes one, `\q` quits. Then work through `exercises/week1.sql`.

## How the pipeline works

```
dataset.csv  --COPY-->  staging.kaggle_tracks  --002_transform-->  tracks, artists, track_artists,
                        (all text, raw)                              genres, track_genres, audio_features
```

A few choices worth knowing about. The comments in `sql/001_schema.sql` go deeper.

- **Staging first, all text.** The CSV lands untouched in a table where every column is text, so one bad value can't break the load. Types are converted in SQL afterwards. This is the ELT pattern (extract, load, then transform), which is how most modern pipelines work.
- **One transaction.** Schema, COPY and transform run together. If anything fails, Postgres rolls it all back and your tables are exactly as before.
- **Idempotent.** Every insert uses `ON CONFLICT`, so re-running updates rows instead of duplicating them. Your week 2 daily chart job needs the same property.
- **Bridge tables for many-to-many.** The CSV stores artists as `"A;B;C"` in one cell. The loader splits those into `track_artists` rows so "every song by B" is a simple join.
- **Natural vs. surrogate keys.** Tracks use Spotify's own id as the primary key. Artists have no id in the data, so Postgres generates one, and the artist name is treated as unique (a simplification: two real artists can share a name).

## Roadmap

- **Week 1:** schema and catalog load, then `exercises/week1.sql`.
- **Week 2 (now):** the daily Last.fm chart job that fills `chart_entries`. Follow [WEEK2.md](WEEK2.md).
- **Week 3:** the similar-songs query.
- **Week 4:** chart history queries with window functions, and scheduling the daily job.
- **Weeks 5 and 6:** a small Streamlit front end and a portfolio README.
