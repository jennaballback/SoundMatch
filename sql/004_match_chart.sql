-- =====================================================================
-- 004_match_chart.sql : link chart rows to catalog tracks
--                                                     (YOU WRITE THIS)
--
-- fetch_chart.py runs this after 003. Until there's SQL in here, the
-- script skips this step and every chart row stays unmatched.
--
-- GOAL: fill in chart_entries.track_id for rows where it's still NULL,
-- by finding the same song in the tracks table. That link is what lets
-- you ask "what do today's chart hits sound like?" later on, by joining
-- chart_entries to audio_features.
--
-- WHY THIS IS HARD: Last.fm and Spotify spell things differently, and
-- there's no shared id. Start simple and improve it step by step:
--
--   Version 1: exact match on the track name AND the main artist.
--     * The main artist is in track_artists with position = 1, joined to artists.
--     * Compare lower(...) on both sides so "bad guy" = "Bad Guy".
--     * Start by writing it as a SELECT that shows chart rows next to the
--       track_id they'd get. Only turn it into an UPDATE once it looks right.
--     * UPDATE ... SET track_id = (subquery) is the easiest form to write.
--       Postgres also has UPDATE ... FROM, which is worth looking up.
--
--   Problem you'll hit: the same song is often in the catalog more than once
--   (the single, the album version, a deluxe edition...). Your subquery can
--   then return several track_ids, and Postgres will refuse with "more than
--   one row returned by a subquery". Pick one on purpose: which of the
--   duplicates would you want? (Hint: the tracks table has a popularity
--   column, and ORDER BY ... LIMIT 1 works inside a subquery.)
--
--   Version 2: look at what didn't match:
--        SELECT * FROM chart_entries WHERE track_id IS NULL;
--     Some are simply not in the catalog (the Kaggle file is from 2022, so
--     newer songs can't match; that's expected and fine). Others are near
--     misses, like "Levitating (feat. DaBaby)" vs "Levitating". Can you
--     clean those up in SQL? Look up regexp_replace() for stripping
--     " (feat. ...)" off the end of a name.
--
-- Only touch rows WHERE track_id IS NULL, so a good match from an earlier
-- day is never overwritten.
--
-- CHECK YOUR WORK: the script's summary prints how many rows matched.
-- Write down the number after each version so you can see it improve.
-- =====================================================================

UPDATE chart_entries c
SET track_id = m.track_id
FROM (
    SELECT DISTINCT ON (c2.chart_date, c2.rank) c2.chart_date, c2.rank, t.track_id
    FROM chart_entries c2
    JOIN tracks t ON lower(t.name) = lower(c2.lastfm_track_name)
    JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
    JOIN artists a ON a.artist_id = ta.artist_id AND lower(a.name) = lower(c2.lastfm_artist_name)
    WHERE c2.track_id IS NULL
    ORDER BY c2.chart_date, c2.rank, t.popularity DESC
) m
WHERE c.chart_date = m.chart_date AND c.rank = m.rank;

