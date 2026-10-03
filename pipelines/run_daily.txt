@echo off
REM Runs the daily chart job. Windows Task Scheduler calls this file once a day.
REM Output goes to logs\daily.log so you can check later that it ran.
cd /d "%~dp0.."
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\daily.log
REM Make sure the database container is up, then give Postgres a few seconds to be ready.
docker compose up -d >> logs\daily.log 2>&1
ping -n 16 127.0.0.1 > nul
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" pipelines\fetch_chart.py >> logs\daily.log 2>&1
