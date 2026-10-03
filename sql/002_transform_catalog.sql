-- =====================================================================
-- 002_transform_catalog.sql : staging.kaggle_tracks -> the core model
--
-- The loader runs this after COPYing the CSV into staging, all inside one
-- transaction: either every table updates or none does.
--
-- Every INSERT uses ON CONFLICT, so re-running the load on the same CSV
-- (or a newer version of it) updates rows instead of duplicating them.
-- =====================================================================


-- Step 1: one clean, typed row per track.
-- The raw file repeats a track once per genre, so DISTINCT ON keeps the
-- first row for each track_id (ordered by the file's own row number, so
-- the result is the same every run). Rows missing an id, a name or an
-- artist are dropped here; the loader reports how many.
CREATE TEMP TABLE clean_tracks ON COMMIT DROP AS
SELECT DISTINCT ON (track_id)
    track_id,
    trim(track_name)                  AS name,
    nullif(trim(album_name), '')      AS album_name,
    artists,
    duration_ms::integer              AS duration_ms,
    (lower(explicit) = 'true')        AS explicit,
    popularity::smallint              AS popularity,
    danceability::real                AS danceability,
    energy::real                      AS energy,
    speechiness::real                 AS speechiness,
    acousticness::real                AS acousticness,
    instrumentalness::real            AS instrumentalness,
    liveness::real                    AS liveness,
    valence::real                     AS valence,
    loudness::real                    AS loudness,
    tempo::real                       AS tempo,
    key::smallint                     AS musical_key,
    mode::smallint                    AS mode,
    time_signature::smallint          AS time_signature
FROM staging.kaggle_tracks
WHERE track_id   IS NOT NULL
  AND track_name IS NOT NULL
  AND artists    IS NOT NULL
ORDER BY track_id, row_index::integer;


-- Step 2: tracks.
INSERT INTO tracks (track_id, name, album_name, duration_ms, explicit, popularity)
SELECT track_id, name, album_name, duration_ms, explicit, popularity
FROM clean_tracks
ON CONFLICT (track_id) DO UPDATE SET
    name        = EXCLUDED.name,
    album_name  = EXCLUDED.album_name,
    duration_ms = EXCLUDED.duration_ms,
    explicit    = EXCLUDED.explicit,
    popularity  = EXCLUDED.popularity;


-- Step 3: artists. string_to_array splits 'A;B' into {A,B} and unnest
-- turns that array into one row per artist.
INSERT INTO artists (name)
SELECT DISTINCT trim(a.name)
FROM clean_tracks c
CROSS JOIN LATERAL unnest(string_to_array(c.artists, ';')) AS a(name)
WHERE trim(a.name) <> ''
ON CONFLICT (name) DO NOTHING;


-- Step 4: track_artists. WITH ORDINALITY numbers the split pieces 1, 2, 3...
-- which gives us each artist's position on the track. GROUP BY handles the
-- odd row where the same artist is listed twice.
INSERT INTO track_artists (track_id, artist_id, position)
SELECT c.track_id, ar.artist_id, min(a.pos)
FROM clean_tracks c
CROSS JOIN LATERAL unnest(string_to_array(c.artists, ';')) WITH ORDINALITY AS a(name, pos)
JOIN artists ar ON ar.name = trim(a.name)
GROUP BY c.track_id, ar.artist_id
ON CONFLICT (track_id, artist_id) DO UPDATE SET position = EXCLUDED.position;


-- Step 5: genres and track_genres. These read from staging directly, not
-- clean_tracks, because we want every genre row, not just one per track.
-- The join to clean_tracks skips rows that were dropped in step 1.
INSERT INTO genres (name)
SELECT DISTINCT trim(s.track_genre)
FROM staging.kaggle_tracks s
JOIN clean_tracks c ON c.track_id = s.track_id
WHERE nullif(trim(s.track_genre), '') IS NOT NULL
ON CONFLICT (name) DO NOTHING;

INSERT INTO track_genres (track_id, genre_id)
SELECT DISTINCT s.track_id, g.genre_id
FROM staging.kaggle_tracks s
JOIN genres g ON g.name = trim(s.track_genre)
JOIN clean_tracks c ON c.track_id = s.track_id
ON CONFLICT DO NOTHING;


-- Step 6: audio_features.
INSERT INTO audio_features (
    track_id, danceability, energy, speechiness, acousticness,
    instrumentalness, liveness, valence, loudness, tempo,
    musical_key, mode, time_signature
)
SELECT
    track_id, danceability, energy, speechiness, acousticness,
    instrumentalness, liveness, valence, loudness, tempo,
    musical_key, mode, time_signature
FROM clean_tracks
ON CONFLICT (track_id) DO UPDATE SET
    danceability     = EXCLUDED.danceability,
    energy           = EXCLUDED.energy,
    speechiness      = EXCLUDED.speechiness,
    acousticness     = EXCLUDED.acousticness,
    instrumentalness = EXCLUDED.instrumentalness,
    liveness         = EXCLUDED.liveness,
    valence          = EXCLUDED.valence,
    loudness         = EXCLUDED.loudness,
    tempo            = EXCLUDED.tempo,
    musical_key      = EXCLUDED.musical_key,
    mode             = EXCLUDED.mode,
    time_signature   = EXCLUDED.time_signature;
