# Omm-Money — Personal wealth dashboard suite

Monorepo of multiple wealth dashboards, each hosted as its own PWA under
GitHub Pages.

## Dashboards

| Path | URL | Status |
|------|-----|--------|
| `portfolio/` | `https://<owner>.github.io/Omm-Money/portfolio/` | live |
| `net-wealth/` | `https://<owner>.github.io/Omm-Money/net-wealth/` | live (ODIN v1) |
| `retirement/` | `https://<owner>.github.io/Omm-Money/retirement/` | planned |
| `realestate/` | `https://<owner>.github.io/Omm-Money/realestate/` | planned |

## Layout

```
Omm-Money/
├── .github/workflows/   # GH Actions (cron jobs for live data)
├── portfolio/           # Stocks portfolio PWA
│   ├── index.html
│   ├── sw.js
│   ├── manifest.json
│   ├── icons/
│   ├── data/            # cost basis, prices, history, signals
│   ├── scripts/         # market_data.py, parsers, audit, screener
│   ├── inbox/           # drop broker PDFs/xlsx here for sync.sh
│   ├── parse.sh
│   └── sync.sh
└── README.md
```

## GitHub Pages config

- Settings → Pages → Source: `main` branch / `/ (root)` → Save
- Each subfolder with an `index.html` is auto-hosted at `/Omm-Money/<folder>/`
