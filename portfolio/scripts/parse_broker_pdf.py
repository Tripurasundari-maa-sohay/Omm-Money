#!/usr/bin/env python3
"""
parse_broker_pdf.py — US broker statement → holdings_cost.json

Usage:
    python scripts/parse_broker_pdf.py path/to/statement.pdf
    python scripts/parse_broker_pdf.py path/to/statement.pdf --output data/holdings_cost.json

Extracts every field the dashboard needs:
    • account value, cash, net deposits, total P/L          → us.*
    • monthly cash, account value, % return, P&L, costs     → us.monthly
    • open positions  (qty, avg buy, fees, dividends)       → us.open
    • closed positions (income, costs, realised, return)    → us.closed
    • charges breakdown                                     → us.charges_breakdown

The India section is preserved as-is from the existing JSON.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pip install pdfplumber")


# ───────────────────────── INSTRUMENT MAP ─────────────────────────
# Normalised name → (ticker, yfinance symbol, asset class).
# Add a line here when a new instrument appears in your statement.
TICKER_MAP = {
    "alphabetinc.classc":                  ("GOOG",  "GOOG",  "Stock"),
    "amazon.cominc.":                      ("AMZN",  "AMZN",  "Stock"),
    "broadcominc.":                        ("AVGO",  "AVGO",  "Stock"),
    "corninginc.":                         ("GLW",   "GLW",   "Stock"),
    "gevernovainc":                        ("GEV",   "GEV",   "Stock"),
    "gevernovainc.":                       ("GEV",   "GEV",   "Stock"),
    "microntechnologyinc.":                ("MU",    "MU",    "Stock"),
    "microsoftcorp.":                      ("MSFT",  "MSFT",  "Stock"),
    "mpmaterialscorp.":                    ("MP",    "MP",    "Stock"),
    "totalenergiesse":                     ("TTE",   "TTE",   "Stock"),
    "isharesmscisouthkoreaetf":            ("EWY",   "EWY",   "ETF"),
    "vanguards&p500growthetf":             ("VOOG",  "VOOG",  "ETF"),
    "8x8inc.":                             ("EGHT",  "EGHT",  "Stock"),
    "costcowholesalecorp.":                ("COST",  "COST",  "Stock"),
    "digitaloceanholdingsinc.":            ("DOCN",  "DOCN",  "Stock"),
    "innodatainc.":                        ("INOD",  "INOD",  "Stock"),
    "inversionesyrepresentacionessa-adr":  ("IRS",   "IRS",   "Stock"),
    "metaplatformsinc.":                   ("META",  "META",  "Stock"),
    "nvidiacorp.":                         ("NVDA",  "NVDA",  "Stock"),
    "rocketlabcorporation":                ("RKLB",  "RKLB",  "Stock"),
    "tollbrothersinc.":                    ("TOL",   "TOL",   "Stock"),
    "abrdnphysicalsilversharesetf":        ("SIVR",  "SIVR",  "ETF"),
    "directiondailymubull2xetf":           ("MUU",   "MUU",   "ETF"),
    "direxiondailymubull2xetf":            ("MUU",   "MUU",   "ETF"),
    "globalxdaxgermanyetf":                ("DAX",   "DAX",   "ETF"),
    "keelinfrastructurecorp":              ("KEEL",  "KEEL",  "Stock"),
    "roundhillhumanoidroboticsetf":        ("HUMN",  "HUMN",  "ETF"),
    # Added 2026-05-28
    "seanergymaritimeholdings":            ("SHIP",  "SHIP",  "Stock"),
    "servicenowinc.":                      ("NOW",   "NOW",   "Stock"),
    "corp.servicenowinc.":                 ("NOW",   "NOW",   "Stock"),
    # Added 2026-06-02
    "oraclecorp.":                         ("ORCL",  "ORCL",  "Stock"),
    "oraclecorporation":                   ("ORCL",  "ORCL",  "Stock"),
    # Added 2026-06-06
    "intelcorp.":                          ("INTC",  "INTC",  "Stock"),
    "intelcorporation":                    ("INTC",  "INTC",  "Stock"),
    "jiadeltd":                            ("JDZG",  "JDZG",  "Stock"),
    "jiadelimited":                        ("JDZG",  "JDZG",  "Stock"),
}


def clean_instrument_name(name: str) -> str:
    """Strip PDF boilerplate that gets prepended (left column bleeds into right column).
    All real instrument names start with uppercase or digit.
    All boilerplate prefixes are lowercase-only runs."""
    tokens = name.split()
    for i, tok in enumerate(tokens):
        if tok and (tok[0].isupper() or tok[0].isdigit()):
            return " ".join(tokens[i:])
    return name  # fallback: return as-is


def norm(s: str) -> str:
    return re.sub(r"[\s,]+", "", s or "").lower()


def lookup(name: str):
    key = norm(name).split("(")[0]  # drop trailing "(ISIN:..." if present
    if key in TICKER_MAP:
        return TICKER_MAP[key]
    # try a fuzzy contains match
    for k, v in TICKER_MAP.items():
        if k in key or key in k:
            return v
    print(f"  [WARN] unmapped instrument: {name!r}", file=sys.stderr)
    return ("UNKNOWN_" + key[:10], "", "Stock")


# ───────────────────────── LOW-LEVEL HELPERS ─────────────────────────
def _floats(s: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?", s)]


def page_rows(page, y_tol: int = 4) -> list[list[str]]:
    """Group page words into rows by top-y proximity; sort each row left-to-right.
       Returns list of rows where each row is a list of word strings."""
    words = page.extract_words()
    buckets: dict[int, list[dict]] = defaultdict(list)
    for w in words:
        key = round(w["top"] / y_tol) * y_tol
        buckets[key].append(w)
    rows = []
    for y in sorted(buckets):
        rows.append([w["text"] for w in sorted(buckets[y], key=lambda x: x["x0"])])
    return rows


# ───────────────────────── SECTION PARSERS ─────────────────────────
def parse_account_summary(pdf) -> dict:
    """Page 2 — find the values that appear with 'USD' suffix in canonical order."""
    rows = page_rows(pdf.pages[1])
    # Period
    period_from = period_to = None
    for r in rows:
        line = " ".join(r)
        m = re.search(r"(\d{2}-\w{3}-\d{4}).*?(\d{2}-\w{3}-\d{4})", line)
        if m and not period_from:
            period_from, period_to = m.group(1), m.group(2)
    # Values: from the row that contains the four USD-marked numbers
    nums_usd = []
    for r in rows:
        for tok in r:
            m = re.match(r"^(-?\d{1,3}(?:,\d{3})*\.\d{2})USD$", tok)
            if m:
                nums_usd.append(float(m.group(1).replace(",", "")))
    # In the canonical PDF order: start_value (often 0.00), then P/L, deposits, end_value
    # but Doha sometimes lays them out: P/L, deposits, end, start.  Distinguish by magnitude:
    # • end_value > deposits (end_value = deposits + total_pl)
    # • start_value = 0 if new account
    pl = deposits = end = start = 0.0
    if len(nums_usd) >= 3:
        # Common observation: row reads "P/L deposits end" then a separate "0.00 USD" for start
        srtd = sorted(nums_usd[:4]) if len(nums_usd) >= 4 else sorted(nums_usd[:3] + [0.0])
        start = srtd[0]
        pl    = srtd[1]
        deposits = srtd[2]
        end   = srtd[3] if len(srtd) > 3 else (deposits + pl)
        # Sanity check: end_value should be the largest of the four numbers
        if end < deposits or end < abs(pl):
            print(
                f"  [WARN] parse_account_summary: end_value ({end}) may not be largest "
                f"— nums_usd={nums_usd[:4]}. Sort-based extraction may be wrong.",
                file=sys.stderr,
            )
    return {
        "start_value":  start,
        "end_value":    end,
        "total_pl":     pl,
        "net_deposits": deposits,
        "period_from":  period_from,
        "period_to":    period_to,
    }


def parse_monthly_timeline(pdf) -> dict:
    """Page 3 (Cash/Accruals/Position/Account-value by month) + Page 4 (% return, P/L, costs).
       Both pages have a two-column layout: x<240 is explanatory prose, x>=240 is the
       data table.  We crop to the right column before extracting rows."""
    rows3 = page_rows(pdf.pages[2].crop((240, 0, pdf.pages[2].width, pdf.pages[2].height)))
    rows4 = page_rows(pdf.pages[3].crop((240, 0, pdf.pages[3].width, pdf.pages[3].height)))

    # Collect date headers in order of appearance (page 3)
    date_set, dates = set(), []
    for r in rows3 + rows4:
        for tok in r:
            m = re.match(r"^(\d{1,2}-\w{3}-\d{4})$", tok)
            if m and tok not in date_set:
                date_set.add(tok); dates.append(tok)
    # We expect 7 monthly columns in the table; keep the first 7 we find
    dates = dates[:7]
    labels = []
    label_dates = []   # ISO date strings (YYYY-MM-DD) — used for real S&P fetch
    for i, d in enumerate(dates):
        parts = d.split("-")  # e.g. ["01", "Nov", "2025"]
        mo = parts[1]
        yr = parts[2][-2:]
        labels.append(f"{mo}-{yr}" if i in (0, len(dates) - 1) else mo)
        try:
            from datetime import datetime as _dt
            label_dates.append(_dt.strptime(d, "%d-%b-%Y").strftime("%Y-%m-%d"))
        except Exception:
            label_dates.append(None)

    def row_starting_with(rows, label_tokens):
        """Find a row whose first tokens match label_tokens AND contains numeric data.
           If the label sits on a row by itself (e.g. 'Benchmark') the data row often
           follows immediately — we fall back to that row."""
        target = "".join(label_tokens).lower()

        def floats_in(r):
            out = []
            for t in r:
                try:
                    out.append(float(t.replace(",", "").replace("%", "")))
                except ValueError:
                    continue
            return out

        for i, r in enumerate(rows):
            if not "".join(r).lower().startswith(target):
                continue
            vals = floats_in(r)
            if vals:
                return vals
            # label was on its own line — try the very next row
            if i + 1 < len(rows):
                vals = floats_in(rows[i + 1])
                if vals:
                    return vals
            # not a match — keep scanning for a later row that does contain data
        return []

    # Page 3 rows
    cash_vals  = row_starting_with(rows3, ["Cash"])[:7]
    acct_vals  = row_starting_with(rows3, ["Accountvalue"])[:7]

    # Page 4 rows
    pct_vals       = row_starting_with(rows4, ["%Return"])[:6]
    bench_vals     = row_starting_with(rows4, ["Benchmark"])[:6]
    pl_vals        = row_starting_with(rows4, ["TotalP/L"])[:6]
    cost_vals      = row_starting_with(rows4, ["Totalcosts"])[:6]

    # Pad missing months so all arrays line up at len(labels)
    def pad(arr, n, fill=0.0):
        return (arr + [fill] * n)[:n]

    n = len(labels)
    cash  = pad(cash_vals, n)
    acct  = pad(acct_vals, n)
    # monthly arrays are over months only — first label is the start date (Nov-25) with 0% / 0 P&L
    pct   = [0.0] + pad(pct_vals,  n - 1)
    bench = [0.0] + pad(bench_vals,n - 1)
    pl    = [0.0] + pad(pl_vals,   n - 1)
    cost  = [0.0] + pad(cost_vals, n - 1)

    def cum(monthlies):
        c, out = 1.0, []
        for r in monthlies:
            c *= 1 + r / 100
            out.append(round((c - 1) * 100, 2))
        return out

    return {
        "labels":               labels,
        "label_dates":          label_dates,   # ISO dates for real S&P 500 fetch
        "cash_balance":         cash,
        "account_value":        acct,
        "monthly_pct_return":   pct,
        "monthly_bench_return": bench,         # broker's benchmark (kept for reference)
        "monthly_pl":           pl,
        "monthly_cost":         cost,
        "port_return_cum_pct":  cum(pct),
        "snp_return_cum_pct":   cum(bench),    # broker benchmark cumulative (used until real S&P fetched)
        "cash_deployed":        [round(av - sum(pl[: i + 1]), 2) for i, av in enumerate(acct)],
    }


# ── data-row pattern for P/L breakdown rows (pages 5-7)
#    "<name>  <income>  <costs>  <P/L>  <ret>%"  where name may have multi-word
PL_ROW_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9&.,/()\- ]+?)\s+"
    r"(?P<income>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+"
    r"(?P<costs>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+"
    r"(?P<pl>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+"
    r"(?P<ret>-?\d+\.\d+)%\s*$"
)


def parse_pl_breakdown(pdf) -> list[dict]:
    """Pages 5, 6, 7 — extract per-instrument P&L rows.

    Previous approach cropped at x=300 which sliced off the FRONT of every
    instrument name (leaving fragments like 'ClassC', 'nc.', '500GrowthETF').
    The regex pattern is specific enough (ends with 4 numbers + %) that we
    don't need the left crop — relying on pattern matching alone is safer.
    """
    rows_out = []
    for page_idx in (4, 5, 6):  # pages 5, 6, 7 → zero-indexed
        if page_idx >= len(pdf.pages):
            continue
        page = pdf.pages[page_idx]
        # Crop at x=50 (not 300 which truncated names, not 0 which drops some rows).
        cropped = page.crop((50, 0, page.width, page.height))
        for row in page_rows(cropped):
            line = " ".join(row)
            # Skip page headers/footers. Add your broker's header string here if it bleeds into rows.
            _SKIP_TOKENS = ("Page", "DohaBank")
            if not line or any(t in line for t in _SKIP_TOKENS):
                continue
            m = PL_ROW_RE.match(line)
            if not m:
                continue
            name = clean_instrument_name(m.group("name").strip())
            # Skip aggregate/header rows but NOT instrument names that start with "Total"
            # (e.g. "TotalEnergiesSE" is a valid instrument, "Total P/L" is not)
            nl = name.lower()
            if nl.startswith(("total ", "totalp", "totalc", "instrument", "p/l ")):
                continue
            rows_out.append({
                "name":    name,
                "income":  float(m.group("income").replace(",", "")),
                "costs":   float(m.group("costs").replace(",", "")),
                "pl":      float(m.group("pl").replace(",", "")),
                "ret_pct": float(m.group("ret")),
            })
    return rows_out


def parse_holdings(pdf) -> list[dict]:
    """Pages 8, 9, 10 — for each holding the name sits one row above the numeric row.
       Numeric row pattern (after currency tag): qty conv open current %chg upl mv mv%."""
    out = []
    DATA_RE = re.compile(
        r"^USD\s+"
        r"(?P<qty>-?\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s+"
        r"(?P<conv>\d+\.\d+)\s+"
        r"(?P<open>\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<cur>\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<chg>-?\d+\.\d+)%\s+"
        r"(?P<upl>-?\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<mv>\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<wt>\d+\.\d+)%\s*$"
    )
    for page_idx in (7, 8, 9):  # pages 8, 9, 10
        if page_idx >= len(pdf.pages):
            continue
        rows = page_rows(pdf.pages[page_idx])
        for i, row in enumerate(rows):
            line = " ".join(row)
            # Bug 1 fix: search for "USD " anywhere in the line, not just at start
            usd_pos = line.find("USD ")
            if usd_pos == -1:
                continue
            data_part = line[usd_pos:]
            m = DATA_RE.match(data_part)
            if not m:
                continue
            # Any text before "USD" on the same row is the tail of the instrument name
            name_suffix = line[:usd_pos].strip()
            # Strip ISIN patterns from name_suffix
            name_suffix = re.sub(r"\(ISIN:.*$", "", name_suffix).strip()

            # Tokens that identify a column-header row — never part of an instrument name
            _HEADER_TOKENS = {
                "Quantity", "ConversionRate", "Openprice", "Currentprice",
                "UnrealizedP/L", "MarketValue", "MarketValue%",
                "%Pricechange", "currency", "rating", "focus",
            }

            # Strings that identify a section-header row (case-insensitive substring match)
            _SECTION_HEADER_SUBSTRINGS = (
                "exchange", "traded products", "sustainability",
                "asset class", "conversion rate", "quantity", "open price",
            )

            # Standalone abbreviation tokens that alone constitute a section header
            _STANDALONE_ABBREV = {"ETF", "ETC", "ETN", "USD"}

            def _is_header_row(row_words: list[str]) -> bool:
                return bool(set(row_words) & _HEADER_TOKENS)

            def _is_section_header_row(row_words: list[str]) -> bool:
                """Return True if this row is a section separator we should not harvest."""
                joined_lower = " ".join(row_words).lower()
                for substr in _SECTION_HEADER_SUBSTRINGS:
                    if substr in joined_lower:
                        return True
                # A row that is ONLY standalone abbreviation tokens
                if row_words and all(w in _STANDALONE_ABBREV for w in row_words):
                    return True
                return False

            # Collect name parts from rows above (up to 3), stopping at another data row
            # or a column-header / section-header row
            name_parts = []
            for j in range(i - 1, max(i - 4, -1), -1):
                prev_line = " ".join(rows[j])
                # Stop if this row looks like another data row
                if rows[j] and rows[j][0] == "USD":
                    break
                prev_usd_pos = prev_line.find("USD ")
                if prev_usd_pos != -1:
                    prev_data = prev_line[prev_usd_pos:]
                    if DATA_RE.match(prev_data):
                        break
                # Stop if this is a column-header row or section-header row
                if _is_header_row(rows[j]) or _is_section_header_row(rows[j]):
                    break
                # Strip ISIN and collect
                prev_clean = re.sub(r"\(ISIN:.*$", "", prev_line).strip()
                # Strip leading ISIN token that sits alone (e.g. "US0231351067)")
                prev_clean = re.sub(r"^[A-Z]{2}\w{10}\)\s*", "", prev_clean).strip()
                if prev_clean:
                    name_parts.insert(0, prev_clean)
                else:
                    break  # empty row — stop collecting

            # Build final name: rows-above parts + same-row suffix
            all_parts = name_parts + ([name_suffix] if name_suffix else [])
            raw_name = " ".join(all_parts).strip()
            # Strip any leading isolated ETF/ETC/ETN/USD tokens that leaked from section headers
            raw_name = re.sub(r"^(?:ETF|ETC|ETN|USD)(?:\s+(?:ETF|ETC|ETN|USD))*\s+", "", raw_name)
            name_clean = clean_instrument_name(raw_name)
            ticker, yf, cls = lookup(name_clean)
            qty = float(m.group("qty").replace(",", ""))
            avg = round(float(m.group("open").replace(",", "")), 4)
            if qty <= 0 or avg <= 0:
                print(f"  [WARN] skipping row with invalid qty={qty} avg={avg} for {name_clean!r}", file=sys.stderr)
                continue
            out.append({
                "tk":    ticker,
                "yf":    yf,
                "name":  name_clean,
                "cls":   cls,
                "qty":   int(qty) if qty.is_integer() else qty,
                "avg":   avg,
                "_statement_ltp": round(float(m.group("cur").replace(",", "")), 4),
                "_statement_upl": round(float(m.group("upl").replace(",", "")), 2),
            })
    return out


def parse_cash(pdf) -> float:
    """Search ALL pages for 'Allaccounts USD <value>' pattern (robust against page reordering)."""
    for page in pdf.pages:
        for r in page_rows(page):
            line = " ".join(r).replace(",", "")
            m = re.search(r"Allaccounts\s+USD\s+(-?\d+\.\d+)", line)
            if m:
                return float(m.group(1))
    print("  [WARN] parse_cash: 'Allaccounts USD' pattern not found on any page", file=sys.stderr)
    return 0.0


def parse_charges(pdf) -> list[dict]:
    """Cost summary page — search all pages for the charges section."""
    label_map = [
        ("Commission",                   "Commission"),
        ("ClientCustodyFee",             "Custody Fee (ongoing)"),
        ("ExchangeFee",                  "Exchange Fee"),
        ("FrenchFinancialTransaction",   "French FTT"),
        ("Externalproductcosts",         "External Product Costs (implicit)"),
    ]
    # Find the page containing "Cost summary" rather than hardcoding page index
    cost_page_rows = []
    for page in pdf.pages:
        text = "".join("".join(r) for r in page_rows(page))
        if "Costsummary" in text or "Commission" in text and "USD" in text:
            cost_page_rows = page_rows(page)
            break
    out = []
    for r in cost_page_rows:
        joined = "".join(r)
        for needle, pretty in label_map:
            if needle in joined:
                m = re.search(r"(-?\d{1,3}(?:,\d{3})*\.\d{2})USD", joined)
                if m:
                    out.append({"type": pretty, "amt": float(m.group(1).replace(",", ""))})
                break
    return out


# ───────────────────────── BUILD ─────────────────────────
def build_cost_json(pdf_path: Path, prev: dict | None) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        summary  = parse_account_summary(pdf)
        timeline = parse_monthly_timeline(pdf)
        pl_rows  = parse_pl_breakdown(pdf)
        holdings = parse_holdings(pdf)
        cash     = parse_cash(pdf)
        charges  = parse_charges(pdf)

    # ── Match P&L rows to positions by TICKER (robust vs truncated names) ──
    #
    # The P&L breakdown (pages 5-7) is per-instrument for the whole period.
    # Instruments that were closed AND reopened (e.g. VOOG, EWY) appear once
    # in the statement with combined costs.  We handle this by:
    #   • If ticker is currently OPEN  → costs go to open position
    #   • If ticker is only CLOSED     → costs go to closed entry
    # This avoids double-counting fees for re-opened positions.

    open_tickers  = {h["tk"] for h in holdings}

    # Preserve per-ticker local fields (buy_date, fx_buy) across reparses.
    # PDF never contains these — they originate from manual entry or fx_buy
    # backfill in market_data.py and must survive PDF reparse.
    prev_open_meta = {p["tk"]: p for p in (prev or {}).get("us", {}).get("open", []) if "tk" in p}

    # user_force_closed: user has decided to write off & close a still-broker-held position
    # (e.g. JDZG — halted, expect zero recovery). Keep these in closed[] across reparses,
    # do NOT re-add to open[] even if PDF lists them.
    prev_closed_all      = (prev or {}).get("us", {}).get("closed", [])
    force_closed_tks     = {p["tk"] for p in prev_closed_all if p.get("user_force_closed")}
    force_closed_entries = [p for p in prev_closed_all if p.get("user_force_closed")]

    # Build ticker → pl_row map. Sum costs if same ticker appears twice.
    pl_by_ticker: dict[str, dict] = {}
    for r in pl_rows:
        tk, _, _ = lookup(r["name"])
        if tk in pl_by_ticker:
            # Same ticker found twice (e.g. split rows): merge costs
            pl_by_ticker[tk]["costs"]   += r["costs"]
            pl_by_ticker[tk]["income"]  += r["income"]
            pl_by_ticker[tk]["pl"]      += r["pl"]
        else:
            pl_by_ticker[tk] = dict(r)

    open_full = []
    for h in holdings:
        if h["tk"].startswith("UNKNOWN"):
            print(f"  [WARN] skipping UNKNOWN ticker in open positions: {h['tk']!r} ({h['name']!r})", file=sys.stderr)
            continue
        if h["tk"] in force_closed_tks:
            print(f"  [INFO] {h['tk']} is user-force-closed (written off) — not re-adding to open[]", file=sys.stderr)
            continue
        plr = pl_by_ticker.get(h["tk"], {})
        entry = {
            "tk":     h["tk"],
            "yf":     h["yf"],
            "name":   h["name"],
            "cls":    h["cls"],
            "qty":    h["qty"],
            "avg":    h["avg"],
            "fees":   round(plr.get("costs", 0.0), 2),
            "income": round(plr.get("income", 0.0), 2),
        }
        prev_p = prev_open_meta.get(h["tk"])
        if prev_p:
            for k in ("buy_date", "fx_buy", "quarantine", "quarantine_reason",
                     "quarantine_date", "lesson", "rule_born",
                     "write_off", "write_off_loss"):
                if prev_p.get(k) is not None:
                    entry[k] = prev_p[k]
        open_full.append(entry)

    closed_full = []
    for tk, r in pl_by_ticker.items():
        if tk in open_tickers:
            # Fees for this ticker already captured in the open position.
            # Before recording a closed tranche, verify there was a real closed trade.
            # If the period P&L is fully explained by unrealised mark-to-market + costs,
            # the position was never closed — skip adding a phantom closed entry.
            stmt_upl = next(
                (h.get("_statement_upl", 0.0) for h in holdings if h["tk"] == tk), 0.0
            )
            realised_est = r["pl"] - stmt_upl - r["costs"]
            if abs(realised_est) < 2.0:
                # No real closed tranche — period P&L fully explained by mark-to-market + costs
                continue
            # Still record the closed tranche's realised P&L (costs=0 to avoid double-count).
            if r["pl"] != 0.0 and not tk.startswith("UNKNOWN"):
                _, yf, cls = lookup(r["name"])
                closed_full.append({
                    "tk":       tk,
                    "name":     r["name"],
                    "cls":      cls,
                    "income":   round(r["income"], 2),
                    "costs":    0.0,                       # realised is ALREADY net of costs (PDF P&L = gross - costs)
                    "_costs_paid": round(r["costs"], 2),   # informational only — do NOT use in total calculation
                    "realised": round(r["pl"], 2),
                    "ret_pct":  round(r["ret_pct"], 2),
                    "_note":    "realised is net of all period costs; costs=0 avoids double-deduction in total",
                })
            continue
        _, yf, cls = lookup(r["name"])
        closed_full.append({
            "tk":       tk,
            "name":     r["name"],
            "cls":      cls,
            "income":   round(r["income"], 2),
            "costs":    round(r["costs"], 2),
            "realised": round(r["pl"], 2),
            "ret_pct":  round(r["ret_pct"], 2),
        })

    if len(open_full) == 0:
        raise ValueError(
            "build_cost_json: no open positions extracted from PDF — "
            "check page layout or TICKER_MAP coverage. Aborting to avoid data loss."
        )

    out = prev or {}
    out["as_of"]      = summary.get("period_to") or out.get("as_of")
    out["fx_inr_usd"] = out.get("fx_inr_usd", 83.5)

    # Merge closed positions: preserve historical records not in current statement period.
    # New statement only covers its own date range — older closed tranches would be lost
    # without this merge. De-duplicate by ticker, preferring new data when both exist.
    # Exception: user_force_closed entries ALWAYS win — they are the user's overlay decision.
    closed_full   = [p for p in closed_full if p["tk"] not in force_closed_tks]
    prev_closed   = prev_closed_all
    new_closed_tks = {p["tk"] for p in closed_full}
    merged_closed = closed_full + [p for p in prev_closed if p["tk"] not in new_closed_tks]

    out["us"] = {
        "broker":                  "US Broker",
        "cash":                    round(cash, 2),
        "cash_infusion_itd":       round(summary["net_deposits"], 2),
        "account_value_statement": round(summary["end_value"], 2),
        "total_pl_statement":      round(summary["total_pl"], 2),
        "monthly":                 timeline,
        "open":                    open_full,
        "closed":                  merged_closed,
        "charges_breakdown":       charges,
    }
    out.setdefault("india", {"open": [], "note": "Awaiting broker statement parser"})
    return out


# ───────────────────────── CLI ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Parse US broker statement → holdings_cost.json")
    ap.add_argument("pdf", help="Path to broker statement PDF")
    ap.add_argument("--output", "-o", default="data/holdings_cost.json")
    args = ap.parse_args()

    pdf = Path(args.pdf).expanduser()
    out_path = Path(args.output).expanduser()
    if not pdf.exists():
        sys.exit(f"file not found: {pdf}")

    prev = json.loads(out_path.read_text()) if out_path.exists() else None
    print(f"Parsing {pdf} …")
    cost = build_cost_json(pdf, prev)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cost, indent=2, ensure_ascii=False))

    us = cost["us"]
    print(f"\nParsed snapshot as of {cost['as_of']}:")
    print(f"  account value      ${us['account_value_statement']:>11,.2f}")
    print(f"  cash               ${us['cash']:>11,.2f}")
    print(f"  cash infusion ITD  ${us['cash_infusion_itd']:>11,.2f}")
    print(f"  total P/L          ${us['total_pl_statement']:>11,.2f}")
    print(f"  open positions     {len(us['open']):>11d}")
    print(f"  closed positions   {len(us['closed']):>11d}")
    print(f"  charges entries    {len(us['charges_breakdown']):>11d}")
    print(f"  monthly periods    {len(us['monthly']['labels']):>11d}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
