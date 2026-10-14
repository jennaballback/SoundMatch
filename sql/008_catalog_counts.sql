-- How big the catalog is, one row per day.
--
-- The daily job runs this file after it adds new chart songs
-- (pipelines/count_catalog.py), so the table becomes a history of how the
-- catalog grows. The Search page shows the latest numbers and a line of
-- songs per day.
--
-- Safe to run as often as you like: running it twice on the same day
-- replaces that day's row (ON CONFLICT ... DO UPDATE) instead of adding a
-- second one.

CREATE TABLE IF NOT EXISTS catalog_counts (
    count_date date PRIMARY KEY,
    songs      integer NOT NULL,   -- every row in tracks
    estimated  integer NOT NULL,   -- songs the daily job added from Deezer previews
    artists    integer NOT NULL,
    counted_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO catalog_counts (count_date, songs, estimated, artists)
SELECT current_date,
       (SELECT count(*) FROM tracks),
       -- Songs added by analyze_chart_songs.py have ids like 'deezer:12345'.
       (SELECT count(*) FROM tracks WHERE track_id LIKE 'deezer:%'),
       (SELECT count(*) FROM artists)
ON CONFLICT (count_date) DO UPDATE
SET songs      = EXCLUDED.songs,
    estimated  = EXCLUDED.estimated,
    artists    = EXCLUDED.artists,
    counted_at = now();
