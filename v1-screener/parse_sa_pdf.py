#!/usr/bin/env python3
"""SA Quant PDF parser — extracts ticker + Quant Rating from SA screener exports.

Supported SA PDF exports:
  - "Quant Strong Buy · Stock Screener"
  - "Quant ETF Strong Buy · ETF Screener"
  - "Top Rated ETFs"
  - Any SA screener PDF with columns: Rank Symbol Name Price Change% ... Quant Rating

Usage:
  python3 parse_sa_pdf.py file1.pdf [file2.pdf ...] [--out sa_ratings.json]
  python3 parse_sa_pdf.py ~/Downloads/*.pdf --push   # also updates VM sa_ratings.json + triggers V1

Output JSON: { "NVDA": {"rating": 4.85, "as_of": "2026-06-20", "source": "stocks"}, ... }
"""
import sys, re, json, argparse
from pathlib import Path
from datetime import date

try:
    import pdfplumber
except ImportError:
    print("pip3 install pdfplumber", file=sys.stderr); sys.exit(1)

# Words that look like tickers but aren't
_NOT_TICKER = {
    'ETF','USD','UK','AI','THE','AND','FOR','NEW','TOP','ADR','INC',
    'NA','NM','GS','US','SA','LP','PLC','AG','SAS','SE','NV','BV','AB',
    'LLC','LTD','REIT','SP','BUY','SELL','HOLD','STRONG','WEAK',
}

def _parse_stocks_line(tokens) -> tuple:
    """Parse ranked stock line: Rank Ticker Name ... Quant% Days grades"""
    if not re.match(r'^\d+$', tokens[0]): return None, None, None
    tk = tokens[1]
    if not re.match(r'^[A-Z]{2,6}$', tk) or tk in _NOT_TICKER: return None, None, None
    floats = [float(t) for t in tokens if re.match(r'^\d{1,2}\.\d{2}$', t) and 3.9 <= float(t) <= 5.1]
    quant  = next((f for f in floats if 4.0 <= f <= 5.0), None)
    if not quant: return None, None, None
    days = None
    past = False
    for t in tokens[2:]:
        if re.match(r'^\d{1,2}\.\d{2}$', t): past = True
        elif past and re.match(r'^\d{1,3}$', t) and 1 <= int(t) <= 999:
            days = int(t); break
    return tk, quant, days

def _parse_etf_line(tokens) -> tuple:
    """Parse ranked ETF line: Rank Ticker Name Price Change% ... DaysAtQuant DaysToCover QuantRating"""
    if not (re.match(r'^\d+$', tokens[0]) or True): return None, None, None
    tk = next((t for t in tokens if re.match(r'^[A-Z]{2,6}$',t) and t not in _NOT_TICKER), None)
    if not tk: return None, None, None
    last = tokens[-1]
    if not re.match(r'^4\.\d{2}$', last): return None, None, None
    days = next((int(t) for t in tokens if re.match(r'^\d{1,3}$',t) and 1<=int(t)<=999), None)
    return tk, float(last), days

def _parse_page_text(text: str) -> dict:
    """Returns {ticker: {rating, days_at_rating}} — handles both stock + ETF formats."""
    results = {}
    for line in text.split('\n'):
        tokens = line.strip().split()
        if len(tokens) < 4: continue
        # Try stock ranked format first (starts with rank integer)
        if re.match(r'^\d+$', tokens[0]):
            tk, rating, days = _parse_stocks_line(tokens)
            if tk and rating:
                results[tk] = {'rating': rating, 'days_at_rating': days}
                continue
        # Try ETF format (ranked or unranked)
        tk, rating, days = _parse_etf_line(tokens)
        if tk and rating:
            results[tk] = {'rating': rating, 'days_at_rating': days}
    return results

def parse_pdf(path: str) -> dict:
    """Return {TICKER: rating} from one SA PDF."""
    results = {}
    fname = Path(path).name.lower()
    src = 'etf' if 'etf' in fname else 'stock'
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                # Text extraction (most reliable for SA layout)
                text = page.extract_text() or ""
                results.update(_parse_page_text(text))
    except Exception as e:
        print(f"⚠ {path}: {e}", file=sys.stderr)
    return results, src

