-- =====================================================================
-- 005_similar_songs.sql : the similar-songs search
--
-- Saves the search as a function, so anything (psql, Python, the
-- Streamlit app) can ask for similar songs with one short line:
--
--     SELECT * FROM similar_songs('0VjIjW4GlUZAMYd2vXMi3b', 10);
--     SELECT * FROM similar_songs('0VjIjW4GlUZAMYd2vXMi3b', 10, true);  -- same language only
--
-- Version 2 adds two things:
--   * Genre counts. A song that shares no genre with yours gets a fixed
--     penalty of 0.2 added in, like a 9th feature. Typical close matches
--     are 0.03 to 0.08 apart, so 0.2 pushes other-genre songs well
--     down the list (0.1 was too weak: Kid Rock still matched Dua Lipa).
--   * Language (from 006_track_language.sql) is returned, and you can ask
--     for same-language songs only.
-- Version 3 also returns each song's track_id, so the web app can link
-- every result to Spotify.
-- Version 4 compares songs on the fair 0-to-1 ranks from
-- 007_ranked_features.sql instead of the raw numbers, so every feature
-- counts equally. Ranks spread songs out more, so the genre penalty went
-- up from 0.2 to 0.3 to keep the same push.
--
-- The DROP is needed because the function's inputs and outputs changed,
-- and CREATE OR REPLACE can only replace a function with the same shape.
-- Run 006_track_language.sql and 007_ranked_features.sql before this file.
-- =====================================================================

DROP FUNCTION IF EXISTS similar_songs(text, integer);
DROP FUNCTION IF EXISTS similar_songs(text, integer, boolean);

CREATE FUNCTION similar_songs(seed_id text, how_many integer DEFAULT 10,
                              same_language boolean DEFAULT false)
RETURNS TABLE (name text, artist text, language text, distance numeric, track_id text)
LANGUAGE sql STABLE
AS $$
WITH seed AS (
  SELECT f.*, t.name AS seed_name, t.language AS seed_language
  FROM features_ranked f
  JOIN tracks t ON t.track_id = f.track_id
  WHERE f.track_id = seed_id
),
-- Every track that shares at least one genre with the seed song.
same_genre AS (
  SELECT DISTINCT tg.track_id
  FROM track_genres tg
  WHERE tg.genre_id IN (SELECT genre_id FROM track_genres WHERE track_id = seed_id)
),
scored AS (
  SELECT t.name, a.name AS artist, t.language, t.track_id,
    round(sqrt(
        power(f.danceability     - s.danceability, 2)
      + power(f.energy           - s.energy, 2)
      + power(f.valence          - s.valence, 2)
      + power(f.acousticness     - s.acousticness, 2)
      + power(f.speechiness      - s.speechiness, 2)
      + power(f.instrumentalness - s.instrumentalness, 2)
      + power(f.tempo            - s.tempo, 2)
      + power(f.loudness         - s.loudness, 2)
      + power(CASE WHEN sg.track_id IS NULL THEN 0.3 ELSE 0 END, 2)  -- no shared genre
    )::numeric, 3) AS distance
  FROM features_ranked f
  CROSS JOIN seed s
  JOIN tracks t ON t.track_id = f.track_id
  JOIN track_artists ta ON ta.track_id = f.track_id AND ta.position = 1
  JOIN artists a ON a.artist_id = ta.artist_id
  LEFT JOIN same_genre sg ON sg.track_id = f.track_id
  WHERE lower(t.name) <> lower(s.seed_name)
    AND (NOT same_language OR t.language = s.seed_language)
),
one_per_song AS (
  SELECT DISTINCT ON (lower(name), artist) name, artist, language, distance, track_id
  FROM scored
  ORDER BY lower(name), artist, distance
)
SELECT * FROM one_per_song
WHERE distance > 0   -- 0 = the same recording under another title, e.g. "Levitating" vs "Levitating (feat. DaBaby)"
ORDER BY distance
LIMIT how_many;
$$;
