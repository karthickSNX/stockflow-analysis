import yfinance as yf
import pandas as pd
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.database import get_conn, release_conn

BATCH_SIZE           = 10   # stocks per yfinance download call
FALLBACK_DAYS        = 120  # days to seed on first run — 120 gives enough buffer for accurate 3M period queries
MAX_WORKERS          = 10   # parallel batch workers — reduce to 6-8 if you see empty results (Yahoo rate limiting)
IST                  = timezone(timedelta(hours=5, minutes=30))
MARKET_CLOSE_MINUTES = 15 * 60 + 30  # 15:30 IST in minutes since midnight

def _market_closed_today() -> bool:
    """Return True if NSE market has closed for today (after 15:30 IST, weekday)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday/Sunday — no session today
        return True
    return (now.hour * 60 + now.minute) >= MARKET_CLOSE_MINUTES

def get_watchlist(conn) -> list:
    cur = conn.cursor()
    cur.execute("SELECT symbol FROM stocks WHERE in_watchlist = TRUE")
    return [row[0] for row in cur.fetchall()]

def get_last_stored_date(conn):
    """
    Returns the most recent trade_date in price_data across all symbols,
    or None if the table is empty (first run).
    """
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM price_data")
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None

def _download_batch(tickers, start, end):
    """Download a batch of tickers for a date range. Runs inside a thread — no DB access."""
    return yf.download(
        tickers=tickers,
        start=start,
        end=end,
        auto_adjust=True,
        group_by="ticker",
        multi_level_index=False,
        threads=True,
        progress=False,
    )

def _fetch_batch(batch_index, batch, num_batches, start_date, end_date):
    """
    Fetch one batch and return parsed rows ready for DB insertion.
    Called inside a ThreadPoolExecutor worker — no DB access here.
    Calls _download_batch directly (no nested executor) — safe on Windows.
    The outer pipeline-level timeout in run_pipeline.py (600s) is the safety
    net if yfinance hangs.
    Returns (batch_index, rows) or (batch_index, None) on error.
    """
    print(f"  Batch {batch_index + 1}/{num_batches}: fetching {len(batch)} stocks...")
    try:
        data = _download_batch(batch, start_date, end_date)
    except Exception as e:
        print(f"  ⚠️  Batch {batch_index + 1} failed: {e} — skipping")
        return batch_index, None

    today = date.today()
    market_closed = _market_closed_today()
    rows = []

    for ticker in batch:
        symbol = ticker.replace(".NS", "")
        try:
            df = data if len(batch) == 1 else data[ticker]
            if df.empty:
                print(f"  ⚠️  {symbol}: no data returned")
                continue
            df = df.dropna(subset=["Close"])
            for dt, row in df.iterrows():
                if dt.date() == today and not market_closed:
                    continue  # market still open — skip intraday price
                rows.append((
                    symbol, dt.date(),
                    round(float(row["Open"]),  2),
                    round(float(row["High"]),  2),
                    round(float(row["Low"]),   2),
                    round(float(row["Close"]), 2),
                    int(row["Volume"]),
                ))
        except Exception as e:
            print(f"  ⚠️  {symbol}: skipped — {e}")

    return batch_index, rows

def fetch_and_store_prices() -> int:
    """
    Fetches only missing price data — checks the latest trade_date in price_data
    and downloads from that date to today. On first run (empty table) seeds
    FALLBACK_DAYS of history.

    Phase 1: all batches fetched in parallel via ThreadPoolExecutor (no DB in workers).
    Phase 2: all rows written in a single executemany + commit — one connection, no
    thread-safety issues. Safe on Windows (no nested executors).
    Returns number of stocks successfully fetched.
    """
    conn = get_conn()
    try:
        watchlist = get_watchlist(conn)
        if not watchlist:
            print("⚠️  Watchlist is empty — skipping price fetch")
            return 0

        last_date = get_last_stored_date(conn)
        today = date.today()

        if last_date is not None and last_date >= today:
            print(f"ℹ️  Price data already up to date ({last_date}) — skipped")
            return len(watchlist)

        if last_date is None:
            start_date = (today - timedelta(days=FALLBACK_DAYS)).isoformat()
            print(f"📈 First run — seeding {FALLBACK_DAYS} days of price history for {len(watchlist)} stocks...")
        else:
            # Inclusive of last_date to catch any missed rows for that day
            start_date = last_date.isoformat()
            print(f"📈 Fetching prices from {start_date} → {today} for {len(watchlist)} stocks...")

        end_date = (today + timedelta(days=1)).isoformat()  # yfinance end is exclusive
        tickers     = [f"{s}.NS" for s in watchlist]
        batches     = [tickers[i:i+BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
        num_batches = len(batches)

        # ── Phase 1: fetch all batches in parallel ────────────────────────────────
        # Workers are DB-free — they only call yfinance and return plain tuples.
        batch_results = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_batch, i, batch, num_batches, start_date, end_date): i
                for i, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                batch_index, rows = future.result()
                if rows is not None:
                    batch_results[batch_index] = rows

        # ── Phase 2: bulk write — single connection, one commit ───────────────────
        all_rows = []
        fetched_symbols = set()
        for rows in batch_results.values():
            for row in rows:
                all_rows.append(row)
                fetched_symbols.add(row[0])  # row[0] is symbol

        print(f"📊 {len(all_rows)} rows collected for {len(fetched_symbols)} stocks — writing to DB...")

        if all_rows:
            cur = conn.cursor()
            cur.executemany("""
                INSERT INTO price_data
                    (symbol, trade_date, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, trade_date) DO UPDATE
                    SET open   = EXCLUDED.open,
                        high   = EXCLUDED.high,
                        low    = EXCLUDED.low,
                        close  = EXCLUDED.close,
                        volume = EXCLUDED.volume
            """, all_rows)

        conn.commit()
        print(f"✅ Prices stored for {len(fetched_symbols)}/{len(watchlist)} stocks ({len(all_rows)} rows)")
        return len(fetched_symbols)
    except Exception as e:
        conn.rollback()
        print(f"❌ Price fetch failed: {e}")
        raise
    finally:
        release_conn(conn)

if __name__ == "__main__":
    fetch_and_store_prices()