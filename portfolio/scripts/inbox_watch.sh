#!/bin/bash
# inbox_watch.sh — called by macOS LaunchAgent when inbox/ changes
# Runs sync.sh only if new PDF or XLSX found; logs to ~/Library/Logs/portfolio-sync.log

LOG="$HOME/Library/Logs/portfolio-sync.log"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
INBOX="$REPO/inbox"

echo "────────────────────────────────" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S')  inbox change detected" >> "$LOG"

# Check for PDF or XLSX
PDF_FOUND=$(find "$INBOX" -maxdepth 1 \( -name "*.pdf" -o -name "*.PDF" \) 2>/dev/null | head -1)
XLS_FOUND=$(find "$INBOX" -maxdepth 1 \( -name "*.xlsx" -o -name "*.XLSX" \) 2>/dev/null | head -1)

if [ -z "$PDF_FOUND" ] && [ -z "$XLS_FOUND" ]; then
  echo "  No PDF/XLSX found — skipping" >> "$LOG"
  exit 0
fi

echo "  Files: PDF=${PDF_FOUND:-none}  XLS=${XLS_FOUND:-none}" >> "$LOG"
bash "$REPO/sync.sh" >> "$LOG" 2>&1
echo "  sync.sh exit: $?" >> "$LOG"
