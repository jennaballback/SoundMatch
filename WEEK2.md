# Week 2: the daily US chart pipeline

This week you build a job that grabs the Last.fm US top tracks, saves them as a dated snapshot, and links each chart song to a song in your Kaggle catalog. Run it every day and you get a history of the chart, which week 4 turns into "biggest climbers" and "longest at number 1" queries.

```
Last.fm API --fetch_chart.py--> staging.lastfm_chart_raw --003_transform--> chart_entries --004_match--> chart_entries.track_id
              (Python)          (raw JSON, one row a day)   (you write)     (one row per    (you write)   (link to tracks)
                                                                             song per day)
```

It's the same extract, load, transform pattern as week 1, just with JSON instead of a CSV.

**Who writes what:** the Python plumbing is done, except one small TODO. The two SQL files, `sql/003_transform_chart.sql` and `sql/004_match_chart.sql`, are yours. Each one is full of hints; try without them first.

All commands below are for PowerShell, run from your project folder. Type or paste them one line at a time and press Enter after each.

---

## Step 1. Get the new files into your project

1. Download the new `music-project.zip` from the project.
2. In File Explorer, right-click the zip, choose **Extract All...**, and click **Extract**. A new folder opens.
3. Open the `music-project` folder inside it. Select these (hold **Ctrl** and click each one):
   - the `sql` folder
   - the `pipelines` folder
   - `requirements.txt`
   - `.env.example`
   - `README.md`
   - `WEEK2.md`
4. Press **Ctrl+C**.
5. Open your real project folder (the one with app.py in it) and press **Ctrl+V**. When Windows asks, choose **Replace the files in the destination**.
6. Back in the extracted folder, open `data`, copy `sample_lastfm_chart.json`, and paste it into your project's `data` folder.

Don't copy the `exercises` folder, or it will overwrite your week 1 answers.

## Step 2. Get a Last.fm API key (free, about 3 minutes)

An API key is like a password that tells Last.fm which app is asking for data.

1. Go to https://www.last.fm/join and make an account (skip this if you have one).
2. Go to https://www.last.fm/api/account/create.
3. Fill in the form:
   - **Contact email:** your email
   - **Application name:** `music-project` (anything works)
   - **Application description:** `Learning project that saves the daily US chart`
   - **Callback URL** and **Application homepage:** leave both blank
4. Click **Submit**.
5. The next page shows an **API key** and a **Shared secret**. You only need the **API key** (32 letters and numbers). Keep the page open.

Now put the key in a `.env` file, which the script reads on startup. In PowerShell, in your project folder:

```powershell
Test-Path .env
```

If that prints `False`, create the file from the template:

```powershell
Copy-Item .env.example .env
```

Then open it:

```powershell
notepad .env
```

Paste your key after `LASTFM_API_KEY=` so the line looks like `LASTFM_API_KEY=abc123...` with no spaces or quotes. Save with **Ctrl+S** and close Notepad.

`.env` is listed in `.gitignore`, so the key never ends up on GitHub if you publish the project later. Don't paste the key into the chat either.

## Step 3. Start everything up

Make sure Docker Desktop is open and running, then:

```powershell
docker compose up -d
```

```powershell
.venv\Scripts\Activate.ps1
```

Your prompt should start with `(.venv)`. Install the two new packages (`requests` talks to websites, `python-dotenv` reads the `.env` file):

```powershell
pip install -r requirements.txt
```

## Step 4. Test run with the sample chart

`data/sample_lastfm_chart.json` is a small made-up chart in exactly the format Last.fm returns, so you can test without touching the internet.

```powershell
python pipelines/fetch_chart.py --file data/sample_lastfm_chart.json
```

You should see `Load: saved the raw JSON`, then two `skipped` lines, because you haven't written the SQL yet. That's expected. Look at what landed:

```powershell
docker compose exec db psql -U music
```

```sql
SELECT chart_date, fetched_at, jsonb_pretty(payload) FROM staging.lastfm_chart_raw;
```

(Press **q** to get out of the long output, and `\q` to leave psql.)

## Step 5. Write the transform (sql/003_transform_chart.sql)

Open `sql/003_transform_chart.sql` in VS Code or Notepad and follow the steps in its comments. You'll learn:

- reading JSON in SQL with `->` and `->>`
- turning a JSON list into rows with `jsonb_array_elements` and `LATERAL`
- the upsert, `INSERT ... ON CONFLICT DO UPDATE`, which is what makes a daily job safe to re-run

Build it up as a SELECT in psql first. When it works, paste it into the file and re-run the Step 4 command. The summary at the end should now list 10 songs. Run it a second time: still 10 rows, not 20.

## Step 6. Call the real API

Open `pipelines/fetch_chart.py` and find the `TODO (you)` in `fetch_from_lastfm()`. Fill in the `params` dict, one `"key": value,` per line. The comment lists all five keys. The API docs are at https://www.last.fm/api/show/geo.getTopTracks.

Then run it without `--file`:

```powershell
python pipelines/fetch_chart.py
```

If Last.fm answers with an error, the script prints its message (for example `Invalid API key`, or which parameter is missing). You now have today's real US chart in `chart_entries`.

## Step 7. Write the matcher (sql/004_match_chart.sql)

Open `sql/004_match_chart.sql` and follow its comments. This is the most "real world" part of the week: two data sources that name things slightly differently, and no shared id. Start with an exact match, look at what fails, then improve it. Write down the match percentage after each version.

Don't expect 100%. The Kaggle catalog stops in 2022, so newer songs can't match, and that's the reason `track_id` is allowed to be NULL.

## Step 8. Send it in for review

Paste both SQL files and your final match percentage in the thread. You'll get feedback on the SQL and on any matches that look wrong.

## Stretch questions (optional)

- Run `python pipelines/fetch_chart.py --file data/sample_lastfm_chart.json --date 2026-09-20` to fake a second day. Write a query that shows each song's rank on both days side by side.
- Can a wrong match happen? Try to find one (hint: think about live versions, remixes and "Remastered" editions that share a title and artist).
- If you re-fetch the same day and a *different* song now sits at rank 5, your upsert replaces the name but keeps the old `track_id`. How would you fix that in 003?

Next week: the similar-songs query. Scheduling this job to run by itself every day comes in week 4.
