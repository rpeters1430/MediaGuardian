import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import auth, cleanup, core, crosscheck, events, guardian, playback, stack


class MuteBody(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=4096)


ALL_KINDS = {**cleanup.KINDS, **crosscheck.KINDS, **playback.KINDS}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


@asynccontextmanager
async def lifespan(app):
    auth.validate_config()
    core.init_db()
    stop = threading.Event()
    if os.getenv("MG_BACKGROUND", "1") != "0":
        guardian.start_background(stop)
    yield
    stop.set()


app = FastAPI(
    title="MediaGuardian", version="0.6.0", lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with core.connect() as db:
            db.execute("SELECT 1")
    except sqlite3.Error:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ready"}


@app.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    auth.same_origin(request)
    auth.authenticate_login(body.username, body.password, request.client.host if request.client else "unknown")
    response.set_cookie(
        "mg_session", auth.make_session(), max_age=auth.TTL,
        httponly=True, secure=request.url.scheme == "https",
        samesite="strict", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@app.get("/auth/me")
def me(user: str = Depends(auth.check_session)):
    return {"username": user}


@app.post("/auth/logout")
def logout(request: Request, response: Response, user: str = Depends(auth.check_session)):
    auth.same_origin(request)
    response.delete_cookie("mg_session", path="/", samesite="strict")
    return {"authenticated": False}


@app.get("/api/status")
def status(user: str = Depends(auth.check_session)):
    return {
        **core.stats(),
        "schedule": guardian.schedule_info(),
        "notifications": {
            "ntfy": bool(events.NTFY_URL),
            "discord": bool(events.DISCORD_WEBHOOK),
            "last_error": events.LAST_NOTIFY_ERROR["error"],
        },
    }


@app.get("/api/issues")
def issues(limit: int = 100, user: str = Depends(auth.check_session)):
    return core.list_issues(limit)


@app.get("/api/libraries")
def libraries(user: str = Depends(auth.check_session)):
    return core.library_stats()


@app.post("/api/scan", status_code=202)
def scan(request: Request, user: str = Depends(auth.check_session)):
    auth.same_origin(request)
    if not core.reserve_scan():
        raise HTTPException(409, "Scan already running")
    threading.Thread(target=guardian.run_scan, kwargs={"reserved": True}, daemon=True).start()
    return {"accepted": True}


@app.get("/api/scans")
def scans(limit: int = 20, user: str = Depends(auth.check_session)):
    return core.list_scans(limit)


@app.get("/api/events")
def list_events(limit: int = 50, all: bool = False, user: str = Depends(auth.check_session)):
    return events.list_events(limit, include_acknowledged=all)


@app.post("/api/events/ack")
def acknowledge_events(request: Request, user: str = Depends(auth.check_session)):
    auth.same_origin(request)
    events.acknowledge_all()
    return {"acknowledged": True}


@app.get("/api/space")
def space(user: str = Depends(auth.check_session)):
    return {
        "capacity": cleanup.capacity(),
        "downloads": [str(root) for root in cleanup.DOWNLOAD_ROOTS],
        "volumes_match": cleanup.volumes_match(),
        "cleanup": cleanup.summary(),
    }


@app.get("/api/findings")
def findings(kind: str | None = None, limit: int = 200, user: str = Depends(auth.check_session)):
    if kind is not None and kind not in ALL_KINDS:
        raise HTTPException(400, "Unknown finding kind")
    return cleanup.list_findings(kind, limit)


@app.get("/api/playback")
def playback_status(user: str = Depends(auth.check_session)):
    return {
        "languages": sorted(playback.LANGUAGES),
        "summary": cleanup.group_summary(playback.KINDS),
    }


@app.get("/api/muted")
def muted(user: str = Depends(auth.check_session)):
    return [{**row, "label": ALL_KINDS.get(row["kind"], row["kind"])} for row in cleanup.list_muted()]


@app.post("/api/findings/mute")
def mute(body: MuteBody, request: Request, user: str = Depends(auth.check_session)):
    auth.same_origin(request)
    if body.kind not in ALL_KINDS:
        raise HTTPException(400, "Unknown finding kind")
    cleanup.mute(body.kind, body.path)
    return {"muted": True}


@app.post("/api/findings/unmute")
def unmute(body: MuteBody, request: Request, user: str = Depends(auth.check_session)):
    auth.same_origin(request)
    cleanup.unmute(body.kind, body.path)
    return {"muted": False}


@app.get("/api/consistency")
def consistency(user: str = Depends(auth.check_session)):
    return crosscheck.snapshot()


@app.post("/api/consistency/run", status_code=202)
def run_consistency(request: Request, user: str = Depends(auth.check_session)):
    auth.same_origin(request)
    if crosscheck.RUN_LOCK.locked():
        raise HTTPException(409, "Check already running")
    threading.Thread(target=guardian.run_crosscheck, daemon=True).start()
    return {"accepted": True}


@app.get("/api/stack")
def stack_status(user: str = Depends(auth.check_session)):
    return stack.snapshot()


@app.get("/")
def dashboard():
    return FileResponse(
        Path(__file__).with_name("dashboard.html"),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
