"""
onboard_new_tickers.py
======================
Auto-detects new tickers in holdings_cost.json and adds them to:
  1. fetch_all_prices_vm.py  — US_HOLDINGS list (US stocks only)
  2. signals_update.py       — SECTOR_MAP
  3. parse_broker_pdf.py     — TICKER_MAP (PDF name → ticker)
  4. holdings_cost.json      — buy_date + fx_buy (from transactions_us.json)

Run as part of sync.sh after PDF/xlsx parse, before market_data.py.

Usage: python3 scripts/onboard_new_tickers.py
"""
from __future__ import annotations
import json, re, sys, time, requests
from pathlib import Path
from datetime import datetime

ROOT        = Path(__file__).resolve().parent.parent
COST_FILE   = ROOT / "data" / "holdings_cost.json"
TXNS_FILE   = ROOT / "data" / "transactions_us.json"
VM_SCRIPT   = Path(__file__).parent / "fetch_all_prices_vm.py"
SIG_SCRIPT  = Path(__file__).parent / "signals_update.py"
PDF_SCRIPT  = Path(__file__).parent / "parse_broker_pdf.py"

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_fx_buy(date_str: str, ccy: str = "INR") -> float | None:
    """Fetch INR/USD FX rate for a given date from frankfurter.app."""
    try:
        r = requests.get(
            f"https://api.frankfurter.app/{date_str}?from=USD&to={ccy}",
            timeout=10
        )
        if r.status_code == 200:
            rate = r.json()["rates"].get(ccy)
            print(f"  FX {date_str}: $1 = ₹{rate}")
            return float(rate)
    except Exception as e:
        print(f"  WARN FX lookup failed for {date_str}: {e}", file=sys.stderr)
    return None


def get_sector_from_fd(ticker: str) -> str:
    """Look up sector from FinanceDatabase. Falls back to 'Unknown'."""
    try:
        import financedatabase as fd
        df = fd.Equities().select()
        if ticker in df.index:
            rows = df.loc[[ticker]]
            us   = rows[rows['country'] == 'United States']
            row  = us.iloc[0] if len(us) else rows.iloc[0]
            sec  = str(row.get('sector', '') or '')
            if sec and sec not in ('None', 'nan', ''):
                return sec
    except Exception:
        pass
    return "Unknown"


def patch_file_line(path: Path, old: str, new: str) -> bool:
    """Replace exact line(s) in a file. Returns True if changed."""
    content = path.read_text()
    if old not in content:
        return False
    path.write_text(content.replace(old, new))
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    cost  = json.loads(COST_FILE.read_text())
    txns  = json.loads(TXNS_FILE.read_text()) if TXNS_FILE.exists() else {}

    us_open    = cost.get("us", {}).get("open", [])
    known_tks  = {h["tk"] for h in us_open}

    # ── 1. Fix missing buy_date / fx_buy ─────────────────────────────────────
    buy_trades: dict[str, dict] = {}
    for t in txns.get("trades", []):
        tk = t.get("tk")
        if tk and t.get("open_close") == "open" and t.get("price"):
            buy_trades.setdefault(tk, t)  # earliest buy per ticker

    changed_cost = False
    for h in us_open:
        tk = h["tk"]
        if not h.get("buy_date") or not h.get("fx_buy"):
            bt = buy_trades.get(tk)
            if bt:
                date_str = bt["date"]
                fx = get_fx_buy(date_str)
                if not h.get("buy_date"):
                    h["buy_date"] = date_str
                    print(f"  [{tk}] buy_date = {date_str}")
                if not h.get("fx_buy") and fx:
                    h["fx_buy"] = fx
                    print(f"  [{tk}] fx_buy   = {fx}")
                changed_cost = True

    if changed_cost:
        COST_FILE.write_text(json.dumps(cost, indent=2))
        print("  ✓ holdings_cost.json updated (buy_date/fx_buy)")

    # ── 2. Detect tickers missing from fetch_all_prices_vm.py ────────────────
    vm_text  = VM_SCRIPT.read_text()
    # Extract current US_HOLDINGS list
    m = re.search(r'US_HOLDINGS\s*=\s*\[(.*?)\]', vm_text, re.DOTALL)
    if not m:
        print("  WARN: US_HOLDINGS not found in fetch_all_prices_vm.py", file=sys.stderr)
        return
    existing_vm = set(re.findall(r'"(\w+)"', m.group(1)))
    new_us = [h["tk"] for h in us_open if h["tk"] not in existing_vm and h.get("cls") in ("Stock", "ETF", None)]

    if new_us:
        for tk in new_us:
            old_line = f'    "SHIP","NOW"'
            new_line = f'    "SHIP","NOW","{tk}"'
            # More robust: find last ticker in list and append after it
            # Find the closing ] of US_HOLDINGS
            pattern = r'(US_HOLDINGS\s*=\s*\[[\s\S]*?)(\s*\])'
            replacement = lambda mo: mo.group(1) + f',\n    "{tk}"' + mo.group(2)
            new_vm = re.sub(pattern, replacement, vm_text, count=1)
            if new_vm != vm_text:
                VM_SCRIPT.write_text(new_vm)
                vm_text = new_vm
                print(f"  ✓ [{tk}] added to fetch_all_prices_vm.py US_HOLDINGS")

    # ── 3. Detect tickers missing from signals_update.py SECTOR_MAP ──────────
    sig_text = SIG_SCRIPT.read_text()
    new_sector = [h["tk"] for h in us_open if f'"{h["tk"]}":' not in sig_text]

    if new_sector:
        for tk in new_sector:
            sector = get_sector_from_fd(tk)
            insert_after = '    "VOOG": "ETF",'
            insert_line  = f'\n    "{tk}": "{sector}",'
            if insert_after in sig_text:
                new_sig = sig_text.replace(insert_after, insert_after + insert_line)
                SIG_SCRIPT.write_text(new_sig)
                sig_text = new_sig
                print(f"  ✓ [{tk}] added to signals_update.py SECTOR_MAP ({sector})")

    # ── 4. Detect tickers missing from parse_broker_pdf.py TICKER_MAP ────────
    # Only add if the ticker itself is not referenced anywhere in the map
    pdf_text = PDF_SCRIPT.read_text()
    for h in us_open:
        tk   = h["tk"]
        name = h.get("name", "")
        if not name:
            continue
        # Skip if ticker already mapped (may be under a different name key)
        if f'("{tk}",' in pdf_text or f'("{tk}", ' in pdf_text:
            continue
        norm_name = re.sub(r"[\s,]+", "", name).lower()
        if norm_name and f'"{norm_name}"' not in pdf_text:
            insert_after = "    # Added 2026-06-02\n"
            yf   = h.get("yf", tk)
            cls  = h.get("cls", "Stock")
            new_entry = f'    "{norm_name}": ("{tk}", "{yf}", "{cls}"),\n'
            # Insert before closing brace of TICKER_MAP
            pdf_text = pdf_text.replace(
                insert_after,
                insert_after + new_entry
            )
            print(f"  ✓ [{tk}] '{norm_name}' added to parse_broker_pdf.py TICKER_MAP")
        PDF_SCRIPT.write_text(pdf_text)

    print("  onboard_new_tickers: done")


if __name__ == "__main__":
    main()
