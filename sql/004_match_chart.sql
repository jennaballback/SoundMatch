-- =====================================================================
-- 004_match_chart.sql : link chart rows to catalog tracks
--                                                     (YOU WRITE THIS)
--
-- fetch_chart.py runs this after 003. Until there's SQL in here, the
-- script skips this step and every chart row stays unmatched.
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



