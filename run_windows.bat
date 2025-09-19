@echo off
setlocal
REM Probeer Python te vinden
where python >nul 2>nul
if %errorlevel% neq 0 (
  echo Python is niet gevonden. Download en installeer Python van https://www.python.org/downloads/
  pause
  exit /b
)
REM Virtuele omgeving
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
set FLASK_APP=app.py
python app.py
pause
