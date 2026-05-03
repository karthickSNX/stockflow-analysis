from fastapi import APIRouter, Query
from datetime import date, timedelta
from app.database import get_conn, release_conn

router = APIRouter()

# Literal-path routes MUST come before /{symbol} — FastAPI matches top-to-bottom.
# Putting /all, /flagged, /sectors, /prices after /{symbol} means they'd be
# swallowed as symbol values and never reach the intended handler.

# ── helpers ──────────────────────────────────────────────────────────────────

def _period_start(period: str) -> date:
    """
    Convert a period string to a start date.
    today → most recent trade date (no filter — uses MAX(trade_date) in query)
    1W    → 7 days ago
    1M    → 30 days ago
    3M    → 90 days ago
    """
    today = date.today()
    return {
        "1W": today - timedelta(days=7),
        "1M": today - timedelta(days=30),
        "3M": today - timedelta(days=90),
    }.get(period, None)  # None → today-only (single latest date)

# ── /all ─────────────────────────────────────────────────────────────────────

@router.get("/all")
def get_all():
    """
    Return latest multi-window scores for every watchlisted stock.
    Pivots divergence_scores so each symbol has score_10d / score_20d / score_30d
    in one row — this is what the dashboard leaderboard (lDiv) consumes.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                s.symbol,
                s.company_name  AS company,
                s.sector,
                MAX(CASE WHEN ds.window_days = 10 THEN ds.score END) AS score_10d,
                MAX(CASE WHEN ds.window_days = 20 THEN ds.score END) AS score_20d,
                MAX(CASE WHEN ds.window_days = 30 THEN ds.score END) AS score_30d,
                MAX(CASE WHEN ds.window_days = 20 THEN ds.direction END) AS direction,
                BOOL_OR(ds.is_flagged) AS is_flagged
            FROM stocks s
            JOIN divergence_scores ds ON ds.symbol = s.symbol
            WHERE s.in_watchlist = TRUE
              AND ds.trade_date = (
                  SELECT MAX(trade_date) FROM divergence_scores
              )
            GROUP BY s.symbol, s.company_name, s.sector
            ORDER BY ABS(MAX(CASE WHEN ds.window_days = 30 THEN ds.score END)) DESC NULLS LAST
        """)
        return [
            {
                "symbol":     r[0],
                "company":    r[1],
                "sector":     r[2],
                "score_10d":  round(float(r[3]), 4) if r[3] is not None else None,
                "score_20d":  round(float(r[4]), 4) if r[4] is not None else None,
                "score_30d":  round(float(r[5]), 4) if r[5] is not None else None,
                "direction":  r[6],
                "is_flagged": r[7],
            }
            for r in cur.fetchall()
        ]
    finally:
        release_conn(conn)

# ── /flagged ──────────────────────────────────────────────────────────────────

@router.get("/flagged")
def get_flagged(window_days: int = 20):
    """Return today's flagged stocks for a given window."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ds.symbol, s.company_name, s.sector,
                   ds.score, ds.direction, ds.trade_date
            FROM divergence_scores ds
            JOIN stocks s ON s.symbol = ds.symbol
            WHERE ds.is_flagged = TRUE
              AND ds.window_days = %s
              AND ds.trade_date = (
                  SELECT MAX(trade_date) FROM divergence_scores
                  WHERE window_days = %s AND is_flagged = TRUE
              )
            ORDER BY ABS(ds.score) DESC
        """, (window_days, window_days))
        return [
            {"symbol": r[0], "company_name": r[1], "sector": r[2],
             "score": float(r[3]), "direction": r[4], "as_of": str(r[5])}
            for r in cur.fetchall()
        ]
    finally:
        release_conn(conn)

# ── /sectors ──────────────────────────────────────────────────────────────────

