"""SoundMatch: the music project web app.

Run it with:
    streamlit run app.py

Then it opens in your browser at http://localhost:8501.
Streamlit re-runs this whole file from top to bottom every time you
type or click something, so the code reads like a simple script.

The dark theme colors live in .streamlit/config.toml. The rest of the look
(font, the gradient header, the track lists) is in style.css.

Playing songs: the song you search gets a Spotify player under the header,
and every song title in a list opens that song on Spotify in a new tab.
The catalog came from Spotify, so its track_id is the real Spotify id.
Chart songs added by pipelines/analyze_chart_songs.py have ids like
"deezer:12345" instead, so they get Deezer's player and a Spotify search link.

Pages: Search, US top tracks (with up/down arrows against the previous
saved chart) and Artists (an artist's songs, chart history and average
sound profile). Click an artist's name in any list to open their page.
"""

import html
import os
from urllib.parse import quote
from pathlib import Path

import psycopg
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


def spotify_link(track_id: str | None, name: str, artist: str) -> str:
    # A catalog song links straight to its Spotify page. A chart song that
    # isn't in the catalog, or was added from Deezer, has no Spotify id, so
    # it links to a Spotify search.
    if track_id and not track_id.startswith("deezer:"):
        return f"https://open.spotify.com/track/{track_id}"
    return "https://open.spotify.com/search/" + quote(f"{name} {artist}")


def banner(kicker: str, title: str, sub: str) -> None:
    # html.escape turns characters like < and & into safe text, so a song
    # name can never break the page.
    st.markdown(
        f'<div class="banner"><div class="kicker">{html.escape(kicker)}</div>'
        f'<div class="title">{html.escape(title)}</div>'
        f'<div class="sub">{html.escape(sub)}</div></div>',
        unsafe_allow_html=True,
    )


def artist_link(name: str, link_name: str | None = None) -> str:
    # The artist's name as a link to their artist page. target="_self" opens
    # it in the same tab. link_name is the catalog's spelling, when it differs.
    url = "?page=Artists&artist=" + quote(link_name or name)
    return f'<a class="artist-link" href="{url}" target="_self">{html.escape(name)}</a>'


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
    # Each row is [number, song, artist html, Spotify link, right-hand cells html].
    # moves, when given, is an extra cell per row after the number (the chart arrows).
    # The number turns into a play button when you point at the row
    # (style.css does that), and the button and the title both open Spotify.
    # target="_blank" opens the link in a new tab, so the app stays open.
    # The artist html is usually artist_link(...), so it opens the artist page.
    head = "".join(f"<th>{h}</th>" for h in headers)
    moves = moves or [None] * len(rows)
    body = "".join(
        f'<tr><td class="num"><span class="idx">{num}</span>'
        f'<a class="play" href="{html.escape(url)}" target="_blank">&#9654;</a></td>'
        f'{"" if move is None else f"<td class=move-cell>{move}</td>"}'
        f'<td><a class="song" href="{html.escape(url)}" target="_blank">{html.escape(song)}</a>'
        f'<div class="artist">{artist}</div></td>'
        f"{right}</tr>"
        for (num, song, artist, url, right), move in zip(rows, moves)
    )
    st.markdown(f'<table class="tracklist"><tr>{head}</tr>{body}</table>',
                unsafe_allow_html=True)


def sound_profile(artist_avg: tuple, catalog_avg: tuple) -> None:
    # One bar per feature: green is the artist's average, the white tick is
    # the average song in the catalog, so you can see what makes them stand out.
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
        f'White tick = the average song in the catalog.</div></div>',
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=3600)
def artist_names() -> list[str]:
    # Remembered for an hour so the long list isn't fetched on every click.
    return [r[0] for r in run_query(ARTIST_NAMES_SQL)]


@st.cache_data(ttl=3600)
def catalog_profile() -> tuple:
    return run_query(CATALOG_PROFILE_SQL)[0]


