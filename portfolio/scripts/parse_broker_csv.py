#!/usr/bin/env python3
"""
parse_broker_csv.py — merge an Interactive Brokers Flex-Query "Activity Statement"
CSV export (raw multi-section format, e.g. [US-BROKER-ACCT]_20260101_20260818.csv) into:

    data/transactions_us.json   (per-trade ledger + cash_moves)
    data/holdings_cost.json     (us.open / us.closed authoritative positions)

This is a companion to parse_broker_pdf.py / build_transactions_us.py /
patch_fees_from_xlsx.py for the older PDF+xlsx statement pair — it follows the
same schema + fee-attribution conventions but reads directly from the raw IBKR
CSV, which is a single file with multiple labeled sections (first column =
section name, second column = "Header" | "Data" | "SubTotal" | "Total").

Conventions replicated from the PDF/xlsx pipeline (see CLAUDE.md):
  • us.open[]  is a full snapshot from the statement's "Open Positions"
    section — the CSV *is* the source of truth for currently-open qty/avg.
  • us.closed[] entries for tickers with realised P/L THIS statement period
    are added/updated; tickers realised in EARLIER periods (not covered by
    this CSV) are left untouched — multiple closed tranches per ticker over
    time are expected and preserved.
  • Fee attribution: a trade's Code column carries "O" (Opening Trade) or "C"
    (Closing Trade). Commission is summed per ticker into open_comm / close_comm.
      - ticker currently OPEN            → open.fees = open_comm[tk]
      - ticker OPEN *and* realised this period (mixed, e.g. DLTH/RKLB/MU/RXT)
                                          → closed.costs = 0.0 (avoid double
                                            counting realised, which is already
                                            net of costs), closed._costs_paid
                                            = close_comm[tk] (informational)
      - ticker CLOSED-ONLY this period   → closed.costs = closed._costs_paid
                                            = open_comm[tk] + close_comm[tk]
    This mirrors patch_fees_from_xlsx.py's split_commissions() exactly, and
    the reconciliation identity is:
        sum(open.fees) + sum(closed._costs_paid for this period's tickers)
            == CSV Trades "Total" row Comm/Fee
  • VOOG: never force-closed; a VOOG entry is only added/updated in closed[]
    if the CSV's Realized & Unrealized Performance Summary shows a nonzero
    Realized Total for VOOG this period. (The historical VOOG closed lot from
    the pre-account-open 6:1 split is a *different*, already-existing closed
    entry and is left completely alone.)

Usage:
    python3 scripts/parse_broker_csv.py <path-to-activity-statement.csv>
        [--transactions data/transactions_us.json]
        [--cost-file data/holdings_cost.json]
        [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TXNS = ROOT / "data" / "transactions_us.json"
DEFAULT_COST = ROOT / "data" / "holdings_cost.json"


# ───────────────────────── CSV LOADING ─────────────────────────
def load_sections(path: Path) -> dict[str, list[list[str]]]:
    """Group every row of the flex-query CSV by its section name (col 0)."""
    sections: dict[str, list[list[str]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row or not row[0]:
                continue
            sections[row[0]].append(row)
    return sections


def _f(s: str | None, default: float = 0.0) -> float:
    if s is None or s == "":
        return default
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return default


def title_name(desc: str) -> str:
    """Best-effort title-case for IBKR's ALL-CAPS instrument descriptions.
    Keeps known acronyms upper-cased, title-cases everything else."""
    KEEP_UPPER = {"etf", "etc", "etn", "adr", "reit", "llc", "lp", "plc", "nv",
                  "sa", "inc", "cl", "spdr", "s&p"}
    out = []
    for w in desc.strip().split():
        bare = w.strip(".,-")
        if bare.lower() in KEEP_UPPER:
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out)


# ───────────────────────── SECTION PARSERS ─────────────────────────
def parse_financial_instrument_info(sections) -> dict[str, dict]:
    """Symbol → {name, isin, exchange, type}"""
    out = {}
    for row in sections.get("Financial Instrument Information", []):
        if row[1] != "Data":
            continue
        # Section,Data,AssetCategory,Symbol,Description,Conid,SecurityID,Underlying,ListingExch,Multiplier,Type,Code
        _, _, asset_cat, sym, desc, conid, isin, underlying, exch, mult, typ = row[:11]
        out[sym] = {
            "name": title_name(desc),
            "isin": isin or None,
            "exchange": exch or None,
            "type": typ or None,
        }
    return out


def parse_open_positions(sections) -> list[dict]:
    out = []
    for row in sections.get("Open Positions", []):
        if row[1] != "Data" or row[2] != "Summary":
            continue
        # Section,Data,Summary,AssetCategory,Currency,Symbol,Quantity,Mult,CostPrice,CostBasis,ClosePrice,Value,UnrealizedP/L,Code
        _, _, _, asset_cat, ccy, sym, qty, mult, cost_price, cost_basis, close_price, value, upl = row[:13]
        out.append({
            "tk": sym,
            "qty": _f(qty),
            "cost_price": _f(cost_price),
            "cost_basis": _f(cost_basis),
            "close_price": _f(close_price),
            "value": _f(value),
            "unrealized_pl": _f(upl),
        })
    return out


def parse_trades(sections) -> list[dict]:
    out = []
    for row in sections.get("Trades", []):
        if row[1] != "Data" or row[2] != "Order":
            continue
        # Section,Data,Order,AssetCategory,Currency,Symbol,Date/Time,Quantity,T.Price,C.Price,Proceeds,Comm/Fee,Basis,RealizedP/L,MTMP/L,Code
        _, _, _, asset_cat, ccy, sym, dt, qty, tprice, cprice, proceeds, comm, basis, rpl, mtm, code = (row + [""] * 16)[:16]
        codes = set(c.strip() for c in (code or "").split(";") if c.strip())
        date_str, time_str = (dt.split(",", 1) + [""])[:2]
        out.append({
            "tk": sym,
            "ccy": ccy,
            "date": date_str.strip(),
            "time": time_str.strip(),
            "qty": _f(qty),
            "price": _f(tprice),
            "proceeds": _f(proceeds),
            "commission": _f(comm),
            "basis": _f(basis),
            "realized_pl": _f(rpl),
            "codes": codes,
        })
    return out


def parse_realized_unrealized(sections) -> dict[str, dict]:
    """Symbol → {realized_total}. Skips Total / Total (All Assets) / Forex rows."""
    out = {}
    for row in sections.get("Realized & Unrealized Performance Summary", []):
        if row[1] != "Data":
            continue
        asset_cat = row[2]
        sym = row[3] if len(row) > 3 else ""
        if asset_cat in ("Total", "Total (All Assets)") or asset_cat == "Forex" or not sym:
            continue
        # cols: AssetCategory,Symbol,CostAdj,RSTProfit,RSTLoss,RLTProfit,RLTLoss,RealizedTotal,...
        realized_total = _f(row[9]) if len(row) > 9 else 0.0
        out[sym] = {"realized_total": realized_total}
    return out


def parse_deposits(sections) -> list[dict]:
    out = []
    for row in sections.get("Deposits & Withdrawals", []):
        if row[1] != "Data" or row[2] == "Total":
            continue
        # Section,Data,Currency,SettleDate,Description,Amount
        _, _, ccy, date, desc, amt = (row + [""] * 6)[:6]
        out.append({"date": date, "description": desc, "amount": _f(amt), "ccy": ccy})
    return out


def parse_interest(sections) -> list[dict]:
    out = []
    for row in sections.get("Interest", []):
        if row[1] != "Data" or row[2] == "Total":
            continue
        _, _, ccy, date, desc, amt = (row + [""] * 6)[:6]
        out.append({"date": date, "description": desc, "amount": _f(amt), "ccy": ccy})
    return out


def parse_corporate_actions(sections) -> list[dict]:
    out = []
    for row in sections.get("Corporate Actions", []):
        if row[1] != "Data" or row[2] == "Total":
            continue
        # Section,Data,AssetCategory,Currency,ReportDate,Date/Time,Description,Quantity,Proceeds,Value,RealizedP/L,Code
        row = (row + [""] * 12)[:12]
        _, _, asset_cat, ccy, report_date, dt, desc, qty, proceeds, value, rpl, code = row
        date_str = dt.split(",", 1)[0].strip()
        out.append({"date": date_str or report_date, "description": desc, "quantity": _f(qty), "proceeds": _f(proceeds)})
    return out


def parse_totals(sections) -> dict:
    cash = 0.0
    for row in sections.get("Cash Report", []):
        if row[1] == "Data" and row[2] == "Ending Cash":
            cash = _f(row[4])
    nav_total = 0.0
    for row in sections.get("Net Asset Value", []):
        if row[1] == "Data" and row[2] == "Total":
            nav_total = _f(row[6])
    deposits_itd = 0.0
    for row in sections.get("Change in NAV", []):
        if row[1] == "Data" and row[2] == "Deposits & Withdrawals":
            deposits_itd = _f(row[3])
    total_pl = 0.0
    for row in sections.get("Total P/L for Statement Period", []):
        nums = [_f(x) for x in row[1:] if x not in ("", None)]
        if nums:
            total_pl = nums[-1]
    period_to = None
    for row in sections.get("Statement", []):
        if row[1] == "Data" and row[2] == "Period":
            # "January 1, 2026 - August 18, 2026"
            try:
                end = row[3].split(" - ")[1].strip()
                period_to = datetime.strptime(end, "%B %d, %Y").date().isoformat()
            except Exception:
                pass
    return {
        "cash": round(cash, 2),
        "account_value_statement": round(nav_total, 2),
        "cash_infusion_itd": round(deposits_itd, 2),
        "total_pl_statement": round(total_pl, 2),
        "as_of_iso": period_to,
    }


# ───────────────────────── FEE ATTRIBUTION ─────────────────────────
def split_commissions(trades: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    open_comm: dict[str, float] = defaultdict(float)
    close_comm: dict[str, float] = defaultdict(float)
    for t in trades:
        tk = t["tk"]
        if "C" in t["codes"]:
            close_comm[tk] += t["commission"]
        else:
            # "O" or unmarked (defensive) → treat as open-side
            open_comm[tk] += t["commission"]
    return dict(open_comm), dict(close_comm)


# ───────────────────────── TRANSACTIONS MERGE ─────────────────────────
def build_new_trade_rows(trades: list[dict], instr: dict[str, dict]) -> list[dict]:
    rows = []
    for t in trades:
        tk = t["tk"]
        meta = instr.get(tk, {})
        side = "buy" if t["qty"] > 0 else "sell"
        qty_abs = abs(t["qty"])
        gross = round(qty_abs * t["price"], 4)
        commission = round(t["commission"], 4)
        net = round(t["proceeds"] + t["commission"], 4)
        open_close = "close" if "C" in t["codes"] else ("open" if "O" in t["codes"] else None)
        realised_pl = round(t["realized_pl"], 4) if side == "sell" else None
        typ = (meta.get("type") or "").upper()
        asset_type = "ETF" if typ in ("ETF", "ETC", "ETN") else "Stock"
        date_compact = t["date"].replace("-", "")
        time_compact = t["time"].replace(":", "")
        qty_tag = str(t["qty"]).replace(".", "p").replace("-", "n")
        trade_id = f"IBKR-{tk}-{date_compact}{time_compact}-{qty_tag}"
        rows.append({
            "trade_id":     trade_id,
            "date":         t["date"],
            "tk":           tk,
            "side":         side,
            "qty":          qty_abs,
            "price":        t["price"],
            "gross":        gross,
            "commission":   commission,
            "ftt":          0.0,
            "exchange_fee": 0.0,
            "net":          net,
            "open_close":   open_close,
            "order_type":   None,
            "realised_pl":  realised_pl,
            "name":         meta.get("name"),
            "isin":         meta.get("isin"),
            "ccy":          t["ccy"],
            "asset_type":   asset_type,
            "exchange":     meta.get("exchange"),
        })
    return rows


def merge_transactions(txns: dict, trades: list[dict], instr: dict[str, dict],
                        deposits: list[dict], interest: list[dict],
                        corp_actions: list[dict]) -> dict:
    existing_trades = txns.setdefault("trades", [])

    def _key(r):
        qty = r.get("qty")
        price = r.get("price")
        return (
            r.get("tk"), r.get("date"),
            round(qty, 4) if qty is not None else None,
            round(price, 4) if price is not None else None,
        )

    existing_keys = {_key(r) for r in existing_trades}

    new_rows = build_new_trade_rows(trades, instr)
    added_trades = []
    for r in new_rows:
        key = _key(r)
        if key in existing_keys:
            continue
        existing_trades.append(r)
        existing_keys.add(key)
        added_trades.append(r)

    existing_cash = txns.setdefault("cash_moves", [])
    existing_cash_keys = {
        (c.get("type"), c.get("date"), round(c.get("amount", 0) or 0, 4), c.get("tk"))
        for c in existing_cash
    }
    added_cash = []

    def add_cash(entry):
        key = (entry["type"], entry["date"], round(entry["amount"], 4), entry.get("tk"))
        if key in existing_cash_keys:
            return
        existing_cash.append(entry)
        existing_cash_keys.add(key)
        added_cash.append(entry)

    for d in deposits:
        add_cash({
            "date": d["date"], "type": "Cash Transfer", "event": "Deposit",
            "amount": round(d["amount"], 4), "ccy": d["ccy"], "tk": None, "name": None,
        })
    for i in interest:
        add_cash({
            "date": i["date"], "type": "Client Interest", "event": None,
            "amount": round(i["amount"], 4), "ccy": i["ccy"], "tk": None, "name": None,
        })
    for ca in corp_actions:
        add_cash({
            "date": ca["date"], "type": "Corporate Action - Stock Split", "event": ca["description"],
            "amount": round(ca["proceeds"], 4), "ccy": "USD", "tk": None, "name": None,
        })

    # ── Sort ──
    existing_trades.sort(key=lambda r: (r["date"] or "", r["trade_id"]), reverse=True)
    existing_cash.sort(key=lambda r: (r["date"] or ""), reverse=True)

    # ── Recompute totals over the full merged set ──
    buys  = [t for t in existing_trades if t["side"] == "buy"]
    sells = [t for t in existing_trades if t["side"] == "sell"]
    totals = {
        "trades_count":      len(existing_trades),
        "buys_count":        len(buys),
        "sells_count":       len(sells),
        "gross_buys":        round(sum(t["gross"] or 0 for t in buys), 2),
        "gross_sells":       round(sum(t["gross"] or 0 for t in sells), 2),
        "commission_total":  round(sum(t["commission"] for t in existing_trades), 2),
        "realised_pl_total": round(sum(t["realised_pl"] or 0 for t in sells), 2),
        "deposits":          round(sum(c["amount"] for c in existing_cash if c["type"] == "Cash Transfer" and c["amount"] > 0), 2),
        "withdrawals":       round(sum(c["amount"] for c in existing_cash if c["type"] == "Cash Transfer" and c["amount"] < 0), 2),
        "dividends":         round(sum(c["amount"] for c in existing_cash if "Dividend" in (c["type"] or "")), 2),
        "interest":          round(sum(c["amount"] for c in existing_cash if "Interest" in (c["type"] or "")), 2),
        "custody_fees":      round(sum(c["amount"] for c in existing_cash if "Custody" in (c["type"] or "")), 2),
        "withholding_tax":   round(sum(c["amount"] for c in existing_cash if "Withholding" in (c["type"] or "")), 2),
    }
    txns["totals"] = totals
    period_start = min((t["date"] for t in existing_trades if t["date"]), default=None)
    period_end   = max((t["date"] for t in existing_trades if t["date"]), default=None)
    txns["period_from"] = period_start
    txns["period_to"]   = period_end
    txns["generated"]   = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    txns["source"] = (txns.get("source") or "US broker transactions xlsx") + \
        " + IBKR CSV flex-query (activity statement) through " + (period_end or "")

    return txns, added_trades, added_cash


# ───────────────────────── HOLDINGS_COST MERGE ─────────────────────────
def merge_holdings_cost(cost: dict, open_positions: list[dict], realized: dict[str, dict],
                         trades: list[dict], instr: dict[str, dict], stmt_totals: dict) -> tuple[dict, dict]:
    us = cost.setdefault("us", {})
    open_comm, close_comm = split_commissions(trades)
    open_tickers = {p["tk"] for p in open_positions}

    prev_open = {p["tk"]: p for p in us.get("open", []) if p.get("tk")}
    force_closed_tks = {p["tk"] for p in us.get("closed", []) if p.get("user_force_closed")}

    diff = {"open": [], "closed_new": [], "closed_updated": [], "phantom_removed": []}

    # ── Rebuild us.open[] from CSV Open Positions (authoritative snapshot) ──
    new_open = []
    for p in open_positions:
        tk = p["tk"]
        if tk in force_closed_tks:
            continue
        meta = instr.get(tk, {})
        typ = (meta.get("type") or "").upper()
        cls = "ETF" if typ in ("ETF", "ETC", "ETN") else "Stock"
        entry = {
            "tk":     tk,
            "yf":     tk,
            "name":   meta.get("name", tk),
            "cls":    cls,
            "qty":    int(p["qty"]) if float(p["qty"]).is_integer() else p["qty"],
            "avg":    round(p["cost_price"], 6),
            "fees":   round(open_comm.get(tk, 0.0), 4),
            "income": 0.0,
        }
        prevp = prev_open.get(tk)
        if prevp:
            for k in ("buy_date", "fx_buy", "quarantine", "quarantine_reason",
                      "quarantine_date", "lesson", "rule_born",
                      "write_off", "write_off_loss"):
                if prevp.get(k) is not None:
                    entry[k] = prevp[k]
            old_fees = prevp.get("fees")
            if old_fees is not None and abs(old_fees - entry["fees"]) > 0.005:
                diff["open"].append({"tk": tk, "old_fees": old_fees, "new_fees": entry["fees"]})
            elif old_fees is None:
                diff["open"].append({"tk": tk, "old_fees": None, "new_fees": entry["fees"]})
        new_open.append(entry)
    us["open"] = new_open

    # ── us.closed[]: update-in-place or append, for tickers realised THIS period ──
    prev_closed = us.get("closed", [])
    period_tickers = {tk for tk, r in realized.items() if abs(r["realized_total"]) > 0.005}

    # cost basis of the *closing* trades per ticker (for ret_pct)
    close_basis = defaultdict(float)
    for t in trades:
        if "C" in t["codes"]:
            close_basis[t["tk"]] += abs(t["basis"])

    handled_prev_idx = set()
    for tk in sorted(period_tickers):
        r = realized[tk]
        realised_val = round(r["realized_total"], 2)
        meta = instr.get(tk, {})
        typ = (meta.get("type") or "").upper()
        cls = "ETF" if typ in ("ETF", "ETC", "ETN") else "Stock"

        if tk in open_tickers:
            costs = 0.0
            costs_paid = round(close_comm.get(tk, 0.0), 4)
        else:
            total_comm = open_comm.get(tk, 0.0) + close_comm.get(tk, 0.0)
            costs = round(total_comm, 4)
            costs_paid = round(total_comm, 4)

        basis = close_basis.get(tk, 0.0)
        ret_pct = round(realised_val / basis * 100, 2) if basis else 0.0

        # find an existing entry for the SAME ticker + SAME realised value (this period's prior attempt)
        match_idx = None
        for i, p in enumerate(prev_closed):
            if i in handled_prev_idx:
                continue
            if p.get("tk") == tk and abs(round(p.get("realised", 0.0), 2) - realised_val) < 0.02:
                match_idx = i
                break

        if match_idx is not None:
            entry = prev_closed[match_idx]
            entry.update({
                "name": meta.get("name", entry.get("name", tk)),
                "cls": cls,
                "income": 0.0,
                "costs": costs,
                "_costs_paid": costs_paid,
                "realised": realised_val,
                "ret_pct": ret_pct,
            })
            handled_prev_idx.add(match_idx)
            diff["closed_updated"].append(tk)
        else:
            prev_closed.append({
                "tk": tk,
                "name": meta.get("name", tk),
                "cls": cls,
                "income": 0.0,
                "costs": costs,
                "_costs_paid": costs_paid,
                "realised": realised_val,
                "ret_pct": ret_pct,
            })
            diff["closed_new"].append(tk)

    # ── Phantom check (scoped to this statement's own tickers) ──
    # Any closed entry whose ticker is currently open AND has no 'C'-coded
    # trade in this CSV's Trades section is a phantom for *this* parse.
    tickers_with_close_trade = {t["tk"] for t in trades if "C" in t["codes"]}
    kept = []
    for p in prev_closed:
        tk = p.get("tk")
        if tk in open_tickers and tk not in tickers_with_close_trade and tk in instr and tk not in force_closed_tks:
            # only remove if this looks like it came from *this* CSV's own tickers
            # (i.e. we just touched it above) — never touch untouched history.
            if tk in period_tickers:
                diff["phantom_removed"].append(tk)
                continue
        kept.append(p)
    us["closed"] = kept

    # ── Statement-level fields ──
    us["cash"] = stmt_totals["cash"]
    us["cash_infusion_itd"] = stmt_totals["cash_infusion_itd"]
    us["account_value_statement"] = stmt_totals["account_value_statement"]
    us["total_pl_statement"] = stmt_totals["total_pl_statement"]
    us.setdefault("broker", "US Broker (IBKR)")
    cost["as_of"] = stmt_totals["as_of_iso"] or cost.get("as_of")

    return cost, diff


# ───────────────────────── CLI ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", help="Path to IBKR flex-query activity statement CSV")
    ap.add_argument("--transactions", default=str(DEFAULT_TXNS))
    ap.add_argument("--cost-file", default=str(DEFAULT_COST))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    csv_path = Path(args.csv_path).expanduser()
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    print(f"Reading {csv_path.name} …")
    sections = load_sections(csv_path)

    instr = parse_financial_instrument_info(sections)
    open_positions = parse_open_positions(sections)
    trades = parse_trades(sections)
    realized = parse_realized_unrealized(sections)
    deposits = parse_deposits(sections)
    interest = parse_interest(sections)
    corp_actions = parse_corporate_actions(sections)
    stmt_totals = parse_totals(sections)

    print(f"  instruments:     {len(instr)}")
    print(f"  open positions:  {len(open_positions)}")
    print(f"  trade rows:      {len(trades)}")
    print(f"  realized rows:   {sum(1 for r in realized.values() if abs(r['realized_total']) > 0.005)} nonzero / {len(realized)} total")
    print(f"  deposits:        {len(deposits)}")
    print(f"  interest rows:   {len(interest)}")
    print(f"  corp actions:    {len(corp_actions)}")
    print(f"  statement cash ${stmt_totals['cash']:,.2f}  account_value ${stmt_totals['account_value_statement']:,.2f}  "
          f"deposits_itd ${stmt_totals['cash_infusion_itd']:,.2f}")

    txns_path = Path(args.transactions).expanduser()
    cost_path = Path(args.cost_file).expanduser()
    txns = json.loads(txns_path.read_text()) if txns_path.exists() else {"trades": [], "cash_moves": []}
    cost = json.loads(cost_path.read_text()) if cost_path.exists() else {}

    txns, added_trades, added_cash = merge_transactions(txns, trades, instr, deposits, interest, corp_actions)
    cost, diff = merge_holdings_cost(cost, open_positions, realized, trades, instr, stmt_totals)

    print(f"\n── transactions_us.json ──")
    print(f"  new trades added:     {len(added_trades)}")
    print(f"  new cash_moves added: {len(added_cash)}")
    print(f"  total trades now:     {len(txns['trades'])}")
    print(f"  total cash_moves now: {len(txns['cash_moves'])}")
    print(f"  totals: {json.dumps(txns['totals'], indent=2)}")

    print(f"\n── holdings_cost.json (us) ──")
    print(f"  open positions rebuilt: {len(cost['us']['open'])}")
    print(f"  open fee diffs:         {len(diff['open'])}")
    for d in diff["open"]:
        print(f"    {d['tk']:6s} fees {d['old_fees']} -> {d['new_fees']}")
    print(f"  closed entries updated: {diff['closed_updated']}")
    print(f"  closed entries added:   {diff['closed_new']}")
    print(f"  phantom removed:        {diff['phantom_removed']}")
    print(f"  total closed entries:   {len(cost['us']['closed'])}")

    if args.dry_run:
        print("\n(dry-run — files NOT written)")
        return 0

    txns_path.write_text(json.dumps(txns, indent=2, ensure_ascii=False))
    cost_path.write_text(json.dumps(cost, indent=2, ensure_ascii=False))
    print(f"\nwrote {txns_path}")
    print(f"wrote {cost_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
