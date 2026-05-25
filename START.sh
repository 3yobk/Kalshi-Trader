#!/usr/bin/env bash
# ============================================================
# START.sh  –  One-click launcher for Kalshi Weather Bot Dashboard
#
# What this does:
#   1. Creates a Python virtual environment (if missing)
#   2. Installs Python deps (requirements.txt + fastapi + uvicorn)
#   3. Installs Node deps for the React dashboard (if missing)
#   4. Starts the FastAPI backend on port 8000
#   5. Starts the Vite dev server on port 5173
#   6. Opens the dashboard in your browser
#   7. Waits — press Ctrl+C to stop everything cleanly
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
UI_DIR="$SCRIPT_DIR/forecast-guard-watch"
BACKEND_PORT=8000
FRONTEND_PORT=5173

# ── Colors ──────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[START]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERR]${NC}   $*"; }

# ── Cleanup on exit ──────────────────────────────────────────
PIDS=()
cleanup() {
    echo ""
    info "Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    info "Done. Goodbye."
}
trap cleanup EXIT INT TERM

# ── 1. Python virtual environment ───────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Creating Python virtual environment at .venv ..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# ── 2. Python dependencies ───────────────────────────────────
info "Installing / verifying Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet "fastapi>=0.111" "uvicorn[standard]>=0.29"

# ── 3. Node dependencies ─────────────────────────────────────
if [ ! -d "$UI_DIR/node_modules" ]; then
    info "Installing Node dependencies (this may take a minute)..."
    cd "$UI_DIR"
    npm install --silent
    cd "$SCRIPT_DIR"
else
    info "Node dependencies already installed."
fi

# ── 4. Write the frontend env file so it points at the backend
UI_ENV="$UI_DIR/.env.local"
if ! grep -q "VITE_BOT_API_URL" "$UI_ENV" 2>/dev/null; then
    echo "VITE_BOT_API_URL=http://127.0.0.1:${BACKEND_PORT}" > "$UI_ENV"
    info "Created $UI_ENV pointing to backend port $BACKEND_PORT"
fi

# ── 5. Start FastAPI backend ─────────────────────────────────
info "Starting FastAPI backend on port $BACKEND_PORT ..."
cd "$SCRIPT_DIR"
uvicorn api_server:app --host 127.0.0.1 --port "$BACKEND_PORT" --log-level warning &
BACKEND_PID=$!
PIDS+=($BACKEND_PID)

# Wait for backend to be ready (up to 15 s)
info "Waiting for backend to be ready..."
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1; then
        info "Backend is up."
        break
    fi
    sleep 0.5
done

# ── 6. Start Vite frontend ───────────────────────────────────
info "Starting Vite dashboard on port $FRONTEND_PORT ..."
cd "$UI_DIR"
npm run dev -- --port "$FRONTEND_PORT" --host 127.0.0.1 &
FRONTEND_PID=$!
PIDS+=($FRONTEND_PID)

# Wait a moment for Vite to compile
sleep 3

# ── 7. Open browser ─────────────────────────────────────────
DASHBOARD_URL="http://127.0.0.1:${FRONTEND_PORT}"
info "Dashboard: $DASHBOARD_URL"
info "Backend API: http://127.0.0.1:${BACKEND_PORT}/api/health"
echo ""
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Kalshi Weather Bot Dashboard is running!       ${NC}"
echo -e "${GREEN}  Open: ${DASHBOARD_URL}                         ${NC}"
echo -e "${GREEN}  Press Ctrl+C to stop everything.               ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════${NC}"

# Try to open browser (works on macOS / Linux with xdg-open)
if command -v open >/dev/null 2>&1; then
    open "$DASHBOARD_URL" 2>/dev/null || true
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$DASHBOARD_URL" 2>/dev/null || true
fi

# ── Keep running until Ctrl+C ────────────────────────────────
wait
