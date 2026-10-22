"""SoundMatch: the music project web app.

Run it with:
    streamlit run app.py

Then it opens in your browser at http://localhost:8501.
Streamlit re-runs this whole file from top to bottom every time you
type or click something, so the code reads like a simple script.

The dark theme colors live in .streamlit/config.toml. The rest of the look
(font, the gradient header, the track lists) is in style.css.

Playing songs: the song you search gets a Spotify player under the header,
and the play button on every song in a list opens it on Spotify in a new tab.
The catalog came from Spotify, so its track_id is the real Spotify id.
Chart songs added by pipelines/analyze_chart_songs.py have ids like
"deezer:12345" instead, so they get Deezer's player and a Spotify search link.

Pages: Search, US top tracks (with up/down arrows against the previous
saved chart), Artists (an artist's songs, chart history and average
sound profile) and Genres (a genre's sound, its top songs and the genres
that sound most like it). Click an artist's name in any list to open their page,
and a song's title to open its song page (its sound, its chart history
and the songs that sound most like it). The play button still opens Spotify.
"""

import altair as alt

import html
from datetime import date
import os
from urllib.parse import quote
from pathlib import Path

import psycopg
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DSN = os.environ.get("DATABASE_URL", "postgresql://music:music@localhost:5432/music")

# Songs to suggest while you type. Every word you typed has to appear
# somewhere in "song name + artist", so "bli" finds Blinding Lights and
# blink-182, and "lights weeknd" finds Blinding Lights too.
# DISTINCT ON keeps one copy of each name + artist (the catalog lists some
# songs several times, once per genre), the most popular one. Then songs
# whose name starts with what you typed come first, most popular first.
SUGGEST_SQL = """
SELECT track_id, name, artist, language
FROM (
    SELECT DISTINCT ON (lower(t.name), lower(a.name))
           t.track_id, t.name, a.name AS artist, t.language, t.popularity,
           t.name ILIKE %(starts)s AS name_starts
    FROM tracks t
    JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
    JOIN artists a ON a.artist_id = ta.artist_id
    WHERE t.name || ' ' || a.name ILIKE ALL (%(words)s)
    ORDER BY lower(t.name), lower(a.name), t.popularity DESC NULLS LAST
) AS matches
ORDER BY name_starts DESC, popularity DESC NULLS LAST, name
LIMIT 8
"""

SIMILAR_SQL = "SELECT name, artist, language, distance, track_id FROM similar_songs(%s, %s, %s)"

# Every day the daily job has saved, newest first.
CHART_DATES_SQL = "SELECT DISTINCT chart_date FROM chart_entries ORDER BY chart_date DESC"

# The newest saved chart before a given day. Usually yesterday, but it can
# be further back if the daily job missed a day.
PREV_DATE_SQL = "SELECT max(chart_date) FROM chart_entries WHERE chart_date < %s"

# One day's chart, plus where each song was on the previous saved chart.
# The "prev" part finds last chart's rank for each song (matched on
# lower-cased name and artist, because Last.fm names are all we have for
# songs that aren't in the catalog). prev_rank is NULL for a new entry.
# The artist joins pick the catalog's spelling of the artist when the song
# is in the catalog, so the artist link opens the right page.
CHART_SQL = """
WITH prev AS (
    SELECT lower(lastfm_track_name) AS song, lower(lastfm_artist_name) AS artist,
           min(rank) AS rank
    FROM chart_entries
    WHERE chart_date = %(prev_day)s
    GROUP BY 1, 2
)
SELECT c.rank, c.lastfm_track_name, c.lastfm_artist_name, c.listeners, c.track_id,
       t.language, p.rank AS prev_rank, coalesce(a.name, c.lastfm_artist_name) AS link_artist
FROM chart_entries c
LEFT JOIN tracks t ON t.track_id = c.track_id
LEFT JOIN track_artists ta ON ta.track_id = c.track_id AND ta.position = 1
LEFT JOIN artists a ON a.artist_id = ta.artist_id
LEFT JOIN prev p ON p.song = lower(c.lastfm_track_name)
                AND p.artist = lower(c.lastfm_artist_name)
WHERE c.chart_date = %(day)s
ORDER BY c.rank
"""

# Every artist with at least one song, most popular first, for the
# artist picker.
ARTIST_NAMES_SQL = """
SELECT a.name
FROM artists a
JOIN track_artists ta ON ta.artist_id = a.artist_id
JOIN tracks t ON t.track_id = ta.track_id
GROUP BY a.name
ORDER BY max(t.popularity) DESC NULLS LAST, a.name
"""

# One artist's songs, counted once each (the catalog lists some songs
# several times), including songs where they're a featured artist.
# Used by the two artist queries below.
ARTIST_SONGS_CTE = """
WITH songs AS (
    SELECT DISTINCT ON (lower(t.name))
           t.track_id, t.name, t.popularity, ta.position
    FROM artists a
    JOIN track_artists ta ON ta.artist_id = a.artist_id
    JOIN tracks t ON t.track_id = ta.track_id
    WHERE lower(a.name) = lower(%(artist)s)
    ORDER BY lower(t.name), t.popularity DESC NULLS LAST
)
"""

