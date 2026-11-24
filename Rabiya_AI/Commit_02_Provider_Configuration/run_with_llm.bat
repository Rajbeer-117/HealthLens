@echo off
title HealthLens with LLM
cd /d "%~dp0"
echo ============================================
echo   HealthLens - starting with a local LLM
echo ============================================

where ollama >nul 2>nul
if errorlevel 1 (
  echo.
  echo Ollama is not installed yet.
  echo 1. The download page will open now. Install Ollama.
  echo 2. Then double-click run_with_llm.bat again.
  start https://ollama.com/download
  pause
  exit /b
)

curl -s http://localhost:11434/api/tags >nul 2>nul
if errorlevel 1 (
  echo Starting Ollama...
  start "Ollama" /min ollama serve
  timeout /t 6 >nul
)

ollama list | findstr /i "llama3.2" >nul
if errorlevel 1 (
  echo Downloading the llama3.2 model - about 2 GB, only the first time...
  ollama pull llama3.2
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment...
  py -m venv .venv
)
call .venv\Scripts\activate.bat
echo Installing requirements...
python -m pip install -q -r requirements.txt

echo.
echo Opening http://127.0.0.1:5000 ... (keep this window open; close it to stop the app)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:5000"
python app.py
pause
