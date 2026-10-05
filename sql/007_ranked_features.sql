-- =====================================================================
-- 007_ranked_features.sql : put every feature on the same fair scale
--
-- The raw features are spread very unevenly. Most songs have energy
-- between 0.5 and 0.9, while valence (happiness) is spread from 0 to 1.
-- So a 0.1 step in energy is a big jump, but a 0.1 step in valence is a
-- small one, and the raw numbers can't tell them apart.
--
-- This file replaces each value with its RANK among all songs, as a
-- number from 0 to 1: 0.9 means "higher than 90% of songs". Ranks are
-- spread evenly, so a 0.1 step means the same thing for every feature.
--
-- Speechiness and instrumentalness stay as they are. Most songs score
-- about 0 on both, so ranks would turn tiny meaningless differences
-- (0.00 vs 0.01) into huge ones. Their raw values are already a clear
-- "how much talking / how little singing" score from 0 to 1.
--
-- A MATERIALIZED VIEW is a saved query result: Postgres runs the query
-- once and keeps the rows, so searches don't re-rank 89,751 songs every
-- time. If you ever load more songs into the catalog, update it with:
--     REFRESH MATERIALIZED VIEW features_ranked;
--
-- round(..., 2) makes nearly equal values (0.501 and 0.503) share a rank.
-- Safe to run again.
-- =====================================================================

DROP MATERIALIZED VIEW IF EXISTS features_ranked;

CREATE MATERIALIZED VIEW features_ranked AS
SELECT track_id,
    percent_rank() OVER (ORDER BY round(danceability::numeric, 2)) AS danceability,
    percent_rank() OVER (ORDER BY round(energy::numeric, 2))       AS energy,
    percent_rank() OVER (ORDER BY round(valence::numeric, 2))      AS valence,
    percent_rank() OVER (ORDER BY round(acousticness::numeric, 2)) AS acousticness,
    percent_rank() OVER (ORDER BY round(tempo::numeric))           AS tempo,      -- whole beats per minute
    percent_rank() OVER (ORDER BY round(loudness::numeric, 1))     AS loudness,
    speechiness::float8      AS speechiness,
    instrumentalness::float8 AS instrumentalness
FROM audio_features;

-- One row per song, and a fast way to find a song's row
CREATE UNIQUE INDEX ON features_ranked (track_id);

-- Quick check: every ranked column should average about 0.5
SELECT count(*) AS songs,
       round(avg(danceability)::numeric, 2) AS avg_danceability,
       round(avg(energy)::numeric, 2)       AS avg_energy,
       round(avg(tempo)::numeric, 2)        AS avg_tempo
FROM features_ranked;
