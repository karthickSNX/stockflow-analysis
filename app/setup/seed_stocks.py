import requests, csv, io
from app.database import get_conn, release_conn

NIFTY50_URL = (
    "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"
)

def seed_stocks():
    print("⬇️  Downloading Nifty 50 CSV from NSE...")
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}  # both included proactively — NSE blocks bare requests
    resp = requests.get(NIFTY50_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    print(f"📋 {len(rows)} stocks found in CSV")

    conn = get_conn()
    try:
        cur = conn.cursor()
        inserted = 0
        for row in rows:
            # CSV columns: 'Company Name', 'Industry', 'Symbol', 'Series', 'ISIN Code'
            symbol = row["Symbol"].strip()
            company = row["Company Name"].strip()
            sector  = row["Industry"].strip()
            isin    = row["ISIN Code"].strip()
            if not symbol:
                continue
            cur.execute("""
                INSERT INTO stocks (symbol, company_name, sector, isin)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE
                  SET company_name = EXCLUDED.company_name,
                      sector       = EXCLUDED.sector,
                      isin         = EXCLUDED.isin
            """, (symbol, company, sector, isin))
            inserted += 1
        conn.commit()
        print(f"✅ Seeded {inserted} stocks into the database.")
    except Exception as e:
        conn.rollback()
        print(f"❌ Seed failed: {e}")
        raise
    finally:
        release_conn(conn)

if __name__ == "__main__":
    seed_stocks()