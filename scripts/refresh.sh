#!/usr/bin/env bash
# Rebuild data.json from an already-saved raw fetch folder, then commit + push (Pages redeploys).
# Usage: scripts/refresh.sh [DAY] [RAW_DIR]
#   DAY     defaults to yesterday in the config timezone (America/Anguilla)
#   RAW_DIR defaults to the newest folder under raw/
set -euo pipefail
cd "$(dirname "$0")/.."
DAY="${1:-$(TZ=America/Anguilla date -d yesterday +%F)}"
RAW="${2:-raw/$(ls raw | sort | tail -1)}"
python3 scripts/build_board.py --day "$DAY" --raw "$RAW"
git add -A
git diff --cached --quiet && { echo "nothing changed"; exit 0; }
git commit -m "board: $DAY"
git push origin main
