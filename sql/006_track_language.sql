-- =====================================================================
-- 006_track_language.sql : a best guess at each song's main language
--
-- The Kaggle file has no language column, so we work it out from the
-- clues we do have, in this order:
--   1. The alphabet the title is written in. Korean, Japanese, Chinese,
--      Russian, Hindi, Arabic and Thai each use their own letters, so a
--      title in those letters is a strong clue.
--   2. Mostly instrumental songs (instrumentalness > 0.5) get
--      'Instrumental', since nobody is singing.
--   3. The genre. Some genres are really languages: k-pop, mandopop,
--      sertanejo (Brazil), french, german, and so on.
--   4. The artist. A song with no clue yet takes its main artist's usual
--      language, if most of that artist's songs got one from rules 1 to 3.
--      (A Hindi singer's song filed under plain "pop" becomes Hindi when
--      most of their other songs are filed under "indian".)
--   5. Everything else is guessed as English. That's the weakest guess:
--      an Italian pop song filed under plain "pop" will be called English.
--
-- U&'[\AC00-\D7A3]' is a range of Unicode letters written as codes, so the
-- file stays plain ASCII. That one means "any Korean letter". The U& in
-- front tells Postgres to turn each \XXXX code into its character.
--
-- Safe to run again: it just recalculates every row.
-- =====================================================================

ALTER TABLE tracks ADD COLUMN IF NOT EXISTS language text;

-- Step 1: one row per track: which of the language genres it was filed under.
-- A song can be in several genres; bool_or asks "is ANY of them k-pop?".
WITH genre_clues AS (
    SELECT tg.track_id,
        bool_or(g.name = 'k-pop')                                              AS korean,
        bool_or(g.name IN ('j-pop', 'j-rock', 'j-idol', 'j-dance', 'anime'))  AS japanese,
        bool_or(g.name IN ('mandopop', 'cantopop'))                            AS chinese,
        bool_or(g.name IN ('brazil', 'mpb', 'forro', 'pagode', 'sertanejo', 'samba')) AS portuguese,
        bool_or(g.name IN ('spanish', 'latin', 'latino', 'reggaeton', 'salsa', 'tango')) AS spanish,
        bool_or(g.name = 'french')                                             AS french,
        bool_or(g.name = 'german')                                             AS german,
        bool_or(g.name = 'swedish')                                            AS swedish,
        bool_or(g.name = 'turkish')                                            AS turkish,
        bool_or(g.name = 'iranian')                                            AS persian,
        bool_or(g.name = 'malay')                                              AS malay,
        bool_or(g.name IN ('indian', 'pop-film'))                              AS indian   -- pop-film = Indian film songs (some are Tamil, still labelled Hindi)
    FROM track_genres tg
    JOIN genres g ON g.genre_id = tg.genre_id
    GROUP BY tg.track_id
),
-- Step 2: one guess per track, using the clues above.
guesses AS (
    SELECT t.track_id,
    CASE
    -- 1. The title's alphabet
    WHEN t.name ~ U&'[\AC00-\D7A3]'                 THEN 'Korean'
    WHEN t.name ~ U&'[\3040-\30FF]'                 THEN 'Japanese'   -- hiragana / katakana
    WHEN t.name ~ U&'[\4E00-\9FFF]' AND gc.japanese THEN 'Japanese'   -- kanji only, but a Japanese genre
    WHEN t.name ~ U&'[\4E00-\9FFF]'                 THEN 'Chinese'
    WHEN t.name ~ U&'[\0400-\04FF]'                 THEN 'Russian'    -- Cyrillic
    WHEN t.name ~ U&'[\0900-\097F]'                 THEN 'Hindi'      -- Devanagari
    WHEN t.name ~ U&'[\0600-\06FF]' AND gc.persian  THEN 'Persian'
    WHEN t.name ~ U&'[\0600-\06FF]'                 THEN 'Arabic'
    WHEN t.name ~ U&'[\0E00-\0E7F]'                 THEN 'Thai'
    -- 2. Nobody singing
    WHEN af.instrumentalness > 0.5                  THEN 'Instrumental'
    -- 3. Language genres
    WHEN gc.korean     THEN 'Korean'
    WHEN gc.japanese   THEN 'Japanese'
    WHEN gc.chinese    THEN 'Chinese'
    WHEN gc.portuguese THEN 'Portuguese'
    WHEN gc.spanish    THEN 'Spanish'
    WHEN gc.french     THEN 'French'
    WHEN gc.german     THEN 'German'
    WHEN gc.swedish    THEN 'Swedish'
    WHEN gc.turkish    THEN 'Turkish'
    WHEN gc.persian    THEN 'Persian'
    WHEN gc.malay      THEN 'Malay'
    WHEN gc.indian     THEN 'Hindi'
    -- No clue yet: rules 4 and 5 below fill these in
    END AS language
    FROM tracks t
    LEFT JOIN genre_clues gc    ON gc.track_id = t.track_id
    LEFT JOIN audio_features af ON af.track_id = t.track_id
),
-- Step 3: each main artist's usual language, counting only songs that got
-- a real clue above. mode() picks the most common value in a group.
-- The HAVING keeps it only when at least half of the artist's sung songs
-- have a language clue, so one Spanish collab doesn't make all of Justin
-- Bieber's songs Spanish.
artist_language AS (
    SELECT ta.artist_id,
           mode() WITHIN GROUP (ORDER BY g.language)
               FILTER (WHERE g.language <> 'Instrumental') AS language
    FROM guesses g
    JOIN track_artists ta ON ta.track_id = g.track_id AND ta.position = 1
    GROUP BY ta.artist_id
    HAVING count(*) FILTER (WHERE g.language <> 'Instrumental') * 2
        >= count(*) FILTER (WHERE g.language IS DISTINCT FROM 'Instrumental')
)
-- Step 4: save each guess on its track. COALESCE takes the first value
-- that isn't NULL: the song's own clue, then the artist's (rule 4),
-- then 'English' (rule 5).
UPDATE tracks t
SET language = COALESCE(g.language, al.language, 'English')
FROM guesses g
JOIN track_artists ta ON ta.track_id = g.track_id AND ta.position = 1
LEFT JOIN artist_language al ON al.artist_id = ta.artist_id
WHERE g.track_id = t.track_id;
