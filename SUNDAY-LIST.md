# SUNDAY LIST — recurring security + maintenance

Run weekly (Sunday). Tick, don't skip the 🔴 items.

---

## 🔴 SECRET KEEPSAFE — credentials hygiene

### Rotate-now (exposed in plaintext chat during 2026-06-10 build)
- [ ] **Google OAuth client_secret** (ODIN gate, `GOCSPX-…`) — shared in plaintext during 2026-06-10 build.
      Rotate: GCP Console → Credentials → "ODIN Net-Wealth Gate" → reset secret →
      update `/home/opc/oauth2-proxy.cfg` (`client_secret = ...`) → `sudo systemctl restart oauth2-proxy`.
      Client ID is public-safe; only the secret must rotate. (Secret value lives on VM cfg + chat only — never in this repo.)
- [ ] **Confirm old secret revoked** in GCP after new one is live on VM.

### Standing credential checks
- [ ] **GITHUB_TOKEN** expiry — GCP-style PAT in `/home/opc/angel_env.sh` + `/etc/save-api.env`.
      Test: `source /home/opc/angel_env.sh && curl -s -o /dev/null -w "%{http_code}" -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user` → must be `200`.
      Token expiry = silent pipeline freeze (no alert unless ntfy topic set).
- [ ] **No secrets in committed code** — grep before any push:
      `grep -rn "ghp_\|GOCSPX-\|apikey\s*=\|client_secret\|password\s*=\s*['\"]" portfolio/scripts/ net-wealth/index.html portfolio/index.html tools/ *.md`
      (NOTE: this SUNDAY-LIST itself names the secret to rotate — delete that line once rotated.)
- [ ] **File perms on VM secret files** (must be `600`, owner `opc` or `root`):
      `/home/opc/oauth2-proxy.cfg`, `/home/opc/angel_env.sh`, `/etc/save-api.env`, `/home/opc/oauth2_emails.txt`
      Check: `ls -l /home/opc/oauth2-proxy.cfg /home/opc/angel_env.sh && sudo ls -l /etc/save-api.env`
- [ ] **oauth2-proxy cookie_secret** stays VM-only (never chat/commit). If ever exposed → regenerate
      (`python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`),
      put in cfg, restart oauth2-proxy. (Logs out all sessions — re-login required.)

---

## 🔵 AUTH GATE — health (post 2026-06-10 build)

- [ ] **Gate denies anon** — `curl -s -o /dev/null -w "%{http_code}\n" https://save.145-241-158-254.nip.io/net-wealth/data/seed.json` → must be `302` (redirect to Google), NOT `200`.
- [ ] **Email whitelist intact** — `cat /home/opc/oauth2_emails.txt` → only authorized gmail(s).
- [ ] **Services up** — `sudo systemctl is-active oauth2-proxy caddy save-api` → all `active`.
- [ ] **TLS cert valid** — Caddy auto-renews Let's Encrypt; spot-check expiry:
      `echo | openssl s_client -servername save.145-241-158-254.nip.io -connect save.145-241-158-254.nip.io:443 2>/dev/null | openssl x509 -noout -enddate`
- [ ] **oauth2-proxy version** — check for CVE/updates: current `v7.6.0` (aarch64).

---

## ⛔ VM DOCROOT — never git-manage it
- `/home/opc/web` is now a **standalone dir** (`.git` removed 2026-06-10). It holds the
  ONLY copy of personal data (seed, history, holdings_cost, holdings_prices, signals).
- **NEVER** `git clone`/`pull`/`reset` over it — personal files are gone from origin,
  so a pull would DELETE them. Deploy code via `scp` only.
- Back it up: `tar czf ~/web-bak-$(date +%F).tgz -C /home/opc web/net-wealth/data web/portfolio/data` (do weekly).

## 🟠 ZERO-TRACE — after Phase 2 cutover (verify GitHub stays clean)

- [ ] **Raw URLs 404** for personal files — must NOT return data:
      `for f in net-wealth/data/seed.json net-wealth/data/history.json portfolio/data/holdings_cost.json portfolio/data/transactions_us.json portfolio/data/processed/stock_signals.json portfolio/data/processed/holdings_prices.json; do echo -n "$f → "; curl -s -o /dev/null -w "%{http_code}\n" "https://raw.githubusercontent.com/Tripurasundari-maa-sohay/Omm-Money/main/$f"; done`
      (all `404`).
- [ ] **No personal data re-committed** — `fetch_all_prices_vm.py` / `daily_nw_snapshot.py` / `signals_update.py` / `save_api.py` write LOCAL VM only for personal files; only `market_indices.json` + `screener.json` go to GitHub.
- [ ] **git history clean** — `git log --all --oneline -- net-wealth/data/seed.json` → empty after scrub.
      If re-appears: a VM script regressed to GitHub commit. Find + fix.
- [ ] **Public still works** — market_indices + screener fetch OK (these stay public by design).

---

## 🟢 PIPELINE — VM cron + data freshness
(see CLAUDE.md AUDIT CHECKLIST for full list)
- [ ] `tail -20 /home/opc/prices.log` — no `401 Bad credentials`.
- [ ] `holdings_prices.json` `generated` < 5 min during market hours.
- [ ] VM crontab intact: `crontab -l` (india/us prices, NW snapshot, signals).
- [ ] NW snapshot growing: history.json gains 1 row/weekday.