# The artist's songs with their chart history: how many days each one was
# on the chart, its best rank and the last day it was there.
ARTIST_SONGS_SQL = ARTIST_SONGS_CTE + """,
charted AS (
    SELECT lower(t.name) AS song, count(DISTINCT c.chart_date) AS days,
           min(c.rank) AS best, max(c.chart_date) AS last_seen
    FROM chart_entries c
    JOIN tracks t ON t.track_id = c.track_id
    JOIN track_artists ta ON ta.track_id = t.track_id
    JOIN artists a ON a.artist_id = ta.artist_id
    WHERE lower(a.name) = lower(%(artist)s)
    GROUP BY 1
)
SELECT s.track_id, s.name, s.popularity, s.position, ch.days, ch.best, ch.last_seen
FROM songs s
LEFT JOIN charted ch ON ch.song = lower(s.name)
ORDER BY ch.days DESC NULLS LAST, s.popularity DESC NULLS LAST
LIMIT 50
"""

# The artist's average sound: the mean of each audio feature over their songs.
ARTIST_PROFILE_SQL = ARTIST_SONGS_CTE + """
SELECT avg(f.danceability), avg(f.energy), avg(f.valence), avg(f.acousticness),
       avg(f.speechiness), avg(f.instrumentalness), avg(f.tempo), avg(f.loudness)
FROM songs s
JOIN audio_features f ON f.track_id = s.track_id
"""

# The same averages over the whole catalog, to compare against.
CATALOG_PROFILE_SQL = """
SELECT avg(danceability), avg(energy), avg(valence), avg(acousticness),
       avg(speechiness), avg(instrumentalness), avg(tempo), avg(loudness)
FROM audio_features
"""


# Every genre with its number of songs, biggest first. One song can be in
# several genres, so it's counted in each.
GENRE_LIST_SQL = """
SELECT g.name, count(DISTINCT lower(t.name)) AS songs
FROM genres g
JOIN track_genres tg ON tg.genre_id = g.genre_id
JOIN tracks t ON t.track_id = tg.track_id
GROUP BY g.name
ORDER BY songs DESC, g.name
"""

# A genre's average sound, the same columns as ARTIST_PROFILE_SQL.
GENRE_PROFILE_SQL = """
SELECT avg(f.danceability), avg(f.energy), avg(f.valence), avg(f.acousticness),
       avg(f.speechiness), avg(f.instrumentalness), avg(f.tempo), avg(f.loudness)
FROM genres g
JOIN track_genres tg ON tg.genre_id = g.genre_id
JOIN audio_features f ON f.track_id = tg.track_id
WHERE g.name = %(genre)s
"""

# The genres that sound most like this one. First every genre's average
# sound (the "avgs" part), then the genre is joined to every other genre
# (a self-join: the same table twice, as "me" and "other") and the
# distance between their averages is measured the same way similar_songs
# does for songs: tempo and loudness scaled to 0-1, then Euclidean distance.
SIMILAR_GENRES_SQL = """
WITH avgs AS (
    SELECT g.name,
           avg(f.danceability) AS dance, avg(f.energy) AS energy,
           avg(f.valence) AS happy, avg(f.acousticness) AS acoustic,
           avg(f.speechiness) AS speech, avg(f.instrumentalness) AS instr,
           avg(f.tempo) / 250 AS tempo, (avg(f.loudness) + 60) / 60 AS loud
    FROM genres g
    JOIN track_genres tg ON tg.genre_id = g.genre_id
    JOIN audio_features f ON f.track_id = tg.track_id
    GROUP BY g.name
)
SELECT other.name,
       sqrt(power(me.dance - other.dance, 2) + power(me.energy - other.energy, 2)
          + power(me.happy - other.happy, 2) + power(me.acoustic - other.acoustic, 2)
          + power(me.speech - other.speech, 2) + power(me.instr - other.instr, 2)
          + power(me.tempo - other.tempo, 2) + power(me.loud - other.loud, 2)) AS distance
FROM avgs me
JOIN avgs other ON other.name <> me.name
WHERE me.name = %(genre)s
ORDER BY distance
LIMIT 8
"""

# A genre's most popular songs, one copy of each, plus how many of its
# songs have been on the US chart.
GENRE_SONGS_SQL = """
SELECT track_id, name, artist, language, popularity
FROM (
    SELECT DISTINCT ON (lower(t.name), lower(a.name))
           t.track_id, t.name, a.name AS artist, t.language, t.popularity
    FROM genres g
    JOIN track_genres tg ON tg.genre_id = g.genre_id
    JOIN tracks t ON t.track_id = tg.track_id
    JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
    JOIN artists a ON a.artist_id = ta.artist_id
    WHERE g.name = %(genre)s
    ORDER BY lower(t.name), lower(a.name), t.popularity DESC NULLS LAST
) AS songs
ORDER BY popularity DESC NULLS LAST, name
LIMIT 25
"""

GENRE_CHART_SQL = """
SELECT count(DISTINCT lower(c.lastfm_track_name))
FROM chart_entries c
JOIN track_genres tg ON tg.track_id = c.track_id
JOIN genres g ON g.genre_id = tg.genre_id
WHERE g.name = %(genre)s
"""


