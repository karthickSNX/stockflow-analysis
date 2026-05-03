import requests
from datetime import datetime
from app.database import get_conn, release_conn

BASE_URL = "https://webapi.niftytrader.in/webapi/Resource/fii-dii-activity-data"

def fetch_year(year: int) -> list:
    """Fetch all trading days for a given year from NiftyTrader API."""
    url = f"{BASE_URL}?request_type=yearly&year_month={year}"
    print(f"  🌐 Fetching {year}...")
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != 1:
        raise ValueError(f"API returned failure for year {year}: {data.get('resultMessage')}")
    rows = data["resultData"]["fii_dii_data"]
    print(f"  ✅ {len(rows)} trading days received")
    return rows

def upsert_rows(rows: list):
    """Parse and upsert a list of API rows into fii_dii_flows."""
    conn = get_conn()
    inserted = skipped = 0
    try:
        cur = conn.cursor()
        for row in rows:
            trade_date = datetime.fromisoformat(row["created_at"]).date()
            cur.execute("""
                INSERT INTO fii_dii_flows
                    (trade_date, fii_buy, fii_sell, fii_net,
                     dii_buy,   dii_sell, dii_net, nifty_close)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trade_date) DO NOTHING
            """, (
                trade_date,
                row["fii_buy_value"],  row["fii_sell_value"],  row["fii_net_value"],
                row["dii_buy_value"],  row["dii_sell_value"],  row["dii_net_value"],
                row["last_trade_price"]
            ))
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
        print(f"  📥 {inserted} inserted, {skipped} already existed (skipped)")
    except Exception as e:
        conn.rollback()
        print(f"  ❌ DB error: {e}")
        raise
    finally:
        release_conn(conn)

if __name__ == "__main__":
    raw = input("Seed which years? (comma-separated, e.g. 2025,2026) [2026]: ").strip()
    years = [int(y.strip()) for y in raw.split(",")] if raw else [2026]

    print(f"\n📂 Seeding years: {years}")
    for year in years:
        print(f"\n── {year} ──")
        try:
            rows = fetch_year(year)
            upsert_rows(rows)
        except Exception as e:
            print(f"  ❌ Skipping {year}: {e}")

    print("\n✅ Seed complete.")