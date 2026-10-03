-- =====================================================================
-- Week 1 exercises: get to know your catalog
--
-- Run these in psql (see the README) after loading the full Kaggle file.
-- Write your query under each question. Hints are there if you get stuck;
-- try without them first. Share your answers in the thread for feedback.
-- =====================================================================


-- 1. How many tracks, artists and genres are there?
--    Can you get all three numbers from ONE query?
--    Hint: a SELECT can contain several scalar subqueries: SELECT (SELECT ...), (SELECT ...)



-- 2. The 10 artists with the most tracks.
--    Hint: JOIN artists to track_artists, GROUP BY, ORDER BY count(*) DESC, LIMIT.



-- 3. Tracks with more than 3 artists. Show the track name and the artist
--    names as one comma-separated string, main artist first.
--    Hint: string_agg(a.name, ', ' ORDER BY ta.position) and HAVING.



-- 4. Average energy and valence per genre. Which genre is the happiest?
--    Which is the saddest? Round to 2 decimals.
--    Hint: round(avg(x)::numeric, 2). avg() of a real gives a double precision,
--    and round(value, digits) only exists for numeric.



-- 5. Find a song by name, ignoring upper/lowercase, e.g. 'blinding lights'.
--    You'll probably get more than one row. Why? Look at album_name.
--    Hint: WHERE lower(t.name) = lower('...') or t.name ILIKE '%...%'



-- 6. (Modeling question, answer in words.) The same song often appears
--    under several track_ids: the single, the album version, a deluxe
--    edition. How would that hurt the similar-songs feature? What could
--    you add to the schema, or to a query, to handle it?



-- 7. (Preview of week 3.) Pick one track_id. Find the 10 tracks whose
--    energy, valence and danceability are closest to it.
--    Hint: distance = sqrt( (a.energy - b.energy)^2 + (a.valence - b.valence)^2 + ... )
--    Join audio_features to itself (a = your song, b = every other song),
--    and remember to exclude the song itself.