# Artists that sound like this one. Like SIMILAR_GENRES_SQL, but per artist:
#   songs:     each artist's songs where they're the main artist, one copy each
#   avgs:      each artist's average sound, tempo and loudness scaled to 0-1
#   genres_of: every genre each artist has songs in, as a list (an array)
# Then this artist ("me") is compared with every other artist. && means
# "the two lists share at least one genre"; artists that share none get the
# same 0.2 penalty similar_songs uses. Only artists with a few songs and at
# least one well-known song count, so the list has names you might know.
SIMILAR_ARTISTS_SQL = """
WITH songs AS (
    SELECT DISTINCT ON (ta.artist_id, lower(t.name))
           ta.artist_id, t.track_id, t.popularity
    FROM track_artists ta
    JOIN tracks t ON t.track_id = ta.track_id
    WHERE ta.position = 1
    ORDER BY ta.artist_id, lower(t.name), t.popularity DESC NULLS LAST
),
avgs AS (
    SELECT s.artist_id, count(*) AS songs, max(s.popularity) AS popularity,
           avg(f.danceability) AS dance, avg(f.energy) AS energy,
           avg(f.valence) AS happy, avg(f.acousticness) AS acoustic,
           avg(f.speechiness) AS speech, avg(f.instrumentalness) AS instr,
           avg(f.tempo) / 250 AS tempo, (avg(f.loudness) + 60) / 60 AS loud
    FROM songs s
    JOIN audio_features f ON f.track_id = s.track_id
    GROUP BY s.artist_id
),
genres_of AS (
    SELECT ta.artist_id, array_agg(DISTINCT tg.genre_id) AS genres
    FROM track_artists ta
    JOIN track_genres tg ON tg.track_id = ta.track_id
    WHERE ta.position = 1
    GROUP BY ta.artist_id
),
me AS (
    SELECT avgs.*, a.name, g.genres
    FROM avgs
    JOIN artists a ON a.artist_id = avgs.artist_id
    LEFT JOIN genres_of g ON g.artist_id = avgs.artist_id
    WHERE lower(a.name) = lower(%(artist)s)
    ORDER BY avgs.songs DESC
    LIMIT 1
)
SELECT a.name,
       sqrt(power(me.dance - o.dance, 2) + power(me.energy - o.energy, 2)
          + power(me.happy - o.happy, 2) + power(me.acoustic - o.acoustic, 2)
          + power(me.speech - o.speech, 2) + power(me.instr - o.instr, 2)
          + power(me.tempo - o.tempo, 2) + power(me.loud - o.loud, 2)
          + power(CASE WHEN g.genres && me.genres THEN 0 ELSE 0.2 END, 2)) AS distance
FROM me
JOIN avgs o ON o.artist_id <> me.artist_id
JOIN artists a ON a.artist_id = o.artist_id AND lower(a.name) <> lower(me.name)
LEFT JOIN genres_of g ON g.artist_id = o.artist_id
WHERE o.songs >= %(min_songs)s AND o.popularity >= %(min_popularity)s
ORDER BY distance
LIMIT 8
"""

# How big the catalog is right now.
CATALOG_NOW_SQL = """
SELECT (SELECT count(*) FROM tracks),
       (SELECT count(*) FROM tracks WHERE track_id LIKE 'deezer:%%'),
       (SELECT count(*) FROM artists)
"""

# The catalog's size on every day the daily job counted it
# (pipelines/count_catalog.py fills this table, see sql/008_catalog_counts.sql).
CATALOG_HISTORY_SQL = "SELECT count_date, songs FROM catalog_counts ORDER BY count_date"

# Everything about one song for its song page. The catalog lists some songs
# several times (once per genre), so the genres are collected from every
# copy with the same name and main artist.
SONG_SQL = """
SELECT t.track_id, t.name, a.name AS artist, t.language, t.popularity, t.album_name,
       f.danceability, f.energy, f.valence, f.acousticness,
       f.speechiness, f.instrumentalness, f.tempo, f.loudness,
       (SELECT string_agg(DISTINCT g.name, ', ')
        FROM tracks t2
        JOIN track_artists ta2 ON ta2.track_id = t2.track_id AND ta2.position = 1
        JOIN track_genres tg ON tg.track_id = t2.track_id
        JOIN genres g ON g.genre_id = tg.genre_id
        WHERE lower(t2.name) = lower(t.name) AND ta2.artist_id = ta.artist_id) AS genres
FROM tracks t
LEFT JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
LEFT JOIN artists a ON a.artist_id = ta.artist_id
LEFT JOIN audio_features f ON f.track_id = t.track_id
WHERE t.track_id = %s
"""

# The song's rank on every day it was on the chart. Any copy of the song
# (same name, same main artist) counts, since the chart may have been
# matched to a different copy.
SONG_CHART_SQL = """
SELECT c.chart_date, min(c.rank) AS rank
FROM chart_entries c
JOIN tracks t ON t.track_id = c.track_id
JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
WHERE lower(t.name) = lower(%(name)s)
  AND ta.artist_id = (SELECT artist_id FROM track_artists
                      WHERE track_id = %(track_id)s AND position = 1)
GROUP BY c.chart_date
ORDER BY c.chart_date
"""


def run_query(sql: str, params: tuple | dict = ()) -> list[tuple]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def like_pattern(text: str) -> str:
    # In ILIKE, % and _ are wildcards. A backslash in front makes them plain
    # characters, so typing "100%" looks for a real percent sign.
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def suggest_songs(text: str) -> list[dict]:
    words = [f"%{like_pattern(w)}%" for w in text.split()]
    if not words:
        return []
    rows = run_query(SUGGEST_SQL, {"words": words, "starts": like_pattern(text) + "%"})
    return [{"track_id": r[0], "name": r[1], "artist": r[2], "language": r[3] or ""}
            for r in rows]


