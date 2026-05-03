from fastapi import APIRouter
from app.database import get_conn, release_conn

router = APIRouter()

@router.get("/all")
def get_all_flows():
    """
    Return the full fii_dii_flows table ordered by date ascending.
    Used by the dashboard FII/DII chart (bar and line modes).
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, fii_buy, fii_sell, fii_net,
                   dii_buy, dii_sell, dii_net, nifty_close
            FROM fii_dii_flows
            ORDER BY trade_date ASC
        """)
        return [
            {
                "trade_date":  str(r[0]),
                "fii_buy":    float(r[1]) if r[1] is not None else None,
                "fii_sell":   float(r[2]) if r[2] is not None else None,
                "fii_net":    float(r[3]) if r[3] is not None else None,
                "dii_buy":    float(r[4]) if r[4] is not None else None,
                "dii_sell":   float(r[5]) if r[5] is not None else None,
                "dii_net":    float(r[6]) if r[6] is not None else None,
                "nifty_close":float(r[7]) if r[7] is not None else None,
            }
            for r in cur.fetchall()
        ]
    finally:
        release_conn(conn)