# SABARNA • DBG Portfolio Dashboard

A live portfolio dashboard hosted on **GitHub Pages**, updated automatically
every 15 minutes by **GitHub Actions**. **No Python or Node.js needed on your laptop.**

---

## What's in this repo

```
portfolio-dashboard/
│
├── dashboard/
│   └── index.html                        ← The entire dashboard (open this in browser)
│
├── scripts/
│   └── market_indices.py                 ← Fetches live prices (runs on GitHub, not your PC)
│
├── data/
│   └── processed/
│       └── market_indices.json           ← Auto-updated by GitHub Actions every 15 min
│
└── .github/
    └── workflows/
        └── full_update.yml               ← The automation that runs everything
```

---

## Setup (one-time, ~10 minutes, done entirely in your browser)

### Step 1 — Create the repository on GitHub

1. Go to **github.com → New repository**
2. Name it `portfolio-dashboard`
3. Set it to **Public** (required for free GitHub Pages)
4. Click **Create repository**

### Step 2 — Upload the files

Upload files in the exact folder structure above. GitHub's web UI lets you
drag-and-drop. Make sure the paths are correct:

| Upload this file | To this path in the repo |
|-----------------|--------------------------|
| `index.html` | `dashboard/index.html` |
| `market_indices.py` | `scripts/market_indices.py` |
| `market_indices.json` | `data/processed/market_indices.json` |
| `full_update.yml` | `.github/workflows/full_update.yml` |

> **Tip for creating folders in GitHub's web uploader:**
> When typing the file name, type `dashboard/index.html` — GitHub automatically
> creates the `dashboard/` folder for you.

### Step 3 — Enable GitHub Pages

1. Go to your repo → **Settings → Pages** (left sidebar)
2. Under **Source**, select **Deploy from a branch**
3. Branch: `main` · Folder: `/dashboard`
4. Click **Save**
5. Wait ~60 seconds then visit `https://YOUR_USERNAME.github.io/portfolio-dashboard/`

### Step 4 — Enable GitHub Actions

1. Go to your repo → **Actions** tab
2. If prompted, click **"I understand my workflows, go ahead and enable them"**
3. Click **full_update.yml** → click **"Run workflow"** → **"Run workflow"** to test it manually right now
4. After ~60 seconds you'll see a green checkmark ✅
5. From now on it runs **automatically every 15 minutes**

### Step 5 — Verify it's working

1. Go to **Actions tab** — you should see runs every 15 minutes
2. Click any run → expand steps to see the fetched prices in the logs
3. Open `data/processed/market_indices.json` in your repo — values should be live
4. Refresh your dashboard URL — market indices should show real numbers

---

## How it works (no laptop Python needed)

```
Your browser  →  github.com/YOUR_USERNAME/portfolio-dashboard/dashboard/index.html
                          ↓  (GitHub Pages serves this as a website)
                     index.html  →  fetch('data/processed/market_indices.json')
                                             ↑
                              GitHub Actions runs every 15 min:
                              1. Starts an Ubuntu machine (free, GitHub pays for it)
                              2. pip install yfinance pytz
                              3. python scripts/market_indices.py
                              4. Commits market_indices.json back to the repo
                              5. Machine shuts down (you pay nothing)
```

---

## If something isn't working

| Problem | Fix |
|---------|-----|
| Dashboard shows `--` for all prices | Go to Actions tab and run the workflow manually |
| GitHub Actions shows red ✗ | Click the failed run → read the error → usually a typo in file path |
| Pages shows 404 | Check Settings → Pages: source must be `main` branch, `/dashboard` folder |
| Prices are stale (several hours old) | GitHub occasionally delays cron jobs — click "Run workflow" to force an update |

---

## Customising

- **Your holdings data**: Edit `data/processed/holdings_enriched.json` directly in GitHub's web editor, or upload a new version. The `js/` chart functions read from this file.
- **Tab content**: Everything is in `dashboard/index.html` — edit it directly in GitHub's file editor (pencil icon).
- **Adding more scripts**: Add them to `scripts/` and call them in `full_update.yml` after the `market_indices.py` step.