# The search box with suggestions is a small web page of its own,
# song_search/index.html. declare_component lets Streamlit show it and
# get back what you typed and which song you clicked.
song_search_box = components.declare_component("song_search", path=str(ROOT / "song_search"))

# The chart date picker, a month calendar of its own (chart_calendar/index.html).
# It gets the days that have a saved chart and sends back the one you click.
chart_calendar = components.declare_component("chart_calendar", path=str(ROOT / "chart_calendar"))


def spotify_link(track_id: str | None, name: str, artist: str) -> str:
    # A catalog song links straight to its Spotify page. A chart song that
    # isn't in the catalog, or was added from Deezer, has no Spotify id, so
    # it links to a Spotify search.
    if track_id and not track_id.startswith("deezer:"):
        return f"https://open.spotify.com/track/{track_id}"
    return "https://open.spotify.com/search/" + quote(f"{name} {artist}")


def banner(kicker: str, title: str, sub: str, cover: str | None = None,
           sub_is_html: bool = False) -> None:
    # html.escape turns characters like < and & into safe text, so a song
    # name can never break the page. cover is a picture address; when it's
    # given, the picture sits to the left of the title (style.css).
    # sub_is_html=True means sub already has links in it (and was escaped
    # piece by piece), so it's used as it is.
    tag = "img"
    picture = f'<{tag} class="cover" src="{html.escape(cover)}" alt="">' if cover else ""
    st.markdown(
        f'<div class="banner{" with-cover" if cover else ""}">{picture}<div>'
        f'<div class="kicker">{html.escape(kicker)}</div>'
        f'<div class="title">{html.escape(title)}</div>'
        f'<div class="sub">{sub if sub_is_html else html.escape(sub)}</div></div></div>',
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=24 * 3600)
def album_cover(track_id: str | None, name: str, artist: str) -> str | None:
    # The album cover comes from Deezer's free API (no key), like the
    # covers in the search box. Songs added from Deezer are looked up by
    # their Deezer id; other songs by title and artist. Remembered for a day.
    # Returns None if Deezer is unreachable or has no match.
    try:
        if track_id and track_id.startswith("deezer:"):
            found = requests.get("https://api.deezer.com/track/" + track_id.removeprefix("deezer:"),
                                 timeout=5).json()
            return found.get("album", {}).get("cover_big")
        for query in (f'artist:"{artist}" track:"{name}"', f"{name} {artist}"):
            results = requests.get("https://api.deezer.com/search",
                                   params={"q": query, "limit": 1}, timeout=5).json().get("data")
            if results:
                return results[0]["album"]["cover_big"]
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def song_page_link(track_id: str | None, name: str = "", artist: str = "") -> str:
    # The address of a song's own page. Chart songs that aren't in the
    # catalog have no track_id, so their page is found by name and artist.
    if track_id:
        return "?song=" + quote(track_id)
    return "?chart_song=" + quote(name) + "&artist=" + quote(artist)


def artist_link(name: str, link_name: str | None = None) -> str:
    # The artist's name as a link to their artist page. target="_self" opens
    # it in the same tab. link_name is the catalog's spelling, when it differs.
    url = "?page=Artists&artist=" + quote(link_name or name)
    return f'<a class="artist-link" href="{url}" target="_self">{html.escape(name)}</a>'


def sub_link(text: str, url: str, new_tab: bool = False) -> str:
    # A plain link for the line under a page's title (style .banner .sub a).
    return (f'<a href="{html.escape(url)}" target="{"_blank" if new_tab else "_self"}">'
            f'{html.escape(text)}</a>')


def genre_link(name: str, extra: str = "") -> str:
    # A genre as a rounded "chip" that opens its genre page. extra is
    # small gray text after the name, like a song count or a match %.
    url = "?page=Genres&genre=" + quote(name)
    small = f' <span class="chip-extra">{extra}</span>' if extra else ""
    return f'<a class="genre-chip" href="{url}" target="_self">{html.escape(name)}{small}</a>'


@st.cache_data(ttl=3600)
def similar_artists(artist: str) -> list[tuple]:
    # Comparing one artist with every other artist takes a moment, so each
    # answer is remembered for an hour.
    return run_query(SIMILAR_ARTISTS_SQL,
                     {"artist": artist, "min_songs": 3, "min_popularity": 40})


def artist_chip(name: str, extra: str = "") -> str:
    # An artist as a chip like genre_link's, opening their artist page.
    url = "?page=Artists&artist=" + quote(name)
    small = f' <span class="chip-extra">{extra}</span>' if extra else ""
    return f'<a class="genre-chip" href="{url}" target="_self">{html.escape(name)}{small}</a>'


def back_button() -> None:
    # A "Back" button for song, artist and genre pages. It works like the
    # browser's own back arrow (history.back), so it returns to whatever you
    # came from. If there's nowhere to go back to (the page was opened in a
    # new tab), it goes to the home page instead.
    # It's a tiny web page of its own (st.iframe), so it can run that
    # one line of JavaScript; window.parent is the SoundMatch page around it.
    # history.length is how many pages this browser tab has visited.
    go_back = ("var p = window.parent; "
               "if (p.history.length > 1) p.history.back(); "
               "else p.location.href = p.location.pathname;")
    look = ("background:#2a2a2a; color:#ffffff; border:none; border-radius:500px; "
            "padding:6px 16px; font:600 14px Helvetica, Arial, sans-serif; cursor:pointer;")
    st.iframe(
        f'<body style="margin:0; overflow:hidden"><button onclick="{go_back}" style="{look}" '
        f'onmouseover="this.style.background=\'#3e3e3e\'" '
        f'onmouseout="this.style.background=\'#2a2a2a\'">&#8592; Back</button>',
        height=40,
    )


