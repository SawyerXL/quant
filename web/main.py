"""FastAPI app factory + lifespan + route mount."""
import sys, os
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure project root is in path before any imports
_project_root = Path(__file__).parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))

from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from web.db import init_db
from web.auth import current_user, optional_user, admin_user, generate_csrf_token
from web.api import market, auth_routes, signals, portfolio, backtest, admin

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Pre-compute initial regime snapshot (non-blocking)
    import threading
    def _init_snap():
        try:
            from web.services.regime_service import refresh_regime
            refresh_regime()
        except Exception:
            pass
    threading.Thread(target=_init_snap, daemon=True).start()
    # Start scheduler for daily auto-refresh
    try:
        from web.scheduler import start_scheduler, stop_scheduler
        start_scheduler()
    except Exception:
        pass
    yield
    try:
        stop_scheduler()
    except Exception:
        pass


app = FastAPI(title="Quant Circle", lifespan=lifespan)

# Mount static files
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# API routes
app.include_router(market.router)
app.include_router(auth_routes.router)
app.include_router(signals.router)
app.include_router(portfolio.router)
app.include_router(backtest.router)
app.include_router(admin.router)


# ═══ Page routes (SSR) ═══

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user=Depends(optional_user)):
    if not user:
        return RedirectResponse(url="/login")
    csrf = generate_csrf_token()
    return templates.TemplateResponse(request, "market/dashboard.html", {
        "user": user, "csrf_token": csrf,
    })


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.get("/signals", response_class=HTMLResponse)
async def signals_page(request: Request, user=Depends(current_user)):
    csrf = generate_csrf_token()
    return templates.TemplateResponse(request, "signals/dashboard.html", {
        "user": user, "csrf_token": csrf,
    })


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request, user=Depends(current_user)):
    csrf = generate_csrf_token()
    return templates.TemplateResponse(request, "portfolio/dashboard.html", {
        "user": user, "csrf_token": csrf,
    })


@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request, user=Depends(current_user)):
    csrf = generate_csrf_token()
    return templates.TemplateResponse(request, "backtest/dashboard.html", {
        "user": user, "csrf_token": csrf,
    })


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user=Depends(admin_user)):
    csrf = generate_csrf_token()
    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "user": user, "csrf_token": csrf,
    })
