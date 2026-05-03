import requests
from datetime import date, datetime
from app.database import get_conn, release_conn

BASE_URL = "https://webapi.niftytrader.in/webapi/Resource/fii-dii-activity-data"
NSEPYTHON_TIMEOUT = 30  # seconds — nse_fiidii() has no internal timeout

def _fetch_niftytrader():
    """
    Primary source — NiftyTrader yearly endpoint.
    The daily endpoint no longer returns buy/sell breakdown — only net values.
    The yearly endpoint still has full fii_buy_value/fii_sell_value/dii_buy_value/dii_sell_value.
    rows[0] is always the latest trading day.

    Also opportunistically backfills any rows in fii_dii_flows that have NULL
    nifty_close or NULL fii_buy — we get the full year for free anyway.

    Returns (trade_date, fii_buy, fii_sell, fii_net,
              dii_buy, dii_sell, dii_net, nifty_close)
    for the latest trading day, or raises on any failure.
    """
    year = datetime.now().year
    url = f"{BASE_URL}?request_type=yearly&year_month={year}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != 1:
        raise ValueError(f"NiftyTrader API error: {data.get('resultMessage')}")
    rows = data["resultData"]["fii_dii_data"]
    if not rows:
        raise ValueError("NiftyTrader returned empty data — market may be closed")

    # Opportunistic backfill — upsert all rows except rows[0] (handled by main flow)
    try:
        conn = get_conn()
        cur = conn.cursor()
        backfilled = 0
        for r in rows[1:]:
            cur.execute("""
                INSERT INTO fii_dii_flows
                    (trade_date, fii_buy, fii_sell, fii_net,
                     dii_buy,   dii_sell, dii_net, nifty_close)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trade_date) DO UPDATE
                    SET fii_buy    = EXCLUDED.fii_buy,
                        fii_sell   = EXCLUDED.fii_sell,
                        fii_net    = EXCLUDED.fii_net,
                        dii_buy    = EXCLUDED.dii_buy,
                        dii_sell   = EXCLUDED.dii_sell,
                        dii_net    = EXCLUDED.dii_net,
                        nifty_close= EXCLUDED.nifty_close
                    WHERE fii_dii_flows.nifty_close IS NULL
                       OR fii_dii_flows.fii_buy IS NULL
            """, (
                datetime.fromisoformat(r["created_at"]).date(),
                float(r["fii_buy_value"]),  float(r["fii_sell_value"]),  float(r["fii_net_value"]),
                float(r["dii_buy_value"]),  float(r["dii_sell_value"]),  float(r["dii_net_value"]),
                float(r["last_trade_price"]),
            ))
            backfilled += cur.rowcount
        conn.commit()
        if backfilled:
            print(f"  🔄 Backfilled/patched {backfilled} historical rows from NiftyTrader")
        release_conn(conn)
    except Exception as be:
        print(f"  ⚠️  Backfill failed (non-fatal): {be}")

    row = rows[0]
    trade_date = datetime.fromisoformat(row["created_at"]).date()
    return (
        trade_date,
        float(row["fii_buy_value"]),  float(row["fii_sell_value"]),  float(row["fii_net_value"]),
        float(row["dii_buy_value"]),  float(row["dii_sell_value"]),  float(row["dii_net_value"]),
        float(row["last_trade_price"]),
    )

def _fetch_nsepython_raw():
    """Inner call — run inside a thread so we can enforce a hard timeout."""
    from nsepython import nse_fiidii
    df = nse_fiidii()
    if df is None or df.empty:
        raise ValueError("nse_fiidii() returned empty — market may be closed or data delayed")
    fii_rows = df[df["category"].str.upper().str.contains("FII")]
    dii_rows = df[df["category"].str.upper().str.contains("DII")]
    if fii_rows.empty:
        raise ValueError(f"No FII row in NSEPython response. Categories: {df['category'].tolist()}")
    fii = fii_rows.iloc[0]
    dii = dii_rows.iloc[0] if not dii_rows.empty else None
    trade_date = datetime.strptime(fii["date"].strip(), "%d-%b-%Y").date()
    return (
        trade_date,
        float(fii["buyValue"]),  float(fii["sellValue"]),  float(fii["netValue"]),
        float(dii["buyValue"])  if dii is not None else None,
        float(dii["sellValue"]) if dii is not None else None,
        float(dii["netValue"])  if dii is not None else None,
        None,  # nifty_close — not available from NSEPython
    )

