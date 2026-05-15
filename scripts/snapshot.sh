#!/usr/bin/env bash
# Usage: bash scripts/snapshot.sh [label]
# Copies holdings_cost.json to data/snapshots/ with today's date + optional label
# Then creates a git tag.
#
# Examples:
#   bash scripts/snapshot.sh                    → holdings_cost_2026-05-15.json
#   bash scripts/snapshot.sh after-may-trades   → holdings_cost_2026-05-15_after-may-trades.json

set -euo pipefail

LABEL="${1:-}"
DATE=$(date +%Y-%m-%d)
FILENAME="holdings_cost_${DATE}${LABEL:+_$LABEL}.json"
DEST="data/snapshots/${FILENAME}"

cp data/holdings_cost.json "$DEST"
echo "✓ Snapshot saved → $DEST"

# Stage snapshot and commit
git add "$DEST"
git diff --cached --quiet && echo "(no changes to commit)" && exit 0

git commit -m "snapshot: holdings_cost ${DATE}${LABEL:+ ($LABEL)}"

# Git tag for easy history lookup
TAG="snapshot-${DATE}${LABEL:+-$LABEL}"
git tag -f "$TAG"
echo "✓ Git tag: $TAG"

echo ""
echo "Push with: git push && git push origin --tags"