def movement(rank: int, prev_rank: int | None) -> str:
    # A green arrow up or red arrow down with how many spots the song moved
    # since the previous chart, a gray dash if it stayed put, or NEW.
    if prev_rank is None:
        return '<span class="move new">NEW</span>'
    if prev_rank > rank:
        return f'<span class="move up">&#9650; {prev_rank - rank}</span>'
    if prev_rank < rank:
        return f'<span class="move down">&#9660; {rank - prev_rank}</span>'
    return '<span class="move same">&#8211;</span>'


def tracklist(headers: list[str], rows: list[list[str]], moves: list[str] | None = None) -> None:
    # Each row is [number, song, artist html, Spotify link, right-hand cells html,
    # song page link]. The title opens the song page in the same tab
    # (target="_self"). Only the play button opens Spotify.
    # moves, when given, is an extra cell per row after the number (the chart arrows).
    # The number turns into a play button when you point at the row
    # (style.css does that), and the button opens Spotify.
    # target="_blank" opens the link in a new tab, so the app stays open.
    # The artist html is usually artist_link(...), so it opens the artist page.
    head = "".join(f"<th>{h}</th>" for h in headers)
    moves = moves or [None] * len(rows)
    body = "".join(
        f'<tr><td class="num"><span class="idx">{num}</span>'
        f'<a class="play" href="{html.escape(url)}" target="_blank">&#9654;</a></td>'
        f'{"" if move is None else f"<td class=move-cell>{move}</td>"}'
        f'<td><a class="song" href="{html.escape(page)}" target="_self">{html.escape(song)}</a>'
        f'<div class="artist">{artist}</div></td>'
        f"{right}</tr>"
        for (num, song, artist, url, right, page), move in zip(rows, moves)
    )
    st.markdown(f'<table class="tracklist"><tr>{head}</tr>{body}</table>',
                unsafe_allow_html=True)


def sound_profile(artist_avg: tuple, catalog_avg: tuple, note: str = "") -> None:
    # One bar per feature: green is the artist's average (or one song's
    # value, on the song page), the white tick is the average song in the
    # catalog, so you can see what makes them stand out.
    labels = ["Danceability", "Energy", "Happiness", "Acousticness",
              "Speechiness", "Instrumentalness"]
    bars = "".join(
        f'<div class="bar-row"><div class="bar-label">{label}</div>'
        f'<div class="bar"><div class="fill" style="width:{100 * float(a):.0f}%"></div>'
        f'<div class="tick" style="left:{100 * float(c):.0f}%"></div></div>'
        f'<div class="bar-value">{100 * float(a):.0f}</div></div>'
        for label, a, c in zip(labels, artist_avg[:6], catalog_avg[:6])
    )
    st.markdown(
        f'<div class="profile"><div class="profile-title">Sound profile</div>{bars}'
        f'<div class="profile-note">Average tempo {float(artist_avg[6]):.0f} BPM, '
        f'loudness {float(artist_avg[7]):.1f} dB. '
        f'White tick = the average song in the catalog.{note}</div></div>',
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=3600)
def artist_names() -> list[str]:
    # Remembered for an hour so the long list isn't fetched on every click.
    return [r[0] for r in run_query(ARTIST_NAMES_SQL)]


@st.cache_data(ttl=3600)
def genre_list() -> list[tuple]:
    return run_query(GENRE_LIST_SQL)


@st.cache_data(ttl=3600)
def catalog_profile() -> tuple:
    return run_query(CATALOG_PROFILE_SQL)[0]

def player(track_id: str) -> None:
    if track_id.startswith("deezer:"):
        # Deezer's player, with the same 30 second preview the scores came from.
        deezer_id = track_id.removeprefix("deezer:")
        st.iframe(f"https://widget.deezer.com/widget/dark/track/{deezer_id}", height=152)
    else:
        # Spotify's own player. It plays a 30 second preview, or the whole song
        # if you're logged in to Spotify in this browser.
        st.iframe(f"https://open.spotify.com/embed/track/{track_id}?theme=0", height=152)


def similar_list(rows: list[tuple]) -> None:
    # A distance of 0 is identical. Turn it into a friendlier "% match"
    # (distance 0.05 shows as 95%).
    tracklist(
        ["#", "Title", "Language", "Match"],
        [[str(i), r[0], artist_link(r[1]), spotify_link(r[4], r[0], r[1]),
          f'<td class="lang">{html.escape(r[2] or "")}</td>'
          f'<td class="match">{max(0, round(100 * (1 - float(r[3]))))}%</td>',
          song_page_link(r[4])]
         for i, r in enumerate(rows, start=1)],
    )


def show_similar(track_id: str, name: str, artist: str, language: str,
                 how_many: int, same_language: bool) -> None:
    rows = run_query(SIMILAR_SQL, (track_id, how_many, same_language))
    if track_id.startswith("deezer:"):
        banner("SOUNDS LIKE", name,
               f"{artist}  -  {language}  -  {len(rows)} songs  -  mood scores estimated")
    else:
        banner("SOUNDS LIKE", name, f"{artist}  -  {language}  -  {len(rows)} songs")
    player(track_id)
    st.markdown(f'<a class="page-link" href="{song_page_link(track_id)}" target="_self">'
                f'Open the song page for {html.escape(name)} &#8594;</a>',
                unsafe_allow_html=True)
    similar_list(rows)


