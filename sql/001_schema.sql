-- =====================================================================
-- 001_schema.sql : the data model
--
-- Every statement uses IF NOT EXISTS, so running this file twice is safe.
-- That property ("idempotent") matters for pipelines: you want to be able
-- to re-run any step without breaking or duplicating anything.
-- =====================================================================


-- ---------------------------------------------------------------------
-- STAGING: a landing zone for raw files, kept separate from clean data.
--
-- Every column is TEXT on purpose. If one row in a 100k-row CSV has
-- "N/A" in a number column, a typed table would reject the whole COPY.
-- Loading as text first means the raw file always lands, and we do the
-- type conversion (and throw out bad rows) in SQL, where we can see it.
-- This load-raw-then-transform pattern is called ELT.
-- ---------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS staging;

CREATE TABLE IF NOT EXISTS staging.kaggle_tracks (
    row_index        text,   -- the CSV's unnamed first column
    track_id         text,
    artists          text,   -- several artists joined with ';'
    album_name       text,
    track_name       text,
    popularity       text,
    duration_ms      text,
    explicit         text,   -- 'True' / 'False'
    danceability     text,
    energy           text,
    key              text,
    loudness         text,
    mode             text,
    speechiness      text,
    acousticness     text,
    instrumentalness text,
    liveness         text,
    valence          text,
    tempo            text,
    time_signature   text,
    track_genre      text
);


-- ---------------------------------------------------------------------
-- CORE MODEL (lives in the default "public" schema)
-- ---------------------------------------------------------------------

-- One row per song. Spotify's own 22-character id is the primary key:
-- it is already unique and stable, so there is no reason to invent one.
-- (That's a "natural key". Compare artists below, which gets a made-up
-- "surrogate key" because the data has no artist id.)
CREATE TABLE IF NOT EXISTS tracks (
    track_id     text PRIMARY KEY,
    name         text NOT NULL,
    album_name   text,
    duration_ms  integer  CHECK (duration_ms > 0),
    explicit     boolean  NOT NULL DEFAULT false,
    popularity   smallint CHECK (popularity BETWEEN 0 AND 100)
);

-- One row per artist. The Kaggle file only gives artist *names*, so we
-- generate an integer id and treat the name as unique. That's a known
-- simplification: two different real artists can share a name.
CREATE TABLE IF NOT EXISTS artists (
    artist_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL UNIQUE
);

-- A song can have several artists and an artist has many songs: that's a
-- many-to-many relationship, which always needs a "bridge" table like
-- this. Storing 'Artist A;Artist B' in one column (like the CSV does)
-- would make "all songs by Artist B" slow and awkward to query.
CREATE TABLE IF NOT EXISTS track_artists (
    track_id   text    NOT NULL REFERENCES tracks  ON DELETE CASCADE,
    artist_id  integer NOT NULL REFERENCES artists ON DELETE CASCADE,
    position   smallint NOT NULL,          -- 1 = main artist, 2+ = featured
    PRIMARY KEY (track_id, artist_id)
);
-- The primary key already indexes lookups by track_id. This index covers
-- the other direction: "every track for this artist".
CREATE INDEX IF NOT EXISTS track_artists_artist_idx ON track_artists (artist_id);

-- Genres, same many-to-many pattern. In the Kaggle file the same track
-- appears once per genre it was sampled under, which is why the raw CSV
-- has duplicate track_ids.
CREATE TABLE IF NOT EXISTS genres (
    genre_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS track_genres (
    track_id  text    NOT NULL REFERENCES tracks ON DELETE CASCADE,
    genre_id  integer NOT NULL REFERENCES genres ON DELETE CASCADE,
    PRIMARY KEY (track_id, genre_id)
);
CREATE INDEX IF NOT EXISTS track_genres_genre_idx ON track_genres (genre_id);

-- Audio features, one row per track (a one-to-one with tracks). They could
-- live in the tracks table, but splitting them keeps "what the song is"
-- apart from "how it sounds", and it's the table the similar-songs query
-- will scan. The CHECKs document the valid ranges and reject bad data.
CREATE TABLE IF NOT EXISTS audio_features (
    track_id          text PRIMARY KEY REFERENCES tracks ON DELETE CASCADE,
    danceability      real CHECK (danceability     BETWEEN 0 AND 1),
    energy            real CHECK (energy           BETWEEN 0 AND 1),
    speechiness       real CHECK (speechiness      BETWEEN 0 AND 1),
    acousticness      real CHECK (acousticness     BETWEEN 0 AND 1),
    instrumentalness  real CHECK (instrumentalness BETWEEN 0 AND 1),
    liveness          real CHECK (liveness         BETWEEN 0 AND 1),
    valence           real CHECK (valence          BETWEEN 0 AND 1),  -- 1 = happy
    loudness          real,                                           -- dB, about -60 to 0
    tempo             real CHECK (tempo >= 0),                        -- beats per minute
    musical_key       smallint CHECK (musical_key BETWEEN -1 AND 11), -- 0 = C, 1 = C#, ... -1 = unknown
    mode              smallint CHECK (mode IN (0, 1)),                -- 1 = major, 0 = minor
    time_signature    smallint
);

-- Daily US top tracks from Last.fm (filled in week 2).
-- One row per (day, rank position). The primary key is what makes the
-- daily job safe to re-run: loading 2026-09-23 twice can't create
-- duplicate rows, it can only update the existing ones.
--
-- We keep Last.fm's own track/artist text because matching it to our
-- catalog is fuzzy and will sometimes fail. track_id stays NULL for songs
-- we couldn't match, so no chart data is ever lost.
CREATE TABLE IF NOT EXISTS chart_entries (
    chart_date          date     NOT NULL,
    rank                smallint NOT NULL CHECK (rank > 0),
    lastfm_track_name   text     NOT NULL,
    lastfm_artist_name  text     NOT NULL,
    listeners           integer,
    track_id            text REFERENCES tracks,   -- NULL = not matched yet
    loaded_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chart_date, rank)
);
CREATE INDEX IF NOT EXISTS chart_entries_track_idx ON chart_entries (track_id);


-- ---------------------------------------------------------------------
-- WEEK 2: raw Last.fm responses.
--
-- Same idea as staging.kaggle_tracks: land the data untouched first,
-- transform it with SQL second. Here the raw data is a JSON document, so
-- it goes in a jsonb column (Postgres's binary JSON type, which you can
-- query with -> and ->>). One row per day. If the API changes its format
-- or our transform has a bug, the original response is still here and we
-- can re-run the transform without calling the API again.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.lastfm_chart_raw (
    chart_date  date PRIMARY KEY,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    payload     jsonb NOT NULL
);
