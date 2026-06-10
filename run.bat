@echo off
REM Start MrSimplex (Windows).
REM First run creates a virtual environment and installs dependencies.
cd /d "%~dp0"

if not exist ".venv" (
  echo [setup] creating virtual environment...
  python -m venv .venv
  call .venv\Scripts\python.exe -m pip install --upgrade pip
  echo [setup] installing dependencies...
  call .venv\Scripts\pip.exe install -r requirements.txt
)

call .venv\Scripts\python.exe bot.py %*