@router.get("/sectors")
def sector_summary(
    window_days: int = 20,
    period: str = Query("today", pattern="^(today|1W|1M|3M)$")
):
    """
    Average divergence score per sector — powers the heatmap.
    period=today  → latest trade date only (existing behaviour)
    period=1W/1M/3M → average across last 7/30/90 days of scores
    Also returns avg_price_change_pct per sector for the same period,
    computed from price_data (latest close vs first close in period).
    Returns as_of date so dashboard can show 'as of Apr 28' label.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        start = _period_start(period)

        if start is None:
            # today — single latest date, existing behaviour
            cur.execute("""
                SELECT s.sector,
                       AVG(ds.score)                                       AS avg_score,
                       COUNT(DISTINCT s.symbol)                            AS stock_count,
                       SUM(CASE WHEN ds.is_flagged THEN 1 ELSE 0 END)     AS flagged_count,
                       MAX(ds.trade_date)                                  AS as_of
                FROM divergence_scores ds
                JOIN stocks s ON s.symbol = ds.symbol
                WHERE ds.window_days = %s
                  AND ds.trade_date = (
                      SELECT MAX(trade_date) FROM divergence_scores WHERE window_days = %s
                  )
                GROUP BY s.sector ORDER BY avg_score DESC
            """, (window_days, window_days))
        else:
            # period average
            cur.execute("""
                SELECT s.sector,
                       AVG(ds.score)                                       AS avg_score,
                       COUNT(DISTINCT s.symbol)                            AS stock_count,
                       SUM(CASE WHEN ds.is_flagged THEN 1 ELSE 0 END)     AS flagged_count,
                       MAX(ds.trade_date)                                  AS as_of
                FROM divergence_scores ds
                JOIN stocks s ON s.symbol = ds.symbol
                WHERE ds.window_days = %s
                  AND ds.trade_date >= %s
                GROUP BY s.sector ORDER BY avg_score DESC
            """, (window_days, start))

        div_rows = cur.fetchall()

        # Price change % per sector — per-symbol subqueries so each stock
        # resolves its own latest/previous date. The old global MAX/MIN caused
        # stocks with different data ranges to silently drop out of the average.
        if start is None:
            cur.execute("""
                SELECT s.sector,
                       AVG((pd_last.close - pd_first.close) / NULLIF(pd_first.close,0) * 100)
                FROM stocks s
                JOIN price_data pd_last ON pd_last.symbol = s.symbol
                    AND pd_last.trade_date = (
                        SELECT MAX(trade_date) FROM price_data p2
                        WHERE p2.symbol = s.symbol
                    )
                JOIN price_data pd_first ON pd_first.symbol = s.symbol
                    AND pd_first.trade_date = (
                        SELECT MAX(trade_date) FROM price_data p3
                        WHERE p3.symbol = s.symbol
                          AND p3.trade_date < (
                              SELECT MAX(trade_date) FROM price_data p4
                              WHERE p4.symbol = s.symbol
                          )
                    )
                WHERE s.in_watchlist = TRUE
                GROUP BY s.sector
            """)
        else:
            cur.execute("""
                SELECT s.sector,
                       AVG((pd_last.close - pd_first.close) / NULLIF(pd_first.close,0) * 100)
                FROM stocks s
                JOIN price_data pd_last ON pd_last.symbol = s.symbol
                    AND pd_last.trade_date = (
                        SELECT MAX(trade_date) FROM price_data p2
                        WHERE p2.symbol = s.symbol
                    )
                JOIN price_data pd_first ON pd_first.symbol = s.symbol
                    AND pd_first.trade_date = (
                        SELECT MIN(trade_date) FROM price_data p3
                        WHERE p3.symbol = s.symbol
                          AND p3.trade_date >= %s
                    )
                WHERE s.in_watchlist = TRUE
                GROUP BY s.sector
            """, (start,))

        price_pct = {r[0]: (round(float(r[1]),2) if r[1] is not None else None)
                     for r in cur.fetchall()}

        return [
            {
                "sector":              r[0],
                "avg_score":          round(float(r[1]),4),
                "stock_count":        r[2],
                "flagged_count":      r[3],
                "as_of":              str(r[4]),
                "price_change_pct":   price_pct.get(r[0]),
            }
            for r in div_rows
        ]
    finally:
        release_conn(conn)

# ── /sector-stocks ────────────────────────────────────────────────────────────

@router.get("/sector-stocks")
def sector_stocks(
    sector: str,
    window_days: int = 20,
    period: str = Query("today", pattern="^(today|1W|1M|3M)$")
):
    """
    Per-stock breakdown for one sector — avg divergence score and price
    change % for the selected period. Powers the heatmap drill-down panel.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        start = _period_start(period)

        if start is None:
            cur.execute("""
                SELECT s.symbol, s.company_name,
                       AVG(ds.score) AS avg_score,
                       BOOL_OR(ds.is_flagged) AS is_flagged
                FROM divergence_scores ds
                JOIN stocks s ON s.symbol = ds.symbol
                WHERE s.sector = %s AND ds.window_days = %s
                  AND ds.trade_date = (SELECT MAX(trade_date) FROM divergence_scores WHERE window_days = %s)
                GROUP BY s.symbol, s.company_name
                ORDER BY ABS(AVG(ds.score)) DESC
            """, (sector, window_days, window_days))
        else:
            cur.execute("""
                SELECT s.symbol, s.company_name,
                       AVG(ds.score) AS avg_score,
                       BOOL_OR(ds.is_flagged) AS is_flagged
                FROM divergence_scores ds
                JOIN stocks s ON s.symbol = ds.symbol
                WHERE s.sector = %s AND ds.window_days = %s
                  AND ds.trade_date >= %s
                GROUP BY s.symbol, s.company_name
                ORDER BY ABS(AVG(ds.score)) DESC
            """, (sector, window_days, start))

        div_stocks = cur.fetchall()
        symbols = [r[0] for r in div_stocks]

        # Price change % per stock — per-symbol latest date subquery.
        # Old today-mode query used a global MAX(trade_date) for pd_last, which
        # dropped stocks whose most recent row didn't match that global date.
        price_pct = {}
        if symbols:
            placeholders = ",".join(["%s"] * len(symbols))
            if start is None:
                cur.execute(f"""
                    SELECT pd_last.symbol,
                           (pd_last.close - pd_first.close) / NULLIF(pd_first.close,0) * 100
                    FROM price_data pd_last
                    JOIN price_data pd_first ON pd_first.symbol = pd_last.symbol
                        AND pd_first.trade_date = (
                            SELECT MAX(trade_date) FROM price_data p2
                            WHERE p2.symbol = pd_last.symbol
                              AND p2.trade_date < (
                                  SELECT MAX(trade_date) FROM price_data p3
                                  WHERE p3.symbol = pd_last.symbol
                              )
                        )
                    WHERE pd_last.symbol IN ({placeholders})
                      AND pd_last.trade_date = (
                          SELECT MAX(trade_date) FROM price_data p4
                          WHERE p4.symbol = pd_last.symbol
                      )
                """, symbols)
            else:
                cur.execute(f"""
                    SELECT pd_last.symbol,
                           (pd_last.close - pd_first.close) / NULLIF(pd_first.close,0) * 100
                    FROM price_data pd_last
                    JOIN price_data pd_first ON pd_first.symbol = pd_last.symbol
                        AND pd_first.trade_date = (
                            SELECT MIN(trade_date) FROM price_data p2
                            WHERE p2.symbol = pd_last.symbol
                              AND p2.trade_date >= %s
                        )
                    WHERE pd_last.symbol IN ({placeholders})
                      AND pd_last.trade_date = (
                          SELECT MAX(trade_date) FROM price_data p3
                          WHERE p3.symbol = pd_last.symbol
                      )
                """, [start] + symbols)
            price_pct = {r[0]: (round(float(r[1]),2) if r[1] is not None else None)
                         for r in cur.fetchall()}

        return [
            {
                "symbol":          r[0],
                "company_name":    r[1],
                "avg_score":       round(float(r[2]),4),
                "is_flagged":      r[3],
                "price_change_pct": price_pct.get(r[0]),
            }
            for r in div_stocks
        ]
    finally:
        release_conn(conn)

