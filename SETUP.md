# Setting up the music project, step by step

This takes about 30 minutes the first time, mostly waiting for downloads. Every command below is typed into a **terminal**:

- **Mac:** open the **Terminal** app (press Cmd+Space, type "Terminal").
- **Windows:** open **PowerShell** (Start menu, type "PowerShell").

Where Mac and Windows differ, both versions are shown. Lines starting with `#` are comments; you don't need to type them.

---

## Step 1. Install Docker Desktop

Docker runs Postgres for you in a sealed box, so you don't have to install or configure a database server yourself.

1. Download Docker Desktop from https://www.docker.com/products/docker-desktop/ and install it like any app. On Windows, accept the prompt to enable WSL 2 if it asks, then restart.
2. Open Docker Desktop and wait until the bottom-left corner says **Engine running**. You can skip the sign-in.
3. Check it in your terminal:

   ```bash
   docker --version
   ```

   You should see something like `Docker version 27.x`.

Docker Desktop must be open whenever you work on the project.

## Step 2. Install Python

1. Download Python 3.12 or newer from https://www.python.org/downloads/ and run the installer.
   - **Windows:** on the first screen, tick **"Add python.exe to PATH"** before clicking Install.
2. Close and reopen your terminal, then check it:

   ```bash
   # Mac
   python3 --version
   # Windows
   py --version
   ```

   You should see `Python 3.12.x` or higher.

A code editor makes the SQL files much nicer to read. [VS Code](https://code.visualstudio.com/) is free and a good default.

## Step 3. Get the project files

1. Download `music-project.zip` from the Claude thread and unzip it into your **Documents** folder, so you end up with `Documents/music-project/README.md`.
2. In your terminal, move into that folder:

   ```bash
   # Mac
   cd ~/Documents/music-project
   # Windows
   cd $HOME\Documents\music-project
   ```

   Type `dir` (Windows) or `ls` (Mac) and check that you see `docker-compose.yml` and `README.md`. If you only see another `music-project` folder, the unzip added an extra level (Windows' "Extract All" does this), so go one level deeper with `cd music-project`.

   Every command from here on is run from inside this folder. If you open a new terminal later, run this `cd` again first.

## Step 4. Start Postgres

```bash
docker compose up -d
```

The first time, this downloads the Postgres image, which takes a minute or two. `-d` means "run in the background". Check it's running:

```bash
docker compose ps
```

You should see a `db` service with status **running** (or **Up**).

## Step 5. Set up Python for the project

A **virtual environment** is a private folder of Python packages for this one project, so it can't clash with anything else on your computer.

```bash
# Mac
python3 -m venv .venv
source .venv/bin/activate

# Windows
py -m venv .venv
.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`. That means it's active. Now install the packages:

```bash
pip install -r requirements.txt
```

On Windows, if activating gives a red error about "running scripts is disabled", run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Step 6. Test the loader on the sample data

```bash
python pipelines/load_catalog.py data/sample_tracks.csv
```

You should see:

```
Staged 14 CSV rows (1 rejected for missing id/name/artist) in 0.1s
Rows now in each table:
  tracks                 11
  artists                 6
  track_artists          14
  genres                  8
  track_genres           13
  audio_features         11
```

If you see that, your database, Python and the loader all work. Run the same command again: the numbers stay the same, because the loader never duplicates rows.

## Step 7. Load the real Kaggle dataset

1. Make a free account at https://www.kaggle.com.
2. Open the Spotify Tracks Dataset: https://www.kaggle.com/datasets/maharshipandya/-spotify-tracks-dataset. If that link doesn't work, search Kaggle for "spotify tracks dataset maharshipandya".
3. Click **Download**, unzip it, and move `dataset.csv` into the project's `data` folder, next to `sample_tracks.csv`.
4. Load it:

   ```bash
   python pipelines/load_catalog.py data/dataset.csv
   ```

   Expect about 114,000 staged rows, 1 rejected, and roughly 89,000 tracks. There are fewer tracks than rows because the file repeats a song once for each genre it's listed under. The sample tracks from step 6 stay in the database too; that's harmless.

## Step 8. Look at your data

Open a SQL prompt inside the database:

```bash
docker compose exec db psql -U music
```

The prompt changes to `music=#`. Try these (each SQL statement ends with `;`):

```sql
\dt
SELECT count(*) FROM tracks;
SELECT name, album_name, popularity FROM tracks ORDER BY popularity DESC LIMIT 10;
```

`\dt` lists your tables. Type `\q` to leave. You're set up. Next, open `exercises/week1.sql` in your editor and start on the questions.

---

## Coming back later

Each time you sit down to work:

1. Open Docker Desktop.
2. In a terminal: `cd` into the project folder, then run `docker compose up -d`.
3. Activate Python: `source .venv/bin/activate` (Mac) or `.venv\Scripts\Activate.ps1` (Windows).

Your data is saved between sessions. `docker compose down` stops the database without deleting anything; `docker compose down -v` deletes all the data so you can start fresh.

## If something goes wrong

| What you see | What to do |
|---|---|
| Docker Desktop says "Virtualization support not detected" (Windows) | Check Task Manager > Performance > CPU. If Virtualization says Enabled, right-click the Start button, open **Terminal (Admin)**, run `wsl --install` (or `wsl --update` if it's already installed), and restart. If it says Disabled, it has to be turned on in your BIOS; ask Claude for help or install Postgres directly instead. |
| `no configuration file provided: not found` | You're one folder too high. Run `cd music-project` and try again (see step 3). |
| `command not found: docker`, "Cannot connect to the Docker daemon", or "failed to connect to the docker API at npipe" | Docker Desktop isn't open or hasn't finished starting. Open it and wait for **Engine running**. |
| `port is already allocated` or "address already in use" on 5432 | You already have a Postgres on your computer. In `docker-compose.yml`, change `"5432:5432"` to `"5433:5432"`, run `docker compose up -d` again, then set the connection before running the loader: Mac `export DATABASE_URL=postgresql://music:music@localhost:5433/music`, Windows `$env:DATABASE_URL="postgresql://music:music@localhost:5433/music"`. |
| `connection refused` when running the loader | The database is still starting. Wait 10 seconds and try again, and check `docker compose ps`. |
| `No module named 'psycopg'` | The virtual environment isn't active (no `(.venv)` in your prompt). Run the activate command from step 5. |
| `command not found: python` on Mac | Use `python3` instead of `python`, or activate the virtual environment, which adds `python`. |
| Anything else | Paste the full error into the Claude thread. |
