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

# Same lookup as pipelines/similar_songs.py: the most popular song whose
# name (and artist, if given) contains what you typed.
FIND_SONG_SQL = """
SELECT t.track_id, t.name, a.name AS artist, t.language
FROM tracks t
JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 1
JOIN artists a ON a.artist_id = ta.artist_id
WHERE t.name ILIKE %s
  AND a.name ILIKE %s
ORDER BY t.popularity DESC
LIMIT 1
"""

SIMILAR_SQL = "SELECT name, artist, language, distance, track_id FROM similar_songs(%s, %s, %s)"

# Every day the daily job has saved, newest first.
CHART_DATES_SQL = "SELECT DISTINCT chart_date FROM chart_entries ORDER BY chart_date DESC"

# One day's chart. track_id is NULL when the song isn't in the catalog,
# and then the LEFT JOIN gives a NULL language too.
CHART_SQL = """
SELECT c.rank, c.lastfm_track_name, c.lastfm_artist_name, c.listeners, c.track_id, t.language
FROM chart_entries c
LEFT JOIN tracks t ON t.track_id = c.track_id
WHERE c.chart_date = %s
ORDER BY c.rank
"""


def run_query(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def spotify_link(track_id: str | None, name: str, artist: str) -> str:
    # A catalog song links straight to its Spotify page. A chart song that
    # isn't in the catalog has no Spotify id, so it links to a Spotify search.
    if track_id:
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


def tracklist(headers: list[str], rows: list[list[str]]) -> None:
    # Each row is [number, song, artist, Spotify link, right-hand cell html].
    # The number turns into a play button when you point at the row
    # (style.css does that), and the button and the title both open Spotify.
    # target="_blank" opens the link in a new tab, so the app stays open.
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        f'<tr><td class="num"><span class="idx">{num}</span>'
        f'<a class="play" href="{html.escape(url)}" target="_blank">&#9654;</a></td>'
        f'<td><a class="song" href="{html.escape(url)}" target="_blank">{html.escape(song)}</a>'
        f'<div class="artist">{html.escape(artist)}</div></td>'
        f"{right}</tr>"
        for num, song, artist, url, right in rows
    )
    st.markdown(f'<table class="tracklist"><tr>{head}</tr>{body}</table>',
                unsafe_allow_html=True)


def show_similar(track_id: str, name: str, artist: str, language: str,
                 how_many: int, same_language: bool) -> None:
    rows = run_query(SIMILAR_SQL, (track_id, how_many, same_language))
    banner("SOUNDS LIKE", name, f"{artist}  -  {language}  -  {len(rows)} songs")
    # Spotify's own player. It plays a 30 second preview, or the whole song
    # if you're logged in to Spotify in this browser.
    components.iframe(f"https://open.spotify.com/embed/track/{track_id}?theme=0", height=152)
    # A distance of 0 is identical. Turn it into a friendlier "% match"
    # (distance 0.05 shows as 95%).
    tracklist(
        ["#", "Title", "Language", "Match"],
        [[str(i), r[0], r[1], spotify_link(r[4], r[0], r[1]),
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
    icons = {"Search": ":material/search:", "US top tracks": ":material/trending_up:"}
    page = st.radio("Go to", ["Search", "US top tracks"], label_visibility="collapsed",
                    format_func=lambda p: f"{icons[p]}  {p}")

# ---------------------------------------------------------------------
# Page 1: type a song, get similar songs
# ---------------------------------------------------------------------
if page == "Search":
    banner("SEARCH", "Find similar songs",
           "Type a song and get back songs that sound like it. The catalog stops around 2022.")

    left, middle, right = st.columns([3, 3, 2])
    song = left.text_input("Song", placeholder="What do you want to listen to?")
    artist = middle.text_input("Artist (optional)", placeholder="Any artist")
    how_many = right.slider("How many", min_value=5, max_value=50, value=10)
    same_language = st.toggle("Same language only")

    if song:
        found = run_query(FIND_SONG_SQL, (f"%{song}%", f"%{artist}%"))
        if not found:
            st.warning(f'No song matching "{song}" in the catalog. Try part of the name.')
        else:
            track_id, name, found_artist, language = found[0]
            show_similar(track_id, name, found_artist, language, how_many, same_language)

# ---------------------------------------------------------------------
# Page 2: the daily Last.fm chart
# ---------------------------------------------------------------------
else:
    dates = [row[0] for row in run_query(CHART_DATES_SQL)]
    if not dates:
        st.info("No chart saved yet. Run pipelines/fetch_chart.py first.")
    else:
        day = st.sidebar.selectbox("Chart date", dates)
        chart = run_query(CHART_SQL, (day,))
        matched = [row for row in chart if row[4] is not None]
        banner("CHART", "US Top Tracks",
               f"{day:%B %d, %Y}  -  {len(chart)} songs, {len(matched)} in the catalog")

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
                rank, name, chart_artist, listeners, track_id, language = labels[picked]
                show_similar(track_id, name, chart_artist, language, 10, same_language)

        tracklist(
            ["#", "Title", "Language", "Listeners"],
            [[str(r[0]),
              r[1],
              r[2],
              spotify_link(r[4], r[1], r[2]),
              f'<td class="lang">{html.escape(r[5] or "")}</td>'
              f'<td class="right">{"<span class=dot>&#9679;</span> " if r[4] else ""}'
              f'{(r[3] or 0):,}</td>']
             for r in chart],
        )