# ── /prices/{symbol} — literal prefix must come before /{symbol} ──────────────

@router.get("/prices/{symbol}")
def get_prices(symbol: str, days: int = 90):
    """
    Return the most recent N days of OHLCV price history for one stock.
    Used by the dashboard stock detail panel to render the price chart.
    Fetches newest N rows then returns them in ASC order for the chart.
    """
    sym = symbol.upper()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, open, high, low, close, volume
            FROM (
                SELECT trade_date, open, high, low, close, volume
                FROM price_data
                WHERE symbol = %s
                ORDER BY trade_date DESC
                LIMIT %s
            ) sub
            ORDER BY trade_date ASC
        """, (sym, days))
        return [
            {
                "date":   str(r[0]),
                "open":   float(r[1]),
                "high":   float(r[2]),
                "low":    float(r[3]),
                "close":  float(r[4]),
                "volume": int(r[5]),
            }
            for r in cur.fetchall()
        ]
    finally:
        release_conn(conn)

# ── /{symbol} — MUST be last ───────────────────────────────────────────────────

@router.get("/{symbol}")
def get_score_history(symbol: str, window_days: int = 20, days: int = 60):
    """
    Return score history + price history + FII/DII flow history for one symbol.
    Used by the dashboard expanded stock row: sparkline, window pills, price chart.
    """
    sym = symbol.upper()
    conn = get_conn()
    try:
        cur = conn.cursor()

        # Score history — fetch most recent N days, return in ASC order for charts
        cur.execute("""
            SELECT trade_date, score, direction, is_flagged
            FROM divergence_scores
            WHERE symbol = %s AND window_days = %s
            ORDER BY trade_date DESC LIMIT %s
        """, (sym, window_days, days))
        score_history = [
            {"date": str(r[0]), "score": float(r[1]),
             "direction": r[2], "is_flagged": r[3]}
            for r in reversed(cur.fetchall())
        ]

        # Per-window latest scores for pills
        cur.execute("""
            SELECT window_days, score
            FROM divergence_scores
            WHERE symbol = %s
              AND trade_date = (SELECT MAX(trade_date) FROM divergence_scores WHERE symbol = %s)
        """, (sym, sym))
        windows = {r[0]: float(r[1]) for r in cur.fetchall()}

        # Price history — fetch most recent N days, return in ASC order for charts
        cur.execute("""
            SELECT trade_date, open, high, low, close, volume
            FROM price_data
            WHERE symbol = %s
            ORDER BY trade_date DESC LIMIT %s
        """, (sym, days))
        price_history = [
            {"date": str(r[0]), "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "volume": int(r[5])}
            for r in reversed(cur.fetchall())
        ]

        # FII/DII flow history — most recent N days in ASC order
        cur.execute("""
            SELECT trade_date, fii_net, dii_net
            FROM fii_dii_flows
            ORDER BY trade_date DESC LIMIT %s
        """, (days,))
        flow_history = [
            {"date": str(r[0]),
             "fii_net": float(r[1]) if r[1] is not None else 0,
             "dii_net": float(r[2]) if r[2] is not None else 0}
            for r in reversed(cur.fetchall())
        ]

        return {
            "symbol":        sym,
            "score_history": score_history,
            "price_history": price_history,
            "flow_history":  flow_history,
            "score_10d":     windows.get(10),
            "score_20d":     windows.get(20),
            "score_30d":     windows.get(30),
        }
    finally:
        release_conn(conn)