def chart_history(days: list[tuple]) -> None:
    # The song's rank on each chart day as a line. Rank 1 is the top, so the
    # y axis is flipped (reverse=True) to put #1 at the top like a real chart.
    best = min(r[1] for r in days)
    st.markdown(
        f'<div class="section-title">On the US chart</div>'
        f'<div class="section-sub">{len(days)} day{"" if len(days) == 1 else "s"}, '
        f'best rank #{best}, last seen {days[-1][0]:%B %d}</div>',
        unsafe_allow_html=True,
    )
    # Each day is a label like "Oct 05", kept in date order (sort=None), so
    # the axis shows exactly the days that were saved.
    data = [{"Day": f"{d:%b %d}", "Rank": r} for d, r in days]
    line = alt.Chart(alt.Data(values=data)).mark_line(
        color="#1ed760", point=alt.OverlayMarkDef(color="#1ed760", size=60)
    ).encode(
        x=alt.X("Day:O", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("Rank:Q", title="Rank", axis=alt.Axis(tickMinStep=1, format="d"),
                scale=alt.Scale(reverse=True, domain=[1, max(r[1] for r in days) + 2])),
        tooltip=["Day:O", "Rank:Q"],
    ).properties(height=220)
    st.altair_chart(line, width="stretch")


@st.cache_data(ttl=600)
def catalog_size() -> tuple[tuple, list[tuple]]:
    # The catalog's size now, plus its size on each day the daily job
    # counted it. Remembered for 10 minutes. to_regclass is NULL when the
    # catalog_counts table doesn't exist yet (before the first daily run).
    now = run_query(CATALOG_NOW_SQL)[0]
    has_table = run_query("SELECT to_regclass('catalog_counts')")[0][0]
    return now, run_query(CATALOG_HISTORY_SQL) if has_table else []


def catalog_tracker() -> None:
    # Three numbers about the catalog.
    (songs, estimated, artists), history = catalog_size()
    earlier = [r for r in history if r[0] < date.today()]
    if earlier:
        change = songs - earlier[-1][1]
        shown = f'<span class="stat-up">{change:+,}</span>' if change > 0 else f"{change:+,}"
        since = f"{shown} since {earlier[-1][0]:%b %d}"
    else:
        since = "counted every day by the daily job"
    tiles = [("Songs in the catalog", f"{songs:,}", since),
             ("Added from the US chart", f"{estimated:,}", "new hits measured from previews"),
             ("Artists", f"{artists:,}", "")]
    st.markdown(
        '<div class="section-title">The catalog</div><div class="stats">'
        + "".join(f'<div class="stat"><div class="stat-label">{label}</div>'
                  f'<div class="stat-value">{value}</div>'
                  f'<div class="stat-sub">{sub}</div></div>'
                  for label, value, sub in tiles)
        + "</div>", unsafe_allow_html=True)


def song_page(track_id: str) -> None:
    back_button()
    found = run_query(SONG_SQL, (track_id,))
    if not found:
        st.warning("That song isn't in the catalog.")
        return
    (track_id, name, artist, language, popularity, album,
     *features, genres) = found[0]
    estimated = track_id.startswith("deezer:")
    # The line under the title: the artist and each genre are links to
    # their pages, the rest is plain text.
    facts = [sub_link(artist, "?page=Artists&artist=" + quote(artist)) if artist else "",
             html.escape(language or ""),
             ", ".join(sub_link(g, "?page=Genres&genre=" + quote(g))
                       for g in (genres or "").split(", ") if g),
             f"from {html.escape(album)}" if album else "",
             f"popularity {popularity}" if popularity is not None else "",
             "mood scores estimated" if estimated else ""]
    banner("SONG", name, "  -  ".join(f for f in facts if f),
           cover=album_cover(track_id, name, artist or ""), sub_is_html=True)
    player(track_id)

    if features[0] is not None:
        sound_profile(features, catalog_profile())

    days = run_query(SONG_CHART_SQL, {"name": name, "track_id": track_id})
    if days:
        chart_history(days)

    st.markdown('<div class="section-title">Sounds like</div>', unsafe_allow_html=True)
    similar_list(run_query(SIMILAR_SQL, (track_id, 10, False)))


# A chart song that isn't in the catalog: its rank on every chart day,
# found by its Last.fm name and artist.
CHART_SONG_SQL = """
SELECT chart_date, min(rank) AS rank
FROM chart_entries
WHERE lower(lastfm_track_name) = lower(%(name)s)
  AND lower(lastfm_artist_name) = lower(%(artist)s)
GROUP BY chart_date
ORDER BY chart_date
"""


def chart_song_page(name: str, artist: str) -> None:
    # A smaller song page for chart songs that aren't in the catalog yet.
    # Without audio features there's no sound profile or similar songs,
    # but the chart history still works.
    back_button()
    banner("SONG", name,
           sub_link(artist, "?page=Artists&artist=" + quote(artist))
           + "  -  not in the catalog yet  -  "
           + sub_link("find it on Spotify", spotify_link(None, name, artist), new_tab=True),
           cover=album_cover(None, name, artist), sub_is_html=True)
    days = run_query(CHART_SONG_SQL, {"name": name, "artist": artist})
    if days:
        chart_history(days)
    st.caption("This song isn't in the catalog yet, so there are no sound scores for it. "
               "The daily job adds new chart songs once it can find a preview to analyze.")


st.set_page_config(page_title="SoundMatch", page_icon=":headphones:", layout="wide")
st.html(ROOT / "style.css")

with st.sidebar:
    st.markdown('<div class="brand">Sound<span>Match</span></div>'
                '<div class="tagline">Find songs that sound alike</div>',
                unsafe_allow_html=True)
    # format_func only changes what's shown: page is still "Search" or
    # "US top tracks". :material/...: puts a Google Material icon in the label.
    icons = {"Search": ":material/search:", "US top tracks": ":material/trending_up:",
             "Artists": ":material/person:", "Genres": ":material/category:"}
    pages = list(icons)
    # An artist link opens the app with ?page=Artists&artist=... in the
    # address, so start on that page when it's there. A song link opens it
    # with ?song=..., and then no menu item is picked. Setting the menu's
    # value in session_state (under its key) only happens on the first run.
    if "page" not in st.session_state:
        start = st.query_params.get("page")
        if st.query_params.get("song") or st.query_params.get("chart_song"):
            st.session_state["page"] = None
        else:
            st.session_state["page"] = start if start in pages else pages[0]
    # Clicking a menu item (on_change runs before the page is drawn) puts
    # just that page in the address, so you leave a song or artist page,
    # and the Back button can bring you back to this page later.
    def open_menu_page() -> None:
        st.query_params.clear()
        st.query_params["page"] = st.session_state["page"]

    page = st.radio("Go to", pages, key="page", index=None, label_visibility="collapsed",
                    format_func=lambda p: f"{icons[p]}  {p}",
                    on_change=open_menu_page)

# ---------------------------------------------------------------------
# A song's own page (opened from a song title link)
# ---------------------------------------------------------------------
if st.query_params.get("song"):
    song_page(st.query_params["song"])
elif st.query_params.get("chart_song"):
    chart_song_page(st.query_params["chart_song"], st.query_params.get("artist") or "")

# ---------------------------------------------------------------------
# Page 1: type a song, get similar songs
# ---------------------------------------------------------------------
elif page == "Search" or page is None:
    banner("SEARCH", "Find similar songs",
           "Pick a song you love and discover tracks with the same vibe.")

    left, right = st.columns([3, 1])
    with left:
        # What the box sent last time (None before you type anything).
        # Streamlit keeps it in session_state under the box's key.
        last = st.session_state.get("song_search") or {}
        typed = last.get("query") or ""
        # Give the box the songs that match what you typed. It shows them
        # as a list under itself, with album covers.
        picked = song_search_box(
            suggestions=suggest_songs(typed) if typed and not last.get("pick") else [],
            for_query=typed,
            start_text=typed,
            key="song_search",
            default=None,
        )
    how_many = right.slider("How many", min_value=5, max_value=50, value=10)
    same_language = st.toggle("Same language only")

    song = (picked or {}).get("pick")
    if song:
        show_similar(song["track_id"], song["name"], song["artist"], song["language"],
                     how_many, same_language)
    elif typed:
        st.caption("Pick a song from the list to see songs that sound like it.")
    else:
        catalog_tracker()

# ---------------------------------------------------------------------
# Page 2: the daily Last.fm chart
# ---------------------------------------------------------------------
elif page == "US top tracks":
    dates = [row[0] for row in run_query(CHART_DATES_SQL)]
    if not dates:
        st.info("No chart saved yet. Run pipelines/fetch_chart.py first.")
    else:
        # The calendar sends back a day as text like "2026-10-22". Until you
        # click one (or if it's not a saved day), the newest chart is shown.
        saved = [d.isoformat() for d in dates]
        with st.sidebar:
            clicked = chart_calendar(dates=saved, selected=st.session_state.get("chart_day"),
                                     key="chart_day", default=None)
        day = date.fromisoformat(clicked) if clicked in saved else dates[0]
        prev_day = run_query(PREV_DATE_SQL, (day,))[0][0]
        chart = run_query(CHART_SQL, {"day": day, "prev_day": prev_day})
        matched = [row for row in chart if row[4] is not None]
        compared = f"  -  arrows compare with {prev_day:%B %d}" if prev_day else ""
        banner("CHART", "US Top Tracks",
               f"{day:%B %d, %Y}  -  {len(chart)} songs, {len(matched)} in the catalog{compared}")

        # Only songs that are in the catalog have audio features to compare.
        if matched:
            same_language = st.toggle("Same language only")
            labels = {f"#{r[0]}  {r[1]} by {r[2]}": r for r in matched}
            picked = st.selectbox(
                "Find songs that sound like a chart hit (green dot = in the catalog)",
                list(labels),
                index=None,
                placeholder="Pick a chart song",
            )
            if picked:
                r = labels[picked]
                show_similar(r[4], r[1], r[2], r[5], 10, same_language)

        # With no earlier chart to compare to, there are no arrows to show.
        headers = ["#", "", "Title", "Language", "Listeners"] if prev_day else \
                  ["#", "Title", "Language", "Listeners"]
        tracklist(
            headers,
            [[str(r[0]),
              r[1],
              artist_link(r[2], r[7]),
              spotify_link(r[4], r[1], r[2]),
              f'<td class="lang">{html.escape(r[5] or "")}</td>'
              f'<td class="right">{"<span class=dot>&#9679;</span> " if r[4] else ""}'
              f'{(r[3] or 0):,}</td>',
              song_page_link(r[4], r[1], r[2])]
             for r in chart],
            moves=[movement(r[0], r[6]) for r in chart] if prev_day else None,
        )

# ---------------------------------------------------------------------
# Page 3: one artist's songs, chart history and sound profile
# ---------------------------------------------------------------------
elif page == "Artists":
    names = artist_names()
    if st.query_params.get("artist"):
        back_button()
    # Pre-pick the artist from the address when you came from an artist
    # link. Matching ignores upper/lower case.
    if "artist_pick" not in st.session_state:
        wanted = (st.query_params.get("artist") or "").lower()
        st.session_state["artist_pick"] = next(
            (n for n in names if n.lower() == wanted), None)
    artist = st.selectbox("Artist", names, index=None, key="artist_pick",
                          placeholder="Type an artist's name")
    if not artist:
        banner("ARTISTS", "Artist pages",
               "Pick an artist to see their songs, their chart history and how they sound.")
    else:
        # Keep the address in step with the picked artist. Only when it
        # changed, since each change adds a step to the browser's history.
        if st.query_params.get("artist") != artist:
            st.query_params["page"] = "Artists"
            st.query_params["artist"] = artist
        songs = run_query(ARTIST_SONGS_SQL, {"artist": artist})
        profile = run_query(ARTIST_PROFILE_SQL, {"artist": artist})[0]
        charted = [r for r in songs if r[4]]
        sub = f"{len(songs)} song{'' if len(songs) == 1 else 's'} in the catalog"
        if charted:
            days = sum(r[4] for r in charted)
            best = min(r[5] for r in charted)
            sub += f"  -  {len(charted)} on the US chart, best rank #{best}, {days} chart days in total"
        banner("ARTIST", artist, sub)

        if profile[0] is not None:
            sound_profile(profile, catalog_profile())

        # Match % works like the genres' one: distance 0.05 shows as 95%.
        close = similar_artists(artist)
        if close:
            st.markdown('<div class="section-title">Similar artists</div>'
                        '<div class="chips">'
                        + "".join(artist_chip(name, f"{max(0, round(100 * (1 - float(d))))}%")
                                  for name, d in close)
                        + "</div>", unsafe_allow_html=True)

        tracklist(
            ["#", "Title", "On the chart", "Popularity"],
            [[str(i), r[1], "featured" if r[3] > 1 else "",
              spotify_link(r[0], r[1], artist),
              f'<td class="lang">'
              f'{f"{r[4]} days, best #{r[5]}, last {r[6]:%b %d}" if r[4] else ""}</td>'
              f'<td class="right">{r[2] if r[2] is not None else ""}</td>',
              song_page_link(r[0])]
             for i, r in enumerate(songs, start=1)],
        )

# ---------------------------------------------------------------------
# Page 4: genres. Every genre as a chip; pick one to see its sound,
# the genres closest to it and its most popular songs.
# ---------------------------------------------------------------------
else:
    genres = genre_list()
    names = [g[0] for g in genres]
    if st.query_params.get("genre"):
        back_button()
    # Pre-pick the genre from the address when you came from a genre chip.
    if "genre_pick" not in st.session_state:
        wanted = st.query_params.get("genre")
        st.session_state["genre_pick"] = wanted if wanted in names else None
    genre = st.selectbox("Genre", names, index=None, key="genre_pick",
                         placeholder="Type a genre")
    if not genre:
        banner("GENRES", "Explore genres",
               f"{len(genres)} genres. Pick one to see how it sounds, its top songs "
               "and the genres that sound most like it.")
        st.markdown('<div class="chips">'
                    + "".join(genre_link(name, f"{songs:,}") for name, songs in genres)
                    + "</div>", unsafe_allow_html=True)
    else:
        if st.query_params.get("genre") != genre:
            st.query_params["page"] = "Genres"
            st.query_params["genre"] = genre
        songs_in_genre = dict(genres)[genre]
        charted = run_query(GENRE_CHART_SQL, {"genre": genre})[0][0]
        banner("GENRE", genre,
               f"{songs_in_genre:,} songs in the catalog  -  {charted} on the US chart")

        profile = run_query(GENRE_PROFILE_SQL, {"genre": genre})[0]
        if profile[0] is not None:
            sound_profile(profile, catalog_profile())

        # Match % works like the songs' one: distance 0.05 shows as 95%.
        close = run_query(SIMILAR_GENRES_SQL, {"genre": genre})
        if close:
            st.markdown('<div class="section-title">Sounds closest to</div>'
                        '<div class="chips">'
                        + "".join(genre_link(name, f"{max(0, round(100 * (1 - float(d))))}%")
                                  for name, d in close)
                        + "</div>", unsafe_allow_html=True)

        st.markdown('<div class="section-title">Top songs</div>', unsafe_allow_html=True)
        top = run_query(GENRE_SONGS_SQL, {"genre": genre})
        tracklist(
            ["#", "Title", "Language", "Popularity"],
            [[str(i), r[1], artist_link(r[2]), spotify_link(r[0], r[1], r[2]),
              f'<td class="lang">{html.escape(r[3] or "")}</td>'
              f'<td class="right">{r[4] if r[4] is not None else ""}</td>',
              song_page_link(r[0])]
             for i, r in enumerate(top, start=1)],
        )
