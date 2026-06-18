#!/usr/bin/env bash
# Single entrypoint for pushing portfolio dashboard changes.
# - Verifies sw.js CACHE + index.html build-stamp are in sync
# - Bumps both if --bump passed
# - Commits + pushes with rebase retry (VM commits every minute)
# - SCPs to VM /home/opc/web/portfolio/
# - Verifies VM URL + GH raw serve the new version
#
# Usage:
#   tools/push.sh "commit message"
#   tools/push.sh --bump "commit message"   # auto-bump version before commit

set -euo pipefail

REPO="/Users/sabarna/Omm-Money"
SW="$REPO/portfolio/sw.js"
HTML="$REPO/portfolio/index.html"
VM_KEY="$HOME/Downloads/ssh-key-2026-05-26.key"
VM_USER="opc"
VM_HOST="145.241.158.254"
VM_DIR="/home/opc/web/portfolio"

cd "$REPO"

BUMP=0
if [[ "${1:-}" == "--bump" ]]; then BUMP=1; shift; fi
MSG="${1:-chore: portfolio update}"

cur_sw=$(grep -oE "portfolio-v[0-9]+" "$SW" | head -1 | sed 's/portfolio-v//')
cur_html=$(grep -oE "build v[0-9]+" "$HTML" | head -1 | sed 's/build v//')

echo "→ Current: sw.js=v$cur_sw  index.html=v$cur_html"

if [[ "$cur_sw" != "$cur_html" ]]; then
  echo "✗ ABORT: sw.js v$cur_sw out of sync with build-stamp v$cur_html"
  echo "  Fix one to match the other, or re-run with --bump"
  exit 1
fi

if [[ "$BUMP" == 1 ]]; then
  new=$((cur_sw + 1))
  echo "→ Bumping v$cur_sw → v$new"
  sed -i '' "s/portfolio-v$cur_sw/portfolio-v$new/" "$SW"
  sed -i '' "s/build v$cur_sw/build v$new/" "$HTML"
  cur_sw=$new
fi

git add -u portfolio/ tools/ 2>/dev/null || true
git add portfolio/sw.js portfolio/index.html tools/push.sh 2>/dev/null || true
if [[ -z "$(git status --porcelain)" ]]; then
  echo "→ No changes to commit (will still re-deploy current HEAD to VM)"
else
  git commit -m "$MSG"
fi

echo "→ Pushing with rebase retry..."
for i in 1 2 3 4 5 6 7 8; do
  git pull --rebase -X theirs origin main >/dev/null 2>&1 || true
  if git push origin main 2>&1 | tee /tmp/push.log | grep -q "main -> main"; then
    echo "✓ Push attempt $i: OK"
    break
  fi
  if [[ $i == 8 ]]; then
    echo "✗ Push failed after 8 attempts"; cat /tmp/push.log; exit 1
  fi
  sleep 2
done

echo "→ Deploying to VM ($VM_HOST)..."
scp -i "$VM_KEY" -o StrictHostKeyChecking=no -q \
  "$SW" "$HTML" \
  "$VM_USER@$VM_HOST:$VM_DIR/"

vm_sw=$(ssh -i "$VM_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" \
  "grep -oE 'portfolio-v[0-9]+' $VM_DIR/sw.js | head -1 | sed 's/portfolio-v//'")
vm_html=$(ssh -i "$VM_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" \
  "grep -oE 'build v[0-9]+' $VM_DIR/index.html | head -1 | sed 's/build v//'")

if [[ "$vm_sw" == "$cur_sw" && "$vm_html" == "$cur_sw" ]]; then
  echo "✓ VM serving v$vm_sw"
else
  echo "✗ VM mismatch: sw=v$vm_sw html=v$vm_html (expected v$cur_sw)"
  exit 1
fi

echo "→ Verifying GH raw..."
raw_sw=$(curl -s "https://raw.githubusercontent.com/Tripurasundari-maa-sohay/Omm-Money/main/portfolio/sw.js" \
  | grep -oE "portfolio-v[0-9]+" | head -1 | sed 's/portfolio-v//')
if [[ "$raw_sw" == "$cur_sw" ]]; then
  echo "✓ GH raw v$raw_sw"
else
  echo "! GH raw v$raw_sw lagging v$cur_sw — CDN propagation (≤5 min)"
fi

echo "→ Checking GH Pages..."
pages_sw=$(curl -s "https://tripurasundari-maa-sohay.github.io/Omm-Money/portfolio/sw.js?nocache=$RANDOM" \
  | grep -oE "portfolio-v[0-9]+" | head -1 | sed 's/portfolio-v//')
if [[ "$pages_sw" == "$cur_sw" ]]; then
  echo "✓ GH Pages v$pages_sw"
else
  echo "! GH Pages v$pages_sw lagging v$cur_sw — Pages may be throttled by VM commit spam"
  echo "  Workaround: use VM URL https://save.145-241-158-254.nip.io/portfolio/"
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  Build v$cur_sw deployed"
echo "  VM    : https://save.145-241-158-254.nip.io/portfolio/"
echo "  Pages : https://tripurasundari-maa-sohay.github.io/Omm-Money/portfolio/"
echo "═══════════════════════════════════════════════"
