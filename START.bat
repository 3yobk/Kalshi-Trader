@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "UI_DIR=%SCRIPT_DIR%forecast-guard-watch"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5173"

echo.
echo  Kalshi Weather Bot - Dashboard Launcher
echo  ========================================
echo.

REM ── 1. Create venv if missing ──────────────────────────────
echo [1/6] Setting up Python environment...
python -m venv "%VENV_DIR%" 2>nul
echo       OK.

REM ── 2. Install Python deps ─────────────────────────────────
echo [2/6] Installing Python dependencies...
"%VENV_DIR%\Scripts\python.exe" -m pip install --quiet -r "%SCRIPT_DIR%requirements.txt" 2>nul
"%VENV_DIR%\Scripts\python.exe" -m pip install --quiet "fastapi>=0.111" "uvicorn[standard]>=0.29" 2>nul
echo       Done.

REM ── 3. Install Node deps ────────────────────────────────────
echo [3/6] Installing Node dependencies...
pushd "%UI_DIR%"
call npm install --prefer-offline --silent
popd
echo       Done.

REM ── 4. Write .env.local for the frontend ───────────────────
echo [4/6] Checking frontend config...
pushd "%UI_DIR%"
if not exist ".env.local" (
    echo VITE_BOT_API_URL=http://127.0.0.1:%BACKEND_PORT%> ".env.local"
    echo       Created .env.local
) else (
    echo       .env.local already exists.
)
popd

REM ── 5. Launch backend + dashboard ──────────────────────────
echo [5/6] Starting API backend and dashboard...

start "Backend  (port %BACKEND_PORT%)" cmd /k ""%VENV_DIR%\Scripts\uvicorn.exe" api_server:app --host 127.0.0.1 --port %BACKEND_PORT%"

timeout /t 4 /nobreak >nul

pushd "%UI_DIR%"
start "Dashboard (port %FRONTEND_PORT%)" cmd /k "npm run dev -- --port %FRONTEND_PORT% --host 127.0.0.1"
popd

timeout /t 5 /nobreak >nul

REM ── 6. Launch the bot in polling mode ──────────────────────
echo [6/6] Starting Kalshi bot (live polling every 60s)...
start "Kalshi Bot (live)" cmd /k ""%VENV_DIR%\Scripts\python.exe" "%SCRIPT_DIR%main.py""

echo.
echo  ============================================================
echo   Backend  : http://127.0.0.1:%BACKEND_PORT%/api/health
echo   Dashboard: http://127.0.0.1:%FRONTEND_PORT%
echo   Bot      : polling every 60s (see "Kalshi Bot" window)
echo  ============================================================
echo.
echo  Three console windows are running.
echo  Close them all to stop everything.
echo.

start "" "http://127.0.0.1:%FRONTEND_PORT%"

pause