def show_similar(track_id: str, name: str, artist: str, language: str,
                 how_many: int, same_language: bool) -> None:
    rows = run_query(SIMILAR_SQL, (track_id, how_many, same_language))
    if track_id.startswith("deezer:"):
        banner("SOUNDS LIKE", name,
               f"{artist}  -  {language}  -  {len(rows)} songs  -  mood scores estimated")
        # Deezer's player, with the same 30 second preview the scores came from.
        deezer_id = track_id.removeprefix("deezer:")
        components.iframe(f"https://widget.deezer.com/widget/dark/track/{deezer_id}", height=152)
    else:
        banner("SOUNDS LIKE", name, f"{artist}  -  {language}  -  {len(rows)} songs")
        # Spotify's own player. It plays a 30 second preview, or the whole song
        # if you're logged in to Spotify in this browser.
        components.iframe(f"https://open.spotify.com/embed/track/{track_id}?theme=0", height=152)
    # A distance of 0 is identical. Turn it into a friendlier "% match"
    # (distance 0.05 shows as 95%).
    tracklist(
        ["#", "Title", "Language", "Match"],
        [[str(i), r[0], artist_link(r[1]), spotify_link(r[4], r[0], r[1]),
          f'<td class="lang">{html.escape(r[2] or "")}</td>'
          f'<td class="match">{max(0, round(100 * (1 - float(r[3]))))}%</td>']
         for i, r in enumerate(rows, start=1)],
    )


st.set_page_config(page_title="SoundMatch", page_icon=":headphones:", layout="wide")
st.html(ROOT / "style.css")

with st.sidebar:
    st.markdown('<div class="brand">Sound<span>Match</span></div>'
                '<div class="tagline">Find songs that sound alike</div>',
                unsafe_allow_html=True)
    # format_func only changes what's shown: page is still "Search" or
    # "US top tracks". :material/...: puts a Google Material icon in the label.
    icons = {"Search": ":material/search:", "US top tracks": ":material/trending_up:",
             "Artists": ":material/person:"}
    pages = list(icons)
    # An artist link opens the app with ?page=Artists&artist=... in the
    # address, so start on that page when it's there. Setting the menu's
    # value in session_state (under its key) only happens on the first run.
    if "page" not in st.session_state:
        start = st.query_params.get("page")
        st.session_state["page"] = start if start in pages else pages[0]
    page = st.radio("Go to", pages, key="page", label_visibility="collapsed",
                    format_func=lambda p: f"{icons[p]}  {p}")
# Leaving the Artists page clears the address, so a reload doesn't jump back.
if page != "Artists" and st.query_params.get("page"):
    st.query_params.clear()

# ---------------------------------------------------------------------
# Page 1: type a song, get similar songs
# ---------------------------------------------------------------------
if page == "Search":
    banner("SEARCH", "Find similar songs",
           "Type a song and get back songs that sound like it. The catalog stops around 2022.")

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

# ---------------------------------------------------------------------
# Page 2: the daily Last.fm chart
# ---------------------------------------------------------------------
elif page == "US top tracks":
    dates = [row[0] for row in run_query(CHART_DATES_SQL)]
    if not dates:
        st.info("No chart saved yet. Run pipelines/fetch_chart.py first.")
    else:
        day = st.sidebar.selectbox("Chart date", dates)
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
              f'{(r[3] or 0):,}</td>']
             for r in chart],
            moves=[movement(r[0], r[6]) for r in chart] if prev_day else None,
        )

# ---------------------------------------------------------------------
# Page 3: one artist's songs, chart history and sound profile
# ---------------------------------------------------------------------
else:
    names = artist_names()
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
        # Keep the address in step with the picked artist.
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

        tracklist(
            ["#", "Title", "On the chart", "Popularity"],
            [[str(i), r[1], "featured" if r[3] > 1 else "",
              spotify_link(r[0], r[1], artist),
              f'<td class="lang">'
              f'{f"{r[4]} days, best #{r[5]}, last {r[6]:%b %d}" if r[4] else ""}</td>'
              f'<td class="right">{r[2] if r[2] is not None else ""}</td>']
             for i, r in enumerate(songs, start=1)],
        )
