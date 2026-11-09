"""Веб-дашборд (FastAPI)."""
import secrets
from dataclasses import asdict
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.formatting import fmt_dt, fmt_hours, money
from app.models import ACTION_TITLES, STATUS_TITLES, utcnow
from app.models import Request as RequestModel
from app.services.stats import compute_stats, recent_requests
from app.workflow_config import WorkflowConfig

TEMPLATES_DIR = Path(__file__).parent / "templates"
PERIODS = (7, 30, 90)


def create_app(sm: async_sessionmaker, wf: WorkflowConfig, tz: ZoneInfo, settings: Settings) -> FastAPI:
    app = FastAPI(title="Согласование заявок", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    env = templates.env
    env.filters["money"] = lambda v: money(v, wf.currency)
    env.filters["hours"] = fmt_hours
    env.filters["dt"] = lambda v: fmt_dt(v, tz)
    env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.0f}%"
    env.globals.update(STATUS_TITLES=STATUS_TITLES, ACTION_TITLES=ACTION_TITLES, wf=wf,
                       open_access=settings.open_access)

    security = HTTPBasic(auto_error=False)

    def auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not settings.dashboard_password:
            return
        if credentials is None or not secrets.compare_digest(
            credentials.password.encode(), settings.dashboard_password.encode()
        ):
            raise HTTPException(401, "Нужен пароль", headers={"WWW-Authenticate": "Basic"})

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def dashboard(request: Request, days: int = Query(30)):
        days = days if days in PERIODS else 30
        async with sm() as session:
            stats = await compute_stats(session, wf, tz, days)
            recent = await recent_requests(session)
        max_daily = max((max(d.created, d.closed) for d in stats.daily), default=0) or 1
        max_stage = max((st.avg_h or 0 for st in stats.stages), default=0) or 1
        return templates.TemplateResponse(request, "dashboard.html", {
            "s": stats, "recent": recent, "periods": PERIODS, "max_daily": max_daily,
            "max_stage": max_stage, "reminder_h": wf.reminder_after_hours,
            "now": fmt_dt(utcnow(), tz),
        })

    @app.get("/requests/{request_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def request_detail(request: Request, request_id: int):
        async with sm() as session:
            req = await session.get(RequestModel, request_id)
        if req is None:
            raise HTTPException(404, "Заявка не найдена")
        return templates.TemplateResponse(request, "request.html", {"r": req})

    @app.get("/api/stats", dependencies=[Depends(auth)])
    async def api_stats(days: int = Query(30)):
        async with sm() as session:
            stats = await compute_stats(session, wf, tz, days if days in PERIODS else 30)
        data = asdict(stats)
        return data

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
