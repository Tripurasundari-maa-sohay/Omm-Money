"""
parse_india_excel.py — parse consolidated India broker Excel and merge into holdings_cost.json.

Usage:
    python3 scripts/parse_india_excel.py <path-to-excel> [--dry-run]

What it does:
  - Reads All Transactions + Upstox sheets
  - Deduplicates positions by ISIN
  - Sets india.cash_infusion_itd = gross buy total (₹)
  - Populates india.closed[] with realized-P&L entries
  - Preserves india.open[] exactly as-is (never modified here)

The PDF stays local — only holdings_cost.json and this script go to GitHub.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

# ── PATHS ────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent.parent
COST_FILE  = ROOT / "data" / "holdings_cost.json"

# ── NSE TICKER MAP ────────────────────────────────────────────────────────────
# Maps scrip-name fragments → (nse_ticker, yf_symbol, asset_type)
# Key = scrip name lower-cased, spaces/special chars stripped.
# yf_symbol uses ".NS" suffix; ETFs from BSE use ".BO".
TICKER_MAP: dict[str, tuple[str, str, str]] = {
    # ── Actively traded / closed positions ──────────────────────────────────
    "actionconst":                   ("ACONS",       "ACONS.NS",       "Stock"),
    "avpinfraconlimited":            ("AVPINFRA",    "AVPINFRA.NS",    "Stock"),
    "bharatelectronicslimited":      ("BEL",         "BEL.NS",         "Stock"),
    "bluejethealthcareltd":          ("BLUEJETHC",   "BLUEJETHC.NS",   "Stock"),
    "bhartihexacomlimited":          ("BHARTIHEXA",  "BHARTIHEXA.NS",  "Stock"),
    "bhartihexa":                    ("BHARTIHEXA",  "BHARTIHEXA.NS",  "Stock"),
    "carraro":                       ("CARRARO",     "CARRARO.NS",     "Stock"),
    "carraroindialtd":               ("CARRARO",     "CARRARO.NS",     "Stock"),
    "carraroindialimited":           ("CARRARO",     "CARRARO.NS",     "Stock"),
    "cclproduts":                    ("CCLPRODUCT",  "CCLPRODUCT.NS",  "Stock"),
    "cclproducts":                   ("CCLPRODUCT",  "CCLPRODUCT.NS",  "Stock"),
    "ceigallindialtd":               ("CEIGALL",     "CEIGALL.NS",     "Stock"),
    "ceigallindia":                  ("CEIGALL",     "CEIGALL.NS",     "Stock"),
    "coforgelimited":                ("COFORGE",     "COFORGE.NS",     "Stock"),
    "crizac":                        ("CRIZAC",      "CRIZAC.NS",      "Stock"),
    "davangere":                     ("DAVANGERE",   "DAVANGERE.NS",   "Stock"),
    "dhanbank":                      ("DHANBANK",    "DHANBANK.NS",    "Stock"),
    "eidparry":                      ("EIDPARRY",    "EIDPARRY.NS",    "Stock"),
    "exicomtelesystemsltd":          ("EXICOMTELE",  "EXICOMTELE.NS",  "Stock"),
    "exicomtele":                    ("EXICOMTELE",  "EXICOMTELE.NS",  "Stock"),
    "glenmarkpharmaceuticalslimited":("GLENMARK",    "GLENMARK.NS",    "Stock"),
    "gmrpowerandurbaninfralimited":  ("GMRPOW",      "GMRPOW.NS",      "Stock"),
    "godigit":                       ("GODIGIT",     "GODIGIT.NS",     "Stock"),
    "godigitgeneralinslimited":      ("GODIGIT",     "GODIGIT.NS",     "Stock"),
    "godigitgeneralinslimited":      ("GODIGIT",     "GODIGIT.NS",     "Stock"),
    "godrejagrovet":                 ("GODREJAGRO",  "GODREJAGRO.NS",  "Stock"),
    "godrejagro":                    ("GODREJAGRO",  "GODREJAGRO.NS",  "Stock"),
    "goldbees":                      ("GOLDBEES",    "GOLDBEES.BO",    "ETF"),
    "gravitaindia":                  ("GRAVITA",     "GRAVITA.NS",     "Stock"),
    "gravitaindialtd":               ("GRAVITA",     "GRAVITA.NS",     "Stock"),
    "hal":                           ("HAL",         "HAL.NS",         "Stock"),
    "hatwaycab":                     ("HATHWAY",     "HATHWAY.NS",     "Stock"),
    "hathawaycab":                   ("HATHWAY",     "HATHWAY.NS",     "Stock"),
    "hathwaycab":                    ("HATHWAY",     "HATHWAY.NS",     "Stock"),
    "hatwaycabltd":                  ("HATHWAY",     "HATHWAY.NS",     "Stock"),
    "hathaycab":                     ("HATHWAY",     "HATHWAY.NS",     "Stock"),
    "hdfcbank":                      ("HDFCBANK",    "HDFCBANK.NS",    "Stock"),
    "happiestmindstechnologies":     ("HAPPSTMNDS",  "HAPPSTMNDS.NS",  "Stock"),
    "happiestminds":                 ("HAPPSTMNDS",  "HAPPSTMNDS.NS",  "Stock"),
    "icicibanklimited":              ("ICICIBANK",   "ICICIBANK.NS",   "Stock"),
    "idea":                          ("IDEA",        "IDEA.NS",        "Stock"),
    "iiflinance":                    ("IIFLFINANCE", "IIFL.NS",        "Stock"),
    "iiflinancelimited":             ("IIFLFINANCE", "IIFL.NS",        "Stock"),
    "iiflfinancelimited":            ("IIFLFINANCE", "IIFL.NS",        "Stock"),
    "indianovers":                   ("INDIANB",     "INDIANB.NS",     "Stock"),
    "indusindbk":                    ("INDUSINDBK",  "INDUSINDBK.NS",  "Stock"),
    "indusindbankltd":               ("INDUSINDBK",  "INDUSINDBK.NS",  "Stock"),
    "indusindbankliimited":          ("INDUSINDBK",  "INDUSINDBK.NS",  "Stock"),
    "indusind":                      ("INDUSINDBK",  "INDUSINDBK.NS",  "Stock"),
    "jiofinancialserviceslimited":   ("JIOFIN",      "JIOFIN.NS",      "Stock"),
    "jiofinancial":                  ("JIOFIN",      "JIOFIN.NS",      "Stock"),
    "jppower":                       ("JPPOWER",     "JPPOWER.NS",     "Stock"),
    "jubilantfoodworkslimited":      ("JUBLFOOD",    "JUBLFOOD.NS",    "Stock"),
    "jupiterwagonslimited":          ("JUPITERWAG",  "JUPITERWAG.NS",  "Stock"),
    "kecintern":                     ("KECL",        "KECL.NS",        "Stock"),
    "kecinternational":              ("KECL",        "KECL.NS",        "Stock"),
    "larsentoubro":                  ("LT",          "LT.NS",          "Stock"),
    "letravenuetechnologyl":         ("IXIGO",       "IXIGO.NS",       "Stock"),
    "letravenuestechnology":         ("IXIGO",       "IXIGO.NS",       "Stock"),
    "lgeindia":                      ("LGEINDIA",    "LGEINDIA.NS",    "Stock"),
    "netweb":                        ("NETWEB",      "NETWEB.NS",      "Stock"),
    "netwebtechnologies":            ("NETWEB",      "NETWEB.NS",      "Stock"),
    "netwebtechnologiesindialtd":    ("NETWEB",      "NETWEB.NS",      "Stock"),
    "nlcindia":                      ("NLCINDIA",    "NLCINDIA.NS",    "Stock"),
    "nlcindialtd":                   ("NLCINDIA",    "NLCINDIA.NS",    "Stock"),
    "nlcindialimited":               ("NLCINDIA",    "NLCINDIA.NS",    "Stock"),
    "ntpclimited":                   ("NTPC",        "NTPC.NS",        "Stock"),
    "ntpcltd":                       ("NTPC",        "NTPC.NS",        "Stock"),
    "olaelectricmobilitylimited":    ("OLAELEC",     "OLAELEC.NS",     "Stock"),
    "olaelectric":                   ("OLAELEC",     "OLAELEC.NS",     "Stock"),
    "olectragreentech":              ("OLECTRA",     "OLECTRA.NS",     "Stock"),
    "olectrahgreentechlimited":      ("OLECTRA",     "OLECTRA.NS",     "Stock"),
    "paramountcommunicationslimited":("PARAMCOMM",   "PARAMCOMM.NS",   "Stock"),
    "paramountcomm":                 ("PARAMCOMM",   "PARAMCOMM.NS",   "Stock"),
    "pcjewellerlimited":             ("PCJEWELLER",  "PCJEWELLER.NS",  "Stock"),
    "reliance":                      ("RELIANCE",    "RELIANCE.NS",    "Stock"),
    "relianceindustrieslimited":     ("RELIANCE",    "RELIANCE.NS",    "Stock"),
    "rvnl":                          ("RVNL",        "RVNL.NS",        "Stock"),
    "sagilityminalimited":           ("SAGILITY",    "SAGILITY.NS",    "Stock"),
    "sagility":                      ("SAGILITY",    "SAGILITY.NS",    "Stock"),
    "sencogoldlimited":              ("SENCO",       "SENCO.NS",       "Stock"),
    "signatureglobalindialtd":       ("SIGNATURE",   "SIGNATURE.NS",   "Stock"),
    "signatureglobal":               ("SIGNATURE",   "SIGNATURE.NS",   "Stock"),
    "silverbees":                    ("SILVERBEES",  "SILVERBEES.BO",  "ETF"),
    "skmeggprod":                    ("SKMEGGPROD",  "SKMEGGPROD.NS",  "Stock"),
    "solarind":                      ("SOLARIND",    "SOLARIND.NS",    "Stock"),
    "supremepowerequipmentlimited":  ("SUPPETRO",    "SUPPETRO.NS",    "Stock"),
    "supremepower":                  ("SUPPETRO",    "SUPPETRO.NS",    "Stock"),
    "swelectenergysystemslimited":   ("SWELECT",     "SWELECT.NS",     "Stock"),
    "swelect":                       ("SWELECT",     "SWELECT.NS",     "Stock"),
    "tatamotors":                    ("TATAMOTORS",  "TATAMOTORS.NS",  "Stock"),
    "themismed":                     ("THEMISMED",   "THEMISMED.NS",   "Stock"),
    "themismedicare":                ("THEMISMED",   "THEMISMED.NS",   "Stock"),
    "waareeeenergieslimited":        ("WAAREEENER",  "WAAREEENER.NS",  "Stock"),
    "waareeenergies":                ("WAAREEENER",  "WAAREEENER.NS",  "Stock"),
    "westcoast":                     ("WESTCOAST",   "WESTCOAST.NS",   "Stock"),
    "westcoastpaper":                ("WESTCOAST",   "WESTCOAST.NS",   "Stock"),
    "westcoas":                      ("WESTCOAST",   "WESTCOAST.NS",   "Stock"),
    "wipro":                         ("WIPRO",       "WIPRO.NS",       "Stock"),
    "wiproltd":                      ("WIPRO",       "WIPRO.NS",       "Stock"),
    "xelpmocdesignandtechlimited":   ("XELPMOC",     "XELPMOC.NS",     "Stock"),
    "xelpmoc":                       ("XELPMOC",     "XELPMOC.NS",     "Stock"),
    "zentechnologieslimited":        ("ZENTEC",      "ZENTEC.NS",      "Stock"),
    "zentechnologies":               ("ZENTEC",      "ZENTEC.NS",      "Stock"),
}


def normalise(name: str) -> str:
    """Lower-case, strip spaces/dots/asterisks/hyphens/brackets for map lookup."""
    import re
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def lookup_ticker(scrip: str) -> tuple[str, str, str]:
    """Return (tk, yf, asset_type).  Falls back to cleaned scrip name if unknown."""
    key = normalise(scrip)
    # Exact match
    if key in TICKER_MAP:
        return TICKER_MAP[key]
    # Prefix / substring match
    for k, v in TICKER_MAP.items():
        if key.startswith(k) or k.startswith(key):
            return v
    # Unknown — derive a rough ticker from the scrip name
    fallback = key[:12].upper()
    print(f"  WARN  unknown scrip '{scrip}' → using '{fallback}'", file=sys.stderr)
    return (fallback, f"{fallback}.NS", "Stock")


# ── PARSE ─────────────────────────────────────────────────────────────────────
def parse_excel(xl_path: Path) -> tuple[float, list[dict]]:
    """
    Returns (cash_infusion_itd, closed_positions_list).

    cash_infusion_itd = gross total of all Buy transactions (₹).
    closed_positions_list = list of dicts, one entry per unique closed position
                            (deduplicated by ISIN where available).
    """
    xl = pd.ExcelFile(xl_path)

    # ── All Transactions ────────────────────────────────────────────────────
    raw = pd.read_excel(xl, sheet_name="All Transactions", header=None)
    df = raw.iloc[3:].copy()
    df.columns = ["num", "scrip", "isin", "date", "exchange",
                  "side", "qty", "price", "amount", "fy", "broker"]
    df["qty"]    = pd.to_numeric(df["qty"],    errors="coerce").fillna(0)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["price"]  = pd.to_numeric(df["price"],  errors="coerce").fillna(0)
    df["scrip"]  = df["scrip"].astype(str).str.strip()
    df["isin"]   = df["isin"].astype(str).str.strip().replace("nan", "")

    cash_infusion_itd = float(df[df["side"] == "Buy"]["amount"].sum())
    print(f"  cash_infusion_itd  = ₹{cash_infusion_itd:,.2f} (gross buys)")

    # ── Upstox realized P&L ─────────────────────────────────────────────────
    raw_up = pd.read_excel(xl, sheet_name="Upstox", header=None)
    df_up = raw_up.iloc[3:].copy()
    df_up.columns = ["num", "scrip", "isin", "date", "exchange",
                     "side", "qty", "rate", "amount", "fy", "rpnl"]
    df_up["rpnl"] = pd.to_numeric(df_up["rpnl"], errors="coerce").fillna(0)
    df_up["scrip"] = df_up["scrip"].astype(str).str.strip()
    df_up["isin"]  = df_up["isin"].astype(str).str.strip().replace("nan", "")

    # Realized P&L per scrip (Upstox) — sum sell-side rpnl
    upstox_rpnl_by_scrip: dict[str, float] = (
        df_up[df_up["side"] == "Sell"]
        .groupby("scrip")["rpnl"]
        .sum()
        .to_dict()
    )

    # ── Identify closed positions ────────────────────────────────────────────
    # signed qty: +Buy, -Sell
    df["signed_qty"] = df.apply(
        lambda r: r["qty"] if r["side"] == "Buy" else -r["qty"], axis=1
    )

    # Group by scrip
    agg = df.groupby("scrip", sort=False).agg(
        net_qty      =("signed_qty", "sum"),
        buy_amt      =("amount", lambda x: x[df.loc[x.index, "side"] == "Buy"].sum()),
        sell_amt     =("amount", lambda x: x[df.loc[x.index, "side"] == "Sell"].sum()),
        isin         =("isin", "first"),
        first_date   =("date", "min"),
        last_date    =("date", "max"),
    ).reset_index()

    closed_df = agg[
        (agg["net_qty"].abs() < 0.01) &
        (agg["scrip"].str.lower() != "nan") &
        (agg["scrip"].str.strip() != "")
    ].copy()

    # ── Deduplicate by ISIN then by resolved ticker ───────────────────────────
    # Same stock may appear as "RELIANCE", "RELIANCE-EQ", "RELIANCE INDUSTRIES LIMITED"
    # across different brokers / segments. Merge them.
    seen_isins:   dict[str, int] = {}   # isin  → index in closed_list
    seen_tickers: dict[str, int] = {}   # tk    → index in closed_list
    closed_list:  list[dict]     = []

    for _, row in closed_df.iterrows():
        scrip    = row["scrip"]
        isin_val = row["isin"] if (row["isin"] and row["isin"].lower() != "nan") else ""

        # Look up ticker
        tk, yf_sym, asset_type = lookup_ticker(scrip)

        # Realized P&L: prefer Upstox data, fallback to sell-buy difference
        rpnl_upstox = upstox_rpnl_by_scrip.get(scrip, None)
        rpnl = rpnl_upstox if rpnl_upstox is not None else round(row["sell_amt"] - row["buy_amt"], 2)

        entry: dict = {
            "tk":         tk,
            "yf":         yf_sym,
            "type":       asset_type,
            "scrip":      scrip,
            "isin":       isin_val or None,
            "buy_amt":    round(float(row["buy_amt"]), 2),
            "sell_amt":   round(float(row["sell_amt"]), 2),
            "rpnl":       round(float(rpnl), 2),
            "rpnl_src":   "upstox" if rpnl_upstox is not None else "calc",
        }

        # Find existing entry to merge into (ISIN preferred, then ticker)
        merge_idx: int | None = None
        if isin_val and isin_val in seen_isins:
            merge_idx = seen_isins[isin_val]
        elif tk in seen_tickers:
            merge_idx = seen_tickers[tk]

        if merge_idx is not None:
            existing = closed_list[merge_idx]
            existing["buy_amt"]  = round(existing["buy_amt"]  + entry["buy_amt"],  2)
            existing["sell_amt"] = round(existing["sell_amt"] + entry["sell_amt"], 2)
            existing["rpnl"]     = round(existing["rpnl"]     + entry["rpnl"],     2)
            src_pair = {existing["rpnl_src"], entry["rpnl_src"]}
            existing["rpnl_src"] = "upstox" if "upstox" in src_pair else "calc"
            if len(scrip) < len(existing["scrip"]):
                existing["scrip"] = scrip
            if isin_val and not existing["isin"]:
                existing["isin"] = isin_val
            print(f"    merged  {entry['tk']:12s} ({scrip}) → same stock as '{existing['scrip']}'")
        else:
            idx = len(closed_list)
            if isin_val:
                seen_isins[isin_val]   = idx
            seen_tickers[tk]           = idx
            closed_list.append(entry)
            print(f"  closed  {tk:12s} ({scrip[:40]:40s})  rpnl={rpnl:>+10,.2f}  [{entry['rpnl_src']}]")

    return cash_infusion_itd, closed_list


# ── MERGE INTO holdings_cost.json ─────────────────────────────────────────────
def merge(cost_file: Path, cash_infusion: float, closed: list[dict], dry_run: bool) -> None:
    cost = json.loads(cost_file.read_text())
    india = cost.setdefault("india", {})

    india["cash_infusion_itd"] = round(cash_infusion, 2)
    india["closed"] = sorted(closed, key=lambda x: x["tk"])

    output = json.dumps(cost, indent=2, ensure_ascii=False)
    if dry_run:
        print("\n── DRY RUN — would write ──────────────────────────────────────")
        snippet = json.dumps({"cash_infusion_itd": india["cash_infusion_itd"],
                              "closed_count": len(india["closed"]),
                              "sample_closed": india["closed"][:3]}, indent=2)
        print(snippet)
        return

    cost_file.write_text(output)
    print(f"\n  ✓  wrote {cost_file}  ({len(india['closed'])} closed positions)")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    paths   = [a for a in args if not a.startswith("--")]

    if not paths:
        print("Usage: python3 scripts/parse_india_excel.py <excel_path> [--dry-run]")
        return 1

    xl_path   = Path(paths[0]).expanduser()
    cost_file = COST_FILE

    if not xl_path.exists():
        print(f"ERROR: {xl_path} not found")
        return 1
    if not cost_file.exists():
        print(f"ERROR: {cost_file} not found")
        return 1

    print(f"Parsing  {xl_path.name}")
    print(f"Target   {cost_file}")
    print()

    cash_infusion, closed = parse_excel(xl_path)
    print(f"\n  {len(closed)} unique closed positions found")
    merge(cost_file, cash_infusion, closed, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
