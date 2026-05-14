import pandas as pd
from datetime import date, timedelta
from app.database import get_conn, release_conn

WINDOWS = [10, 20, 30]
FLAG_THRESHOLD = 0.3  # Stocks with abs(score) ≥ this get flagged. Raise for stricter, lower for more signals.
FFILL_LIMIT    = 5    # Max consecutive trading days a flow gap is forward-filled before the date is dropped.
FORCE_RECALCULATE_DAYS = 3  # Always re-score the last N trading days so FLAG_THRESHOLD changes take effect immediately.

def load_flows(conn) -> pd.DataFrame:
    """
    Load FII net flows into a DataFrame indexed by date.
    Logs a summary of coverage (row count, date range, latest date) on
    every run. Also warns if any gap between consecutive rows exceeds 5
    calendar days — a sign that fetch_flows has been failing silently.
    """
    cur = conn.cursor()
    cur.execute("SELECT trade_date, fii_net FROM fii_dii_flows ORDER BY trade_date")
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["date", "fii_net"])
    df["date"] = pd.to_datetime(df["date"])
    if df.empty:
        print("⚠️  fii_dii_flows is empty — run seed_flows.py first")
        return df.set_index("date")
    # Always print a one-line coverage summary so you can spot a stale table at a glance.
    print(
        f"📅 Flow data: {len(df)} rows | "
        f"{df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()} | "
        f"latest: {df['date'].iloc[-1].date()}"
    )
    # Gap detection — warn if any gap is large enough that ffill won't cover it.
    # Gaps ≤ 5 days are bridged silently by the left-join ffill in compute_scores.
    # Gaps > 5 days mean some price dates will be dropped from scoring.
    if len(df) > 1:
        gap_days = df["date"].diff().dt.days
        big_gaps = gap_days[gap_days > 5]
        if not big_gaps.empty:
            print(
                f"⚠️  Flow gaps detected: {len(big_gaps)} gap(s) > 5 calendar days. "
                f"Largest: {int(big_gaps.max())}d ending "
                f"{df.loc[big_gaps.idxmax(), 'date'].date()} — "
                "price dates inside this gap will be dropped from scoring. "
                "Run seed_flows.py to patch."
            )
        else:
            print("✅ Flow data has no large gaps — ffill will cover any minor holiday gaps")
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

    Uses a LEFT JOIN on price dates so that every trading day with a price
    is included. Flow gaps up to FFILL_LIMIT consecutive days are forward-
    filled (last known flow value carried forward). Dates where no nearby
    flow value exists after filling are dropped.

    Previously used an INNER JOIN which silently dropped stocks whose price
    dates didn't overlap with flow dates — the root cause of the "only 3 / 43
    stocks flagged" issue when fii_dii_flows had gaps.

    Returns DataFrame with columns: date, score, direction, is_flagged.
    """
    # LEFT JOIN: keep all price dates, attach flow where available.
    # Forward-fill up to FFILL_LIMIT days to bridge short flow gaps (holidays,
    # fetch failures). Dates still missing after ffill are dropped via dropna.
    merged = prices.join(flows["fii_net"], how="left")
    merged["fii_net"] = merged["fii_net"].ffill(limit=FFILL_LIMIT)
    merged = merged.dropna(subset=["fii_net"])

    if len(merged) < window + 2:
        return pd.DataFrame()  # not enough data for this window

    merged["fii_net"]  = merged["fii_net"].astype(float)
    merged["close"]    = merged["close"].astype(float)

    # 1. Institutional momentum — normalise FII net using rolling window max.
    #    Rolling max ensures scores stay comparable over time — using the
    #    all-time series max (old approach) compressed all scores toward zero
    #    as history grew and made scores from different dates incomparable.
    rolling_fii_max = merged["fii_net"].abs().rolling(window, min_periods=1).max().replace(0, 1)
    inst_norm = merged["fii_net"] / rolling_fii_max

    # 2. Institutional momentum — rolling mean of normalised signal.
    inst_mom  = inst_norm.rolling(window).mean()

    # 3. Price momentum — percentage change over window, clipped to avoid outliers.
    price_mom = merged["close"].pct_change(window).clip(-1, 1)

    # 4. Score — difference between institutional and price momentum, range [-1, 1].
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

    The last FORCE_RECALCULATE_DAYS trading days are always re-scored
    (ON CONFLICT DO UPDATE handles overwrites). This means changing
    FLAG_THRESHOLD takes effect on the next run without a full reseed.

    Returns (stocks_scored, stocks_flagged).
    """
    conn = get_conn()
    try:
        # Load all FII flows once — reused for every stock.
        # load_flows() also logs a warning if fii_dii_flows has large gaps.
        flows = load_flows(conn)
        if flows.empty:
            print("⚠️  No flow data found — run fetch_flows first")
            return 0, 0

        cur = conn.cursor()
        cur.execute("SELECT symbol FROM stocks WHERE in_watchlist = TRUE")
        symbols = [r[0] for r in cur.fetchall()]
        print(f"🔍 Scoring {len(symbols)} watchlisted stocks across windows {WINDOWS} | threshold: {FLAG_THRESHOLD} | force-recalc last {FORCE_RECALCULATE_DAYS} days")

        # Find the latest scored date per (symbol, window_days).
        # The last FORCE_RECALCULATE_DAYS days will be re-scored regardless,
        # so that FLAG_THRESHOLD changes apply without a full reseed.
        # Using a per-(symbol, window) MAX (not a single global MAX) prevents
        # newly-watchlisted stocks from permanently skipping historical dates.
        cur.execute("""
            SELECT symbol, window_days, MAX(trade_date)
            FROM divergence_scores
            GROUP BY symbol, window_days
        """)
        last_scored_map = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        # Cutoff: dates strictly before this are skipped (already scored and old enough).
        # Dates on or after this cutoff are always recalculated.
        force_cutoff = date.today() - timedelta(days=FORCE_RECALCULATE_DAYS)

        scored = 0
        skipped_no_prices = []
        skipped_no_overlap = []
        flagged_symbols = set()
        all_rows = []
        rows_new = 0
        rows_recalc = 0
        rows_skipped = 0

        for symbol in symbols:
            prices = load_prices(conn, symbol)
            if prices.empty:
                skipped_no_prices.append(symbol)
                continue

            stock_has_rows = False
            all_windows_empty = True
            for window in WINDOWS:
                scores_df = compute_scores(flows, prices, window)
                if scores_df.empty:
                    continue
                all_windows_empty = False
                last_scored = last_scored_map.get((symbol, window))
                for _, row in scores_df.iterrows():
                    row_date = row["date"].date()
                    # Skip only if: this date was already scored AND it's older than
                    # the force-recalculate window. Recent dates are always rewritten.
                    if last_scored and row_date < last_scored and row_date < force_cutoff:
                        rows_skipped += 1
                        if row["is_flagged"]:
                            flagged_symbols.add(symbol)
                        continue
                    is_flagged = bool(row["is_flagged"])
                    all_rows.append((
                        symbol, row_date,
                        window, row["score"], row["direction"],
                        is_flagged
                    ))
                    # Track whether this is a genuinely new date or a force-recalc overwrite.
                    if last_scored and row_date <= last_scored:
                        rows_recalc += 1
                    else:
                        rows_new += 1
                    stock_has_rows = True
                    if is_flagged:
                        flagged_symbols.add(symbol)

            if all_windows_empty:
                skipped_no_overlap.append(symbol)
            elif stock_has_rows or any((symbol, w) in last_scored_map for w in WINDOWS):
                scored += 1

        # Log any stocks that couldn't be scored, so silent failures are visible.
        if skipped_no_prices:
            print(f"⚠️  {len(skipped_no_prices)} stock(s) skipped — no price data in DB: {', '.join(skipped_no_prices)}")
        if skipped_no_overlap:
            print(f"⚠️  {len(skipped_no_overlap)} stock(s) skipped — price/flow overlap too short for any window: {', '.join(skipped_no_overlap)}")

        print(
            f"📊 Rows to write: {len(all_rows)} total "
            f"({rows_new} new, {rows_recalc} force-recalc overwrite, {rows_skipped} already-scored skipped) "
            f"across {scored} stocks — writing to DB..."
        )

        # Single bulk write — ON CONFLICT DO UPDATE overwrites recent dates with
        # freshly computed scores (picks up FLAG_THRESHOLD changes automatically).
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
            print("ℹ️  No rows to write — already up to date")

        # Always return flagged count from latest date in DB (covers already-scored days).
        cur.execute("""
            SELECT COUNT(DISTINCT symbol) FROM divergence_scores
            WHERE trade_date = (SELECT MAX(trade_date) FROM divergence_scores)
            AND is_flagged = TRUE
        """)
        total_flagged = cur.fetchone()[0]

        conn.commit()
        print(f"✅ Divergence done — {scored}/{len(symbols)} stocks scored | {total_flagged} flagged on latest date | threshold: {FLAG_THRESHOLD}")
        return scored, total_flagged
    except Exception as e:
        conn.rollback()
        print(f"❌ Divergence calculation failed: {e}")
        raise
    finally:
        release_conn(conn)