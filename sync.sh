#!/bin/bash
# ─────────────────────────────────────────────────────────────────
#  sync.sh  — drop files into inbox/ and run this once
#
#  Supported inbox files:
#    *.pdf   → US portfolio PDF  (Doha Bank Global statement)
#    *.xlsx  → India consolidated transaction Excel
#
#  After parsing, updates data/holdings_cost.json and pushes.
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
INBOX="$REPO/inbox"
SCRIPTS="$REPO/scripts"

echo "──────────────────────────────────────────"
echo "  Portfolio Sync  ·  $(date '+%Y-%m-%d %H:%M')"
echo "  Repo : $REPO"
echo "  Inbox: $INBOX"
echo "──────────────────────────────────────────"

# ── Check dependencies ─────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found"; exit 1
fi

# ── Process PDFs (US portfolio) ────────────────────────────────
PDF_COUNT=0
for f in "$INBOX"/*.pdf "$INBOX"/*.PDF; do
  [ -f "$f" ] || continue
  echo ""
  echo "📄  US PDF: $(basename "$f")"
  python3 "$SCRIPTS/parse_broker_pdf.py" "$f"
  PDF_COUNT=$((PDF_COUNT + 1))
done

# ── Process Excel (India transactions) ─────────────────────────
XL_COUNT=0
for f in "$INBOX"/*.xlsx "$INBOX"/*.XLSX; do
  [ -f "$f" ] || continue
  echo ""
  echo "📊  India Excel: $(basename "$f")"
  python3 "$SCRIPTS/parse_india_excel.py" "$f"
  XL_COUNT=$((XL_COUNT + 1))
done

if [ $PDF_COUNT -eq 0 ] && [ $XL_COUNT -eq 0 ]; then
  echo ""
  echo "⚠  No files found in inbox/."
  echo "   Drop a .pdf (US) or .xlsx (India) into the inbox/ folder and re-run."
  exit 0
fi

# ── Git commit & push ──────────────────────────────────────────
echo ""
echo "📦  Committing data/holdings_cost.json …"
cd "$REPO"
git add data/holdings_cost.json
git diff --cached --quiet && echo "  (no changes to commit)" && exit 0
git commit -m "sync: portfolio update $(date '+%Y-%m-%d')"
for attempt in 1 2 3; do
  git rebase --abort 2>/dev/null || true
  git pull --rebase origin main && git push && break
  echo "  push attempt $attempt failed — retrying…"
  sleep 3
done

echo ""
echo "✅  Done! GitHub Actions will refresh live prices within 15 min."
echo "    You can safely delete files from inbox/ now."
