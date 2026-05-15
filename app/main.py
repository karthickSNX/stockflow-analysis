import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from app.database import close_all
from app.routes import stocks, divergence, pipeline, flows

load_dotenv()

# ── Human-readable access log ───────────────────────────────────────────────
# Replaces uvicorn's raw "127.0.0.1:51664 - GET /divergence/sectors HTTP/1.1 200"
# with plain English like "Fetching sector data (3M)… done"
# Polling endpoints (/pipeline/status, /health) are silenced entirely.

_ROUTE_LABELS = {
    "/divergence/all":        "Loading all divergence scores",
    "/divergence/sectors":    "Fetching sector heatmap data",
    "/divergence/sector-stocks": "Fetching sector drill-down",
    "/divergence/flagged":    "Fetching flagged stocks",
    "/divergence/prices":     "Loading price history",
    "/divergence/":           "Loading stock detail",
    "/flows/all":             "Fetching FII/DII flow data",
    "/stocks/count":          "Checking stock universe",
    "/stocks":                "Loading stock list",
    "/pipeline/run":          "Pipeline triggered",
    "/pipeline/history":      "Loading pipeline history",
    "/dashboard":             "Serving dashboard",
}

def _friendly(path: str) -> str:
    # Extract query params for context (period=3M, symbol=RELIANCE, etc.)
    label = None
    base = path.split("?")[0]
    qs   = path.split("?")[1] if "?" in path else ""
    for prefix, desc in _ROUTE_LABELS.items():
        if base.startswith(prefix):
            label = desc
            break
    if not label:
        label = f"Request to {base}"
    # Append any useful query params in plain English
    extras = []
    for part in qs.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k == "period":   extras.append(f"period={v}")
            if k == "sector":   extras.append(f"sector={v.replace('%20',' ')}")
            if k == "window_days": extras.append(f"{v}d window")
    # Symbol from path e.g. /divergence/RELIANCE
    sym = base.split("/")[-1]
    if sym.isupper() and len(sym) <= 10:
        extras.append(sym)
    return label + (f"  ({', '.join(extras)})" if extras else "")

class _HumanLog(logging.Handler):
    _MUTE = ("/pipeline/status", "/health", "/favicon")
    def emit(self, record):
        try:
            if not isinstance(record.args, tuple) or len(record.args) != 5:
                return
            _, method, path, _, status = record.args
            if any(p in path for p in self._MUTE):
                return
            ok = "✓" if str(status).startswith("2") else f"✗ {status}"
            print(f"{ok}  {_friendly(path)}", flush=True)
        except Exception:
            pass

# Replace uvicorn's access logger handlers entirely so its formatter never runs
_acc = logging.getLogger("uvicorn.access")
_acc.handlers.clear()
_acc.propagate = False
_acc.addHandler(_HumanLog())

# Resolve dashboard path relative to this file — works regardless of cwd
DASHBOARD_PATH = Path(__file__).parent.parent / "dashboard.html"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Replaces deprecated @app.on_event. Runs startup logic, then yields,
    then runs shutdown logic when uvicorn stops."""
    print("─" * 52, flush=True)
    print("🟢  Server ready  →  http://localhost:8000/dashboard", flush=True)
    print("─" * 52, flush=True)
    yield
    print("\n🔴  Server shutting down", flush=True)
    close_all()

app = FastAPI(
    title="StockFlow Analysis API",
    description="FII/DII vs retail flow divergence for NSE stocks",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow the dashboard HTML to call this API from the browser.
# Since the dashboard is served from the same origin (FastAPI), CORS is
# only needed if you ever open dashboard.html as a file:// URL directly.
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "http://localhost:8000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)

# NOTE: We do NOT mount StaticFiles(directory=".") — that would expose
# .env and other secrets at /static/.env. The dashboard is a single HTML
# file with no external assets, so it needs no static file serving at all.

# Register route modules
app.include_router(stocks.router,    prefix="/stocks",    tags=["stocks"])
app.include_router(divergence.router, prefix="/divergence", tags=["divergence"])
app.include_router(pipeline.router,   prefix="/pipeline",  tags=["pipeline"])
app.include_router(flows.router,      prefix="/flows",     tags=["flows"])

@app.get("/dashboard")
async def serve_dashboard():
    """Serve dashboard.html — path resolved relative to this file, not cwd."""
    return FileResponse(str(DASHBOARD_PATH))

@app.get("/health")
async def health():
    return {"status": "ok"}