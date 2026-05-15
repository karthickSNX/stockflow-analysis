import asyncio
from fastapi import APIRouter, BackgroundTasks
from app.database import get_conn, release_conn
from app.pipeline.run_pipeline import run_pipeline

router = APIRouter()
_pipeline_running = False

@router.post("/run")
async def trigger_pipeline(background_tasks: BackgroundTasks):
    """
    Trigger the pipeline from the dashboard. Returns immediately.
    run_pipeline() is synchronous blocking code — run_in_executor() offloads
    it to uvicorn's threadpool so the event loop stays free to serve other
    requests (status polls, heatmap etc) while the pipeline runs.
    Using uvicorn's own threadpool (not threading.Thread) is important on
    Windows with --reload: it ensures stdout from the pipeline thread is
    forwarded correctly to the terminal via the reloader's parent process.
    """
    global _pipeline_running
    if _pipeline_running:
        return {"status": "already_running",
                "message": "Pipeline is already running"}

    async def run_and_reset():
        global _pipeline_running
        _pipeline_running = True
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: run_pipeline(triggered_by="manual"))
        finally:
            _pipeline_running = False

    background_tasks.add_task(run_and_reset)
    return {"status": "started",
            "message": "Pipeline started. Poll /pipeline/status for progress."}

@router.get("/status")
async def pipeline_status():
    """Return current running state + latest run info."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, started_at, finished_at, status, current_stage,
                   stocks_fetched, stocks_scored, stocks_flagged, triggered_by
            FROM pipeline_runs ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        latest = None
        if row:
            latest = {
                "id": row[0], "started_at": str(row[1]),
                "finished_at": str(row[2]) if row[2] else None,
                "status": row[3], "current_stage": row[4],
                "stocks_fetched": row[5],
                "stocks_scored": row[6], "stocks_flagged": row[7],
                "triggered_by": row[8]
            }
        return {"is_running": _pipeline_running, "latest_run": latest}
    finally:
        release_conn(conn)

@router.get("/history")
def pipeline_history(limit: int = 20):
    """Return recent pipeline runs for the history tab."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, started_at, finished_at, status,
                   stocks_fetched, stocks_scored, stocks_flagged,
                   error_message, triggered_by
            FROM pipeline_runs ORDER BY started_at DESC LIMIT %s
        """, (limit,))
        return [
            {"id": r[0], "started_at": str(r[1]),
             "finished_at": str(r[2]) if r[2] else None,
             "status": r[3], "stocks_fetched": r[4],
             "stocks_scored": r[5], "stocks_flagged": r[6],
             "error_message": r[7], "triggered_by": r[8]}
            for r in cur.fetchall()
        ]
    finally:
        release_conn(conn)