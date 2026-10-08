@'
# SoundMatch

Type in a song and SoundMatch finds tracks with a similar feel, matched on energy, mood, tempo, danceability and more. It also records the US top 100 every day and tracks how songs move up and down the chart. Every song, artist and genre has its own page, and the search covers this week's new hits even though they're newer than its song data.

Behind the app is an end-to-end data project I built from the ground up: a daily data pipeline, a Postgres database of about 90,000 songs, a similarity search written in SQL, a machine learning model that fills in missing data, and a web app that ties it all together.

### Features
- **Search as you type.** Song suggestions with album covers appear as you type. Pick one to get a ranked list of the closest matches, each with a match score and a play button that opens the song on Spotify.
- **A daily chart history.** Every morning a scheduled job saves the Last.fm US top 100 as a dated snapshot. The chart page shows how many spots each song moved since the last saved chart (green up, red down) and marks new entries.
- **Song pages.** Clicking any song title opens its page: the album cover, a built-in player, bars showing how it sounds (energy, mood, danceability and more), a line graph of its chart ranks day by day, and the songs that sound closest to it. The artist and genres under the title are links to their own pages.
- **Artist pages.** Each artist gets a page with their songs, their chart stats and their average sound compared with the average song in the catalog.
- **Genre explorer.** Browse every genre with its song count. A genre's page shows how it sounds, its top songs and the genres that sound most like it, worked out in SQL by comparing every genre's average sound with every other's.
- **New releases, too.** The song dataset ends in 2022, so most of today's hits aren't in it. For each one, the pipeline grabs a 30-second preview, measures its tempo, loudness and key, and uses a model I trained to predict how it sounds. That way, brand-new songs can be searched alongside everything else. Chart songs that still can't be analyzed get a smaller page with their chart graph and a link to find them on Spotify.

## Architecture
 
Raw data lands untouched in a `staging` schema (the Kaggle CSV via `COPY`, each day's Last.fm response as `jsonb`), and SQL scripts transform it into a normalized model: tracks, artists, genres, audio features and dated chart entries. A Python job run daily by Windows Task Scheduler extracts the chart, loads it and runs the transform and matching SQL in one transaction, then estimates audio features for any chart songs the catalog doesn't have, while the similarity search lives in the database as a SQL function. A Streamlit front end queries Postgres directly for every page: search, the daily chart, and the song, artist and genre pages.
 
```
Kaggle CSV   --COPY-->  staging.kaggle_tracks    --002-->  tracks, artists, track_artists,
                                                           genres, track_genres, audio_features
                                                                    |
Last.fm API  --daily--> staging.lastfm_chart_raw --003-->  chart_entries --004--> matched to tracks
                                                                    |
                              unmatched songs: Deezer preview -> librosa -> mood model
                              -> added to tracks as "estimated" (analyze_chart_songs.py)
                                                                    |
                                       similar_songs() (005) + language (006)
                                                                    |
                                                          Streamlit app (app.py)
```
 
Stack: Postgres 16 in Docker, Python 3 with psycopg 3, Streamlit, scikit-learn and librosa. Data comes from Kaggle, Last.fm and Deezer, all free.

## Design decisions
 
**Data sources after Spotify closed its API.** Spotify stopped giving new apps audio features and recommendations in late 2024. So the song catalog and its audio features come from the [Kaggle Spotify Tracks dataset](https://www.kaggle.com/datasets/maharshipandya/-spotify-tracks-dataset), the daily chart comes from Last.fm, and 30-second previews and album covers come from Deezer. All three are free.
 
**Raw data first, safe to re-run.** Everything lands untouched in a `staging` schema (the CSV as all-text columns, each day's API response as raw `jsonb`) and is cleaned with SQL afterwards. If the API changes shape or my transform has a bug, I fix the SQL and re-run it without calling the API again. Every load runs in one transaction with `ON CONFLICT` upserts, so running the daily job twice, or re-running it after a failure, never duplicates data.
 
**Similarity search lives in the database.** `similar_songs()` is a SQL function that ranks songs by Euclidean distance over eight audio features. Tempo and loudness are rescaled so they don't outweigh the 0 to 1 features. Testing real searches led to a few rules: songs that share no genre with the one you picked get a penalty (0.1 was too weak, since Kid Rock still came up for Dua Lipa, so it's 0.2), duplicate versions of a song are hidden, and there's an optional "same language only" filter based on a language guess I built in SQL. Because the logic is in the database, psql, the command-line script and the web app all get the same results.
 
**Measuring before trusting a model.** Only about 29% of the chart matched the catalog, because most hits came out after 2022. To estimate audio features for the rest, I compared each approach with a "lazy guess" baseline (always predict the average), using mean error on six mood scores:
- Free local LLMs (llama3.2 and llama3.1:8b) did *worse* than the lazy guess (0.243 and 0.229 vs 0.198), so I dropped them.
- A scikit-learn model trained on my own catalog, using tempo, loudness, key, mode, length and genre, halved the error (0.108 vs 0.201 on 5,000 songs it never saw).
- The full chain on real 30-second previews (Deezer clip, measured with librosa, then the model) first lost to the baseline. The cause was that librosa measured songs about 6 dB quieter than Spotify. After correcting that offset, it beat the baseline on all six scores (0.120 vs 0.178).
Estimated songs are marked `source = 'estimated'`, and the app labels them so it's clear their scores are guesses.

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
pipelines/load_catalog.py       catalog loader (runs 001 and 002)
pipelines/fetch_chart.py        daily chart job (runs 001, 003 and 004)
pipelines/run_daily.bat         the script Windows Task Scheduler runs each morning
pipelines/similar_songs.py      command-line version of the search
pipelines/llm_accuracy_check.py tests free local LLMs at guessing audio features
pipelines/train_mood_model.py   trains the audio-feature model on the catalog
pipelines/analyze_chart_songs.py  estimates features for chart songs not in the catalog
app.py, style.css, .streamlit/  the Streamlit web app
song_search/index.html          the search box with live suggestions and covers
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
docker compose exec -T db psql -U music < sql/005_similar_songs.sql
 
# Today's chart (or --file data/sample_lastfm_chart.json to test offline)
python pipelines/fetch_chart.py
 
# Train the audio-feature model, then fill in chart songs the catalog doesn't have
python pipelines/train_mood_model.py
python pipelines/analyze_chart_songs.py
 
streamlit run app.py
```
 
To try it without downloading anything, `python pipelines/load_catalog.py data/sample_tracks.csv` loads a small made-up sample.
'@ | Set-Content -Path README.md -Encoding utf8
