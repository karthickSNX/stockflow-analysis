import traceback
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from app.database import get_conn, release_conn
from app.pipeline.fetch_flows import fetch_and_store_flows
from app.pipeline.fetch_prices import fetch_and_store_prices
from app.pipeline.divergence import calculate_divergence

PIPELINE_TIMEOUT = 600   # seconds — hard ceiling for the entire pipeline (10 min)
STALE_AFTER     = 3      # minutes — runs older than this with status='running' are dead

def _ts():
    """Current IST time as a readable string for log lines."""
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M:%S")

def log(msg):
    """Print with IST timestamp so every line is traceable."""
    print(f"[{_ts()}] {msg}", flush=True)

def _kill_stale_runs(conn):
    """
    Mark any pipeline_runs rows that have been 'running' for more than
    STALE_AFTER minutes as 'failed'. Called at the start of every run.
    Prevents the dashboard poll from hanging indefinitely after a crash.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_AFTER)
    cur = conn.cursor()
    cur.execute("""
        UPDATE pipeline_runs
        SET status = 'failed',
            finished_at = %s,
            error_message = 'Killed — stale run detected on next pipeline start'
        WHERE status = 'running' AND started_at < %s
    """, (datetime.now(timezone.utc), cutoff))
    killed = cur.rowcount
    conn.commit()
    if killed:
        log(f"⚠️  Cleared {killed} stale run(s) that never finished")

def _run_stages():
    """Run all three pipeline stages. Called inside a thread for timeout enforcement."""
    log("▶  Stage 1/3 — Fetching FII/DII flows...")
    fetch_and_store_flows()
    log("✓  Stage 1/3 — Flows done")

    log("▶  Stage 2/3 — Fetching price data from yfinance...")
    stocks_fetched = fetch_and_store_prices()
    log(f"✓  Stage 2/3 — Prices done ({stocks_fetched} stocks stored)")

    log("▶  Stage 3/3 — Calculating divergence scores...")
    stocks_scored, stocks_flagged = calculate_divergence()
    log(f"✓  Stage 3/3 — Scoring done ({stocks_scored} scored, {stocks_flagged} flagged)")

    return stocks_fetched, stocks_scored, stocks_flagged

def run_pipeline(triggered_by: str = "cron"):
    conn = get_conn()
    run_id = None
    try:
        _kill_stale_runs(conn)

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pipeline_runs (status, triggered_by)
            VALUES ('running', %s) RETURNING id
        """, (triggered_by,))
        run_id = cur.fetchone()[0]
        conn.commit()
        release_conn(conn)
        conn = None

        print(f"\n{'─'*52}", flush=True)
        log(f"🚀 PIPELINE STARTING  (run #{run_id}, triggered by: {triggered_by})")
        log(f"   Hard timeout: {PIPELINE_TIMEOUT}s — will kill if a stage hangs")
        print(f"{'─'*52}\n", flush=True)

        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_run_stages)
            try:
                stocks_fetched, stocks_scored, stocks_flagged = future.result(timeout=PIPELINE_TIMEOUT)
            except FuturesTimeout:
                raise TimeoutError(
                    f"Pipeline timed out after {PIPELINE_TIMEOUT}s — a stage is hanging"
                )

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE pipeline_runs
            SET status='success', finished_at=%s,
                stocks_fetched=%s, stocks_scored=%s, stocks_flagged=%s
            WHERE id=%s
        """, (datetime.now(timezone.utc),
                stocks_fetched, stocks_scored, stocks_flagged, run_id))
        conn.commit()

        print(f"\n{'─'*52}", flush=True)
        log(f"✅ PIPELINE DONE  (run #{run_id})")
        log(f"   Fetched : {stocks_fetched} stocks")
        log(f"   Scored  : {stocks_scored} stocks")
        log(f"   Flagged : {stocks_flagged} stocks")
        print(f"{'─'*52}\n", flush=True)
        print("🟢 Server idle", flush=True)

    except Exception as e:
        err = traceback.format_exc()
        print(f"\n{'─'*52}", flush=True)
        log(f"❌ PIPELINE FAILED  (run #{run_id})")
        log(f"   Error: {e}")
        print(err, flush=True)
        print(f"{'─'*52}\n", flush=True)
        if run_id:
            if conn is None:
                conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE pipeline_runs
                SET status='failed', finished_at=%s, error_message=%s
                WHERE id=%s
            """, (datetime.now(timezone.utc), str(e)[:500], run_id))
            conn.commit()
    finally:
        if conn:
            release_conn(conn)

if __name__ == "__main__":
    run_pipeline(triggered_by="manual")