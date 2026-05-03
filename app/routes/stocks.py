from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.database import get_conn, release_conn

router = APIRouter()

class WatchlistUpdate(BaseModel):
    in_watchlist: bool

@router.get("/sectors")
def get_sectors():
    """Return distinct sectors from the stocks table."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sector, COUNT(*) as stock_count
            FROM stocks WHERE sector IS NOT NULL
            GROUP BY sector ORDER BY sector
        """)
        return [
            {"sector": r[0], "stock_count": r[1]}
            for r in cur.fetchall()
        ]
    finally:
        release_conn(conn)

@router.get("")
def get_stocks(sector: str = None, watchlist_only: bool = False):
    """Return stocks, optionally filtered by sector or watchlist."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        query = "SELECT symbol, company_name, sector, in_watchlist FROM stocks"
        params = []
        conditions = []
        if sector:
            conditions.append("sector = %s"); params.append(sector)
        if watchlist_only:
            conditions.append("in_watchlist = TRUE")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY symbol"
        cur.execute(query, params)
        return [
            {"symbol": r[0], "company_name": r[1],
             "sector": r[2], "in_watchlist": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        release_conn(conn)

@router.patch("/{symbol}/watchlist")
def toggle_watchlist(symbol: str, body: WatchlistUpdate):
    """Add or remove a stock from the watchlist."""
    add = body.in_watchlist
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE stocks SET in_watchlist = %s WHERE symbol = %s RETURNING symbol",
            (add, symbol.upper())
        )
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
        conn.commit()
        return {"symbol": symbol, "in_watchlist": add}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        release_conn(conn)

@router.get("/count")
def get_stock_count():
    """Return total number of stocks seeded — used by dashboard on boot."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM stocks")
        return {"total": cur.fetchone()[0]}
    finally:
        release_conn(conn)

# ── Seed endpoints called by dashboard "Initialise Stock Universe" button ──
import threading, subprocess, sys
_seed_state = {"running": False, "done": False, "progress": 0, "message": ""}

def _run_seed():
    _seed_state.update({"running": True, "done": False, "progress": 0, "message": "Starting..."})
    try:
        _seed_state["message"] = "Seeding stock universe..."; _seed_state["progress"] = 20
        subprocess.run([sys.executable, "app/setup/seed_stocks.py"], check=True)
        _seed_state.update({"progress": 100, "message": "Done — 50 stocks seeded.", "done": True})
    except Exception as e:
        _seed_state.update({"message": str(e), "done": True})
    finally:
        _seed_state["running"] = False

@router.post("/seed")
def seed_stocks():
    """Kick off seed_stocks.py in a background thread."""
    if _seed_state["running"]:
        raise HTTPException(status_code=409, detail="Seed already running")
    threading.Thread(target=_run_seed, daemon=True).start()
    return {"status": "started"}

@router.get("/seed/status")
def seed_status():
    """Poll seed progress — dashboard calls this every 1.5 s."""
    return _seed_state