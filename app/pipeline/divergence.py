import pandas as pd
from app.database import get_conn, release_conn

WINDOWS = [10, 20, 30]
FLAG_THRESHOLD = 0.3  # Stocks with abs(score) ≥ this get flagged. Raise for stricter, lower for more signals.

def load_flows(conn) -> pd.DataFrame:
    """Load FII net flows into a DataFrame indexed by date."""
    cur = conn.cursor()
    cur.execute("SELECT trade_date, fii_net FROM fii_dii_flows ORDER BY trade_date")
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["date", "fii_net"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")

def load_prices(conn, symbol: str) -> pd.DataFrame:
    """Load close prices for one symbol."""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, close FROM price_data
        WHERE symbol = %s ORDER BY trade_date
    """, (symbol,))
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")

def compute_scores(flows: pd.DataFrame, prices: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    Align flows and prices on date, compute divergence score.
    Returns DataFrame with columns: date, score, direction, is_flagged
    """
    # Align both series on trading dates that exist in both
    merged = prices.join(flows["fii_net"], how="inner")
    if len(merged) < window + 2:
        return pd.DataFrame()  # not enough data for this window

    merged["fii_net"]  = merged["fii_net"].astype(float)
    merged["close"]    = merged["close"].astype(float)

    # 1. Institutional momentum — normalise FII net using rolling window max
    #    Rolling max ensures scores stay comparable over time — using the
    #    all-time series max (old approach) compressed all scores toward zero
    #    as history grew and made scores from different dates incomparable.
    rolling_fii_max = merged["fii_net"].abs().rolling(window, min_periods=1).max().replace(0, 1)
    inst_norm = merged["fii_net"] / rolling_fii_max

    # 2. Institutional momentum — rolling mean of normalised signal
    inst_mom  = inst_norm.rolling(window).mean()

    # 3. Price momentum — percentage change over window, clipped to avoid outliers
    price_mom = merged["close"].pct_change(window).clip(-1, 1)

    # 4. Score — difference between institutional and price momentum, range [-1, 1]
    raw_score = (inst_mom - price_mom).clip(-1, 1)

    result = pd.DataFrame({
        "date": merged.index,
        "score": raw_score.round(4)
    }).dropna()

    result["is_flagged"] = result["score"].abs() >= FLAG_THRESHOLD
    result["direction"] = result["score"].apply(
        lambda s: (
            "FII_BUYING_RETAIL_SELLING"  if s >= FLAG_THRESHOLD
            else "FII_SELLING_RETAIL_BUYING" if s <= -FLAG_THRESHOLD
            else "ALIGNED"
        )
    )
    return result

def calculate_divergence() -> tuple:
    """
    Main entry point — scores all watchlisted stocks across all windows.
    Computes all scores in memory first, then writes everything in one
    single executemany call — dramatically faster than per-stock writes
    over a remote DB connection (Supabase).
    Returns (stocks_scored, stocks_flagged).
    """
    conn = get_conn()
    try:
        # Load all FII flows once — reused for every stock
        flows = load_flows(conn)
        if flows.empty:
            print("⚠️  No flow data found — run fetch_flows first")
            return 0, 0

        cur = conn.cursor()
        cur.execute("SELECT symbol FROM stocks WHERE in_watchlist = TRUE")
        symbols = [r[0] for r in cur.fetchall()]

        # Find the latest scored date per (symbol, window_days) — only write NEW dates.
        # Using a single global MAX(trade_date) (old approach) caused newly-watchlisted
        # stocks to permanently skip historical dates because another stock had already
        # been scored on those dates.
        cur.execute("""
            SELECT symbol, window_days, MAX(trade_date)
            FROM divergence_scores
            GROUP BY symbol, window_days
        """)
        last_scored_map = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        scored = 0
        flagged_symbols = set()
        all_rows = []  # only NEW date rows — skip anything already in DB

        for symbol in symbols:
            prices = load_prices(conn, symbol)
            if prices.empty:
                continue

            stock_has_rows = False
            for window in WINDOWS:
                scores_df = compute_scores(flows, prices, window)
                if scores_df.empty:
                    continue
                last_scored = last_scored_map.get((symbol, window))
                for _, row in scores_df.iterrows():
                    row_date = row["date"].date()
                    # Skip dates already in DB — only write new trading days
                    if last_scored and row_date <= last_scored:
                        if row["is_flagged"]:
                            flagged_symbols.add(symbol)
                        continue
                    is_flagged = bool(row["is_flagged"])
                    all_rows.append((
                        symbol, row_date,
                        window, row["score"], row["direction"],
                        is_flagged
                    ))
                    stock_has_rows = True
                    if is_flagged:
                        flagged_symbols.add(symbol)

            if stock_has_rows or any((symbol, w) in last_scored_map for w in WINDOWS):
                scored += 1

        print(f"📊 {len(all_rows)} new score rows for {scored} stocks — writing to DB...")

        # Single bulk write of only new rows
        if all_rows:
            cur.executemany("""
                INSERT INTO divergence_scores
                    (symbol, trade_date, window_days, score, direction, is_flagged)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, trade_date, window_days) DO UPDATE
                    SET score      = EXCLUDED.score,
                        direction  = EXCLUDED.direction,
                        is_flagged = EXCLUDED.is_flagged
            """, all_rows)
        else:
            print("ℹ️  No new dates to score — already up to date")

        # Always return flagged count from latest date in DB (covers already-scored days)
        cur.execute("""
            SELECT COUNT(DISTINCT symbol) FROM divergence_scores
            WHERE trade_date = (SELECT MAX(trade_date) FROM divergence_scores)
            AND is_flagged = TRUE
        """)
        total_flagged = cur.fetchone()[0]

        conn.commit()
        print(f"✅ Scored {scored} stocks. {total_flagged} flagged on latest date.")
        return scored, total_flagged
    except Exception as e:
        conn.rollback()
        print(f"❌ Divergence calculation failed: {e}")
        raise
    finally:
        release_conn(conn)