def parse_all(pdf_paths: list) -> dict:
    """Merge results from multiple PDFs. Higher rating wins on conflict."""
    today = date.today().isoformat()
    merged = {}
    for path in pdf_paths:
        tickers, src = parse_pdf(path)
        print(f"  {Path(path).name}: {len(tickers)} tickers extracted")
        for tk, v in tickers.items():
            rating = v['rating']
            days   = v.get('days_at_rating')
            if tk not in merged or rating > merged[tk]['rating']:
                merged[tk] = {
                    'rating': rating,
                    'days_at_rating': days,
                    'as_of': today,
                    'source': src
                }
    return merged

def push_to_vm(ratings: dict, key='~/.ssh/ssh-key-2026-05-26.key', host='opc@145.241.158.254'):
    """Merge into VM sa_ratings.json + trigger V1 run."""
    import subprocess, tempfile, os, json
    key = os.path.expanduser(key)
    # Pull existing VM ratings
    r = subprocess.run(
        ['ssh', '-i', key, host, 'cat /home/opc/v1/sa_ratings.json'],
        capture_output=True, text=True
    )
    try:
        base = json.loads(r.stdout)
    except Exception:
        base = {}
    base.update(ratings)
    content = json.dumps(base, indent=2)
    # Push back
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write(content); fname = f.name
    subprocess.run(['scp', '-i', key, '-q', fname, f'{host}:/home/opc/v1/sa_ratings.json'])
    os.unlink(fname)
    # Update sa_top.txt (add any new tickers)
    top_tickers = '\n'.join(sorted(base.keys())) + '\n'
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write('# SA Quant tickers — auto-updated by parse_sa_pdf.py\n')
        f.write(top_tickers); fname = f.name
    subprocess.run(['scp', '-i', key, '-q', fname, f'{host}:/home/opc/v1/sa_top.txt'])
    os.unlink(fname)
    # Trigger V1 run
    subprocess.Popen(
        ['ssh', '-i', key, host, 'python3 /home/opc/v1/run_v1.py >> /home/opc/v1.log 2>&1']
    )
    print(f"✓ pushed {len(base)} ratings to VM + triggered V1 run")

def main():
    ap = argparse.ArgumentParser(description='Parse SA Quant PDFs → sa_ratings.json')
    ap.add_argument('pdfs', nargs='+', help='SA screener PDF files')
    ap.add_argument('--out', default='sa_ratings.json', help='Output JSON file (default: sa_ratings.json)')
    ap.add_argument('--push', action='store_true', help='Push to VM + trigger V1 immediately')
    ap.add_argument('--min-rating', type=float, default=4.50, help='Min SA Quant rating to include (default 4.50)')
    args = ap.parse_args()

    print(f"Parsing {len(args.pdfs)} PDF(s)…")
    merged = parse_all(args.pdfs)

    # Filter by min rating
    filtered = {tk: v for tk, v in merged.items() if v['rating'] >= args.min_rating}
    filtered_out = len(merged) - len(filtered)
    print(f"  {len(merged)} total → {len(filtered)} kept (≥{args.min_rating}) · {filtered_out} filtered")

    # Print ranked table
    print(f"\n{'TK':8} {'RATING':>6}  {'AS_OF':>10}  {'SRC'}")
    print('-' * 38)
    for tk, v in sorted(filtered.items(), key=lambda x: -x[1]['rating']):
        print(f"{tk:8} {v['rating']:>6.2f}  {v['as_of']:>10}  {v['source']}")

    # Save local
    out = Path(args.out)
    # Merge with existing file if present
    existing = {}
    if out.exists():
        try: existing = json.loads(out.read_text())
        except: pass
    existing.update(filtered)
    out.write_text(json.dumps(existing, indent=2))
    print(f"\n✓ saved {len(existing)} ratings → {out}")

    if args.push:
        push_to_vm(filtered)

if __name__ == '__main__':
    main()
