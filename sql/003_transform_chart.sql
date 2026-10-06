-- =====================================================================
-- 003_transform_chart.sql : staging.lastfm_chart_raw -> chart_entries
--                                                     (YOU WRITE THIS)
--
-- fetch_chart.py runs this file right after it saves the raw JSON.
-- Until there's SQL in here, the script just skips this step.
--
-- GOAL: one INSERT statement that turns every raw JSON payload in
-- staging.lastfm_chart_raw into rows in chart_entries, one row per track:
--     chart_date, rank, lastfm_track_name, lastfm_artist_name, listeners
-- (leave track_id out; that's the match step's job, in 004).
--
-- It's fine to rebuild every day in staging each run: that's only 100 rows
-- a day, and the upsert makes it harmless. Simple beats clever here.
--
-- Work up to it in psql one piece at a time. First load the sample:
--     python pipelines/fetch_chart.py --file data/sample_lastfm_chart.json
-- then open psql and try these in order:
--
--   a) Look at the raw row:
--        SELECT chart_date, jsonb_pretty(payload) FROM staging.lastfm_chart_raw;
--
--   b) Get the list of tracks out. -> returns JSON, ->> returns text:
--        SELECT payload -> 'tracks' -> 'track' FROM staging.lastfm_chart_raw;
--
--   c) Turn that one JSON list into one ROW per track with
--      jsonb_array_elements(...). It's a "set-returning function": you put
--      it in the FROM clause next to the table, like a join:
--        SELECT r.chart_date, t.track
--        FROM staging.lastfm_chart_raw AS r
--        CROSS JOIN LATERAL jsonb_array_elements(r.payload -> 'tracks' -> 'track') AS t(track);
--      (LATERAL means "for each row of r, run this function on that row".)
--
--   d) Now pick fields out of t.track with ->> and cast them to the right
--      types. Look at the JSON from step (a) to find the paths. Watch for:
--        * the artist name is nested one level deeper than the track name
--        * every number arrives as TEXT (like "1843201"), so cast it: ::integer
--        * Last.fm's rank starts at 0, but the table has CHECK (rank > 0).
--          What do you need to do to it?
--        * the rank lives under a key with an @ in it; quote it: -> '@attr'
--
--   e) When your SELECT returns the right 10 rows, put
--        INSERT INTO chart_entries (col1, col2, ...)
--      in front of it, and ON CONFLICT (...) DO UPDATE SET ... after it,
--      so that running the pipeline twice updates rows instead of failing.
--      You wrote nothing like this in week 1, but sql/002_transform_catalog.sql
--      has several examples. EXCLUDED.col means "the value I tried to insert".
--      Which columns should the update overwrite? (Hint: not the key
--      columns, and think about whether loaded_at should change.)
--
-- CHECK YOUR WORK: run the script twice with --file. The row count in the
-- summary should be 10 both times. Then try it with a different date:
--     python pipelines/fetch_chart.py --file data/sample_lastfm_chart.json --date 2026-09-20
-- and SELECT chart_date, count(*) FROM chart_entries GROUP BY 1;
-- =====================================================================
INSERT INTO chart_entries (chart_date, rank, lastfm_track_name, lastfm_artist_name, listeners)
SELECT r.chart_date,
       (t.track -> '@attr' ->> 'rank')::int + 1,
       t.track ->> 'name',
       t.track -> 'artist' ->> 'name',
       (t.track ->> 'listeners')::int
FROM staging.lastfm_chart_raw r
CROSS JOIN LATERAL jsonb_array_elements(r.payload -> 'tracks' -> 'track') AS t(track)
ON CONFLICT (chart_date, rank) DO UPDATE
SET lastfm_track_name  = EXCLUDED.lastfm_track_name,
    lastfm_artist_name = EXCLUDED.lastfm_artist_name,
    listeners          = EXCLUDED.listeners;


