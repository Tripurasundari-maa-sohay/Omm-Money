# Portfolio Dashboard — Claude Code Rules

## Git Conflict Resolution (CRITICAL)

When `git pull --rebase` or `git push` causes conflicts:

**ALWAYS:**
```bash
git checkout --theirs data/holdings_cost.json
git checkout --theirs data/processed/
git add data/
git rebase --continue   # or: git push origin main
```

**NEVER:**
```bash
git reset --hard origin/main   # WIPES ALL CODE CHANGES
```

**After resolving any conflict, verify code survived:**
```bash
grep -n "S\.inrFxMon\s*=" index.html          # must exist
grep -n "build_inr_monthly\|build_fx_buy" scripts/market_data.py  # must exist
```

Only `data/` files should be taken from remote (GitHub Actions owns them).
Code files (`index.html`, `*.py`, `*.yml`, `sw.js`) are always ours — never reset.

## Data File Ownership

| File | Owner | Conflict resolution |
|------|-------|---------------------|
| `data/processed/*.json` | GitHub Actions | `--theirs` |
| `data/holdings_cost.json` | GitHub Actions (FX/prices) + local (buy_date/fx_buy) | `--theirs` then re-apply local fields |
| `data/history/` | GitHub Actions | `--theirs` |
| `index.html` | Claude/developer | NEVER reset |
| `scripts/*.py` | Claude/developer | NEVER reset |
| `sw.js` | Claude/developer | NEVER reset |
| `.github/workflows/*.yml` | Claude/developer | NEVER reset |
