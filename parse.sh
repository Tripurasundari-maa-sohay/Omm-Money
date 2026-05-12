#!/usr/bin/env bash
# parse.sh — local-only Doha Bank PDF → holdings_cost.json
#
# Usage (from repo root):
#     ./parse.sh ~/Downloads/Portfolio_21550276_*.pdf
#
# The PDF NEVER leaves your machine.  Only the resulting JSON
# (data/holdings_cost.json — free of names, account numbers, addresses)
# gets committed and pushed.
#
# First run does a one-time setup: creates .venv/ and installs Python deps.
# Subsequent runs are fast (< 5 seconds).

set -e

# ── locate repo root (this script's dir) ─────────────────────
cd "$(dirname "$0")"

# ── arg check ────────────────────────────────────────────────
if [ -z "${1:-}" ]; then
  echo "Usage: ./parse.sh <path-to-statement.pdf>"
  echo
  echo "Example:"
  echo "    ./parse.sh ~/Downloads/Portfolio_21550276_96900_1341710_2025-12-01_2026-05-08.pdf"
  exit 1
fi
PDF="$1"
if [ ! -f "$PDF" ]; then
  echo "✗ PDF not found: $PDF"
  exit 1
fi

# ── venv + deps (idempotent) ────────────────────────────────
if [ ! -d .venv ]; then
  echo "→ first-time setup: creating .venv …"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# ── run parser ──────────────────────────────────────────────
python scripts/parse_broker_pdf.py "$PDF" --output data/holdings_cost.json

echo
echo "✓ Updated data/holdings_cost.json"
echo
echo "Next steps:"
echo "    git add data/holdings_cost.json"
echo "    git commit -m \"broker: $(date +%Y-%m-%d) statement\""
echo "    git push"
echo
echo "(The PDF stays on your machine — .gitignore excludes it.)"
