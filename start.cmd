@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
  py -3 -m venv .venv 2>nul
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 (
    echo Could not create a virtualenv. Install Python 3.11+ from https://www.python.org/downloads/ and check "Add python.exe to PATH".
    exit /b 1
  )
  call .venv\Scripts\pip install -r requirements.txt
  if errorlevel 1 exit /b 1
  call .venv\Scripts\playwright install chromium
  if errorlevel 1 exit /b 1
)

echo Open http://127.0.0.1:8000 in your browser.
call .venv\Scripts\uvicorn backend.app:app --host 127.0.0.1 --port 8000
