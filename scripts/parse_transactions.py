"""
parse_transactions.py — compute accurate per-position fees from broker transaction Excel.

Usage:
  python scripts/parse_transactions.py <path-to-Transactions-*.xlsx>

Algorithm (per ticker):
  1. Sort all trades chronologically (skip corporate-action rows for qty tracking)
  2. Walk forward tracking running position qty
  3. Find last timestamp when running qty crossed through 0 (position fully closed)
  4. Sum fees only for trades AFTER that reset point
  5. That gives fees attributable to the CURRENT open position

Special handling:
  - Corporate actions (stock splits, etc.) are excluded from reset detection
    e.g. VOOG 6:1 split on 2026-04-21: qty goes 3→0→18 via corp action —
    NOT treated as a close/reopen
  - Income (dividends) computed from Bookings sheet for current position period

Outputs:
  - Console diff table
  - Writes corrected fees back to data/holdings_cost.json (open positions only)
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT         = Path(__file__).resolve().parent.parent
COST_BASIS   = ROOT / "data" / "holdings_cost.json"


# ── helpers ──────────────────────────────────────────────────────────────────

def clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Strip non-breaking spaces from column names."""
    df.columns = [c.replace("\xa0", " ").strip() for c in df.columns]
    return df


def extract_sym(raw: str) -> str:
    """'EWY:arcx' → 'EWY'"""
    return str(raw).split(":")[0].upper()


# ── core algorithm ────────────────────────────────────────────────────────────

def current_position_fees(
    trades_df: pd.DataFrame,
    fee_map: dict[int, float],
    sym: str,
) -> tuple[float, date | None]:
    """
    Returns (total_fees_for_current_position, position_open_date).
    fee_map: trade_id → fee (negative numbers)
    """
    rows = trades_df[trades_df["sym"] == sym].sort_values("Adjusted Trade Date")
    if rows.empty:
        return 0.0, None

    # Walk through trades tracking qty; skip corp-action rows for reset detection
    running_qty = 0
    last_reset_idx = -1  # index in `rows` after which current position starts

    for i, (_, row) in enumerate(rows.iterrows()):
        is_corp = pd.notna(row.get("Corporate Action Id"))
        qty = int(row["Traded Quantity"])

        if is_corp:
            # Treat as qty adjustment without triggering a reset
            running_qty += qty
            continue

        running_qty += qty

        if running_qty == 0:
            last_reset_idx = i  # position fully closed here

    # Sum fees for non-corp trades after the last reset
    total_fee = 0.0
    open_date = None
    for i, (_, row) in enumerate(rows.iterrows()):
        if i <= last_reset_idx:
            continue
        is_corp = pd.notna(row.get("Corporate Action Id"))
        if is_corp:
            continue
        trade_id = int(row["Trade ID"])
        fee = fee_map.get(trade_id, 0.0)
        total_fee += fee
        if open_date is None:
            open_date = row["Adjusted Trade Date"]
            if hasattr(open_date, "date"):
                open_date = open_date.date()

    return round(total_fee, 2), open_date


def current_position_income(
    bookings_df: pd.DataFrame,
    sym: str,
    open_date: date | None,
) -> float:
    """Sum dividends received on or after open_date for the symbol."""
    div_types = {"Corporate Actions - Cash Dividends"}
    rows = bookings_df[
        (bookings_df["sym"] == sym)
        & bookings_df["Amount Type"].isin(div_types)
    ]
    if open_date is not None:
        rows = rows[pd.to_datetime(rows["Booking date"]).dt.date >= open_date]
    return round(float(rows["Booked Amount"].sum()), 2)


# ── main ─────────────────────────────────────────────────────────────────────

def main(xlsx_path: str) -> int:
    path = Path(xlsx_path)
    if not path.exists():
        print(f"ERROR  file not found: {xlsx_path}", file=sys.stderr)
        return 1

    xl = pd.read_excel(path, sheet_name=None)
    trades_raw   = clean_cols(xl["Trades"])
    txn_raw      = clean_cols(xl["Transactions"])
    bookings_raw = clean_cols(xl["Bookings"])

    # Parse symbol from "SYM:exchange"
    trades_raw["sym"]   = trades_raw["Instrument Symbol"].apply(extract_sym)
    txn_raw["sym"]      = txn_raw["Instrument Symbol"].apply(extract_sym)
    bookings_raw["sym"] = bookings_raw["Instrument Symbol"].apply(extract_sym)

    # Build fee map: trade_id → fee (from Transactions "Total cost")
    fee_map: dict[int, float] = {}
    for _, row in txn_raw.iterrows():
        tid = row.get("Trade ID")
        if pd.isna(tid):
            continue
        fee = row.get("Total cost", 0.0)
        if pd.isna(fee):
            fee = 0.0
        fee_map[int(tid)] = float(fee)

    # Load holdings
    if not COST_BASIS.exists():
        print(f"ERROR  {COST_BASIS} not found", file=sys.stderr)
        return 1
    cost = json.loads(COST_BASIS.read_text())
    open_pos = cost.get("us", {}).get("open", [])

    print(f"\n{'TICKER':<8} {'OLD FEE':>9} {'NEW FEE':>9} {'DIFF':>8}  {'OLD INC':>8} {'NEW INC':>8}  OPEN DATE")
    print("-" * 80)

    changed_fees   = 0
    changed_income = 0
    # Note: holdings with no transactions (e.g. transferred from another broker)
    # will have new_fee=0 which is correct — fee_map only covers transactions in this file.

    for pos in open_pos:
        tk  = pos["tk"]
        sym = pos.get("yf", tk).split(".")[0]  # strip ".NS" etc.

        try:
            new_fee, open_date = current_position_fees(trades_raw, fee_map, sym)
            new_inc = current_position_income(bookings_raw, sym, open_date)
        except Exception as exc:
            print(f"  WARN  {tk} ({sym}): error computing fees/income — skipping: {exc}", file=sys.stderr)
            continue

        old_fee = pos.get("fees", 0.0) or 0.0
        old_inc = pos.get("income", 0.0) or 0.0

        fee_diff = new_fee - old_fee
        inc_diff = new_inc - old_inc

        flag_fee = " ❌" if abs(fee_diff) >= 0.01 else ""
        flag_inc = " ⚠" if abs(inc_diff) >= 0.01 else ""

        open_str = str(open_date) if open_date else "—(no reset)"
        print(
            f"{tk:<8} {old_fee:>9.2f} {new_fee:>9.2f} {fee_diff:>+8.2f}{flag_fee}"
            f"  {old_inc:>8.2f} {new_inc:>8.2f}{flag_inc}  {open_str}"
        )

        if abs(fee_diff) >= 0.01:
            pos["fees"] = new_fee
            changed_fees += 1

        if abs(inc_diff) >= 0.01:
            pos["income"] = new_inc
            changed_income += 1

    print("-" * 80)
    print(f"\nFee fixes:    {changed_fees}")
    print(f"Income fixes: {changed_income}")

    if changed_fees + changed_income > 0:
        COST_BASIS.write_text(json.dumps(cost, indent=2))
        print(f"\n✓ Updated {COST_BASIS.relative_to(ROOT)}")
    else:
        print("\n✓ No changes needed — holdings_cost.json up to date")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/parse_transactions.py <Transactions-*.xlsx>")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
