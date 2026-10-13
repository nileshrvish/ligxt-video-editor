#!/usr/bin/env bash
# Ligxt Video Editor launcher for Git Bash.
# Usage:  ./start.sh            (normal)
#         ./start.sh -p 9000    (different port)
#         ./start.sh -n         (don't open the browser)
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT=8765
OPEN=1
while getopts "p:n" opt; do
  case $opt in
    p) PORT="$OPTARG" ;;
    n) OPEN=0 ;;
    *) echo "Usage: ./start.sh [-p port] [-n]"; exit 2 ;;
  esac
done
URL="http://127.0.0.1:$PORT"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "The Python environment is missing. Run setup.ps1 first (in PowerShell)." >&2
  exit 1
fi
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  echo "The UI has not been built. Run setup.ps1, or: cd frontend && npm run build" >&2
  exit 1
fi
open_browser() {
  if command -v start >/dev/null 2>&1; then start "" "$URL"
  elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
}
if curl -s -m 2 "$URL/api/health" >/dev/null 2>&1; then
  echo "Ligxt is already running at $URL"
  [ "$OPEN" = 1 ] && open_browser
  exit 0
fi
export LIGXT_PORT="$PORT"
export LIGXT_OPEN_BROWSER="$OPEN"
echo "Starting Ligxt Video Editor at $URL  (press Ctrl+C to stop)"
exec "$PY" -m uvicorn app.main:app --app-dir "$ROOT/backend" --host 127.0.0.1 --port "$PORT" --log-level warning
