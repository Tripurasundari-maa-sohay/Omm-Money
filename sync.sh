#!/bin/bash
# ─────────────────────────────────────────────────────────────────
#  sync.sh  — drop files into inbox/ and run this once
#
#  Supported inbox files:
#    *.pdf   → US portfolio PDF  (Doha Bank / Saxo statement)
#    *.xlsx  → either:
#               • Saxo transactions xlsx (sheets: Transactions/Trades/Bookings)
#                 → fee patch onto holdings_cost.json + build transactions_us.json
#               • Upstox/India consolidated xlsx (sheet: "All Transactions")
#                 → India holdings update
#
#  After parsing, updates data/*.json and pushes one commit.
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

# ── Process PDFs (US portfolio statement) ──────────────────────
PDF_COUNT=0
for f in "$INBOX"/*.pdf "$INBOX"/*.PDF; do
  [ -f "$f" ] || continue
  echo ""
  echo "📄  US PDF: $(basename "$f")"
  python3 "$SCRIPTS/parse_broker_pdf.py" "$f"
  PDF_COUNT=$((PDF_COUNT + 1))
done

# ── Process xlsx ───────────────────────────────────────────────
# Sniff sheet names to decide if file is Saxo (US) or India (Upstox)
XL_SAXO_COUNT=0
XL_INDIA_COUNT=0
for f in "$INBOX"/*.xlsx "$INBOX"/*.XLSX; do
  [ -f "$f" ] || continue
  echo ""
  echo "📊  xlsx detected: $(basename "$f")"
  KIND=$(python3 - "$f" <<'PYEOF'
import sys, openpyxl
xl = sys.argv[1]
try:
    wb = openpyxl.load_workbook(xl, read_only=True, data_only=True)
    sheets = set(wb.sheetnames)
except Exception as e:
    print("unknown")
    sys.exit(0)
if {"Trades", "Bookings"}.issubset(sheets):
    print("saxo")
elif {"All Transactions"}.issubset(sheets) or {"Upstox"}.issubset(sheets):
    print("india")
else:
    print("unknown")
PYEOF
)
  case "$KIND" in
    saxo)
      echo "   → Saxo/Doha format · patching fees + building transactions_us.json"
      python3 "$SCRIPTS/patch_fees_from_xlsx.py" "$f"
      python3 "$SCRIPTS/build_transactions_us.py" "$f"
      XL_SAXO_COUNT=$((XL_SAXO_COUNT + 1))
      ;;
    india)
      echo "   → India/Upstox format"
      python3 "$SCRIPTS/parse_india_excel.py" "$f"
      XL_INDIA_COUNT=$((XL_INDIA_COUNT + 1))
      ;;
    *)
      echo "   ⚠ unknown xlsx format (sheets do not match Saxo or Upstox layouts) — skipping"
      ;;
  esac
done

if [ $PDF_COUNT -eq 0 ] && [ $XL_SAXO_COUNT -eq 0 ] && [ $XL_INDIA_COUNT -eq 0 ]; then
  echo ""
  echo "⚠  No files found in inbox/."
  echo "   Drop a .pdf (US) or .xlsx (US or India) into the inbox/ folder and re-run."
  exit 0
fi

# ── Refresh live prices + all derived chart data ───────────────
# Run the same pipeline GH Actions runs (`full_update.yml`) so the dashboard
# tile values and chart series catch up to the new PDF *immediately*, not
# 5-15 min later when cron fires.
echo ""
echo "🔄  Refreshing live prices + weekly/combined charts + signals …"

# Activate the local venv if present (sync.sh sister script parse.sh creates it).
if [ -f "$REPO/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$REPO/.venv/bin/activate"
fi

python3 "$SCRIPTS/market_data.py"     || echo "  ⚠ market_data.py failed — keeping last good prices file"
python3 "$SCRIPTS/signals_update.py"  || echo "  ⚠ signals_update.py failed — signals stale"
python3 "$SCRIPTS/data_audit.py"      || echo "  ⚠ data_audit.py failed — audit stale"
python3 "$SCRIPTS/patch_chart.py"     || echo "  ⚠ patch_chart.py failed — chart anchor stale"

# ── Git commit & push ──────────────────────────────────────────
echo ""
echo "📦  Committing data updates …"
cd "$REPO"
git add data/holdings_cost.json
[ $XL_SAXO_COUNT -gt 0 ] && git add data/transactions_us.json
git add data/processed/holdings_prices.json \
        data/processed/market_indices.json   \
        data/processed/stock_signals.json    \
        data/processed/audit.json            \
        data/processed/audit_history.json 2>/dev/null || true
git diff --cached --quiet && echo "  (no changes to commit)" && exit 0

MSG="sync: portfolio update $(date '+%Y-%m-%d')"
[ $PDF_COUNT -gt 0 ]      && MSG="$MSG · PDF($PDF_COUNT)"
[ $XL_SAXO_COUNT -gt 0 ]  && MSG="$MSG · Saxo xlsx($XL_SAXO_COUNT)"
[ $XL_INDIA_COUNT -gt 0 ] && MSG="$MSG · India xlsx($XL_INDIA_COUNT)"
git commit -m "$MSG"
for attempt in 1 2 3; do
  git rebase --abort 2>/dev/null || true
  git pull --rebase origin main && git push && break
  echo "  push attempt $attempt failed — retrying…"
  sleep 3
done

echo ""
echo "✅  Done! Dashboard tiles + charts already match the new statement."
echo "    GitHub Actions will keep refreshing live prices every 5–15 min."
echo "    You can safely delete files from inbox/ now."