def _fetch_nsepython():
    """
    Fallback source — NSEPython nse_fiidii() with a hard 30s timeout.
    nse_fiidii() has no internal timeout — if NSE's server hangs, it blocks
    forever. We run it in a thread and cancel after NSEPYTHON_TIMEOUT seconds.
    nifty_close is always None — NSEPython doesn't supply it.
    Requires an Indian IP.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_fetch_nsepython_raw)
        try:
            return future.result(timeout=NSEPYTHON_TIMEOUT)
        except FuturesTimeout:
            raise TimeoutError(f"nse_fiidii() timed out after {NSEPYTHON_TIMEOUT}s — NSE server unresponsive")

def fetch_and_store_flows() -> int:
    """
    Pulls today's FII/DII data and upserts to fii_dii_flows.
    Tries NiftyTrader first (no IP restriction, provides nifty_close).
    Falls back to NSEPython if NiftyTrader is unreachable (requires Indian IP;
    nifty_close will be NULL on fallback days).

    Partial-row recovery: if today's row exists but nifty_close IS NULL
    (written by a previous fallback run), NiftyTrader is tried again to patch it.
    NSEPython path always uses DO NOTHING — no point updating with another NULL.

    Returns 1 if a new row was inserted, 0 if skipped or all fetches failed.
    """
    print("📥 Fetching today's FII/DII flows...")
    today = date.today()

    # Check existing row — skip only if nifty_close is already populated
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT nifty_close FROM fii_dii_flows WHERE trade_date = %s", (today,)
        )
        existing = cur.fetchone()
    finally:
        release_conn(conn)

    if existing is not None and existing[0] is not None:
        print(f"ℹ️  Flow row for {today} already complete — skipped")
        return 0

    if existing is not None and existing[0] is None:
        print(f"⚠️  Row for {today} exists but nifty_close is NULL — attempting NiftyTrader patch...")

    # Try NiftyTrader first
    nt_success = False
    row_data = None
    try:
        row_data = _fetch_niftytrader()
        nt_success = True
        print("  ✅ NiftyTrader fetch succeeded")
    except Exception as e:
        print(f"  ⚠️  NiftyTrader failed ({e}) — trying NSEPython fallback...")

    # If NiftyTrader failed and row already exists with NULL, NSEPython
    # also returns NULL — nothing to gain, skip
    if not nt_success and existing is not None:
        print("  ⚠️  NiftyTrader unavailable and row already exists — nifty_close stays NULL")
        return 0

    if not nt_success:
        try:
            row_data = _fetch_nsepython()
            print("  ✅ NSEPython fallback succeeded (nifty_close will be NULL)")
        except Exception as e2:
            print(f"  ❌ NSEPython fallback also failed: {e2}")
            print("  ⚠️  Skipping flow update — pipeline will score with existing data")
            return 0

    trade_date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net, nifty_close = row_data

    conn = get_conn()
    try:
        cur = conn.cursor()
        if nt_success and existing is not None:
            # Patch existing NULL row — update nifty_close only
            cur.execute("""
                UPDATE fii_dii_flows SET nifty_close = %s
                WHERE trade_date = %s AND nifty_close IS NULL
            """, (nifty_close, trade_date))
            conn.commit()
            print(f"✅ Patched nifty_close for {trade_date}: {nifty_close}")
            return 0
        elif nt_success:
            cur.execute("""
                INSERT INTO fii_dii_flows
                    (trade_date, fii_buy, fii_sell, fii_net,
                     dii_buy,   dii_sell, dii_net, nifty_close)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trade_date) DO UPDATE
                    SET nifty_close = EXCLUDED.nifty_close
                    WHERE fii_dii_flows.nifty_close IS NULL
            """, (trade_date, fii_buy, fii_sell, fii_net,
                    dii_buy, dii_sell, dii_net, nifty_close))
        else:
            # NSEPython fallback — DO NOTHING, no point overwriting with another NULL
            cur.execute("""
                INSERT INTO fii_dii_flows
                    (trade_date, fii_buy, fii_sell, fii_net,
                     dii_buy,   dii_sell, dii_net, nifty_close)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trade_date) DO NOTHING
            """, (trade_date, fii_buy, fii_sell, fii_net,
                    dii_buy, dii_sell, dii_net, nifty_close))
        inserted = cur.rowcount
        conn.commit()
        close_str = f" | Nifty close: {nifty_close}" if nifty_close else " (nifty_close NULL)"
        print(f"✅ Flow row {'inserted' if inserted else 'already existed'} for {trade_date}{close_str}")
        return inserted
    except Exception as e:
        conn.rollback()
        print(f"❌ Flow insert failed: {e}")
        raise
    finally:
        release_conn(conn)

if __name__ == "__main__":
    fetch_and_store_flows()