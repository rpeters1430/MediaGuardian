import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import auth, core, events, guardian, stack


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
    title="MediaGuardian", version="0.3.0", lifespan=lifespan,
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


@app.get("/api/stack")
def stack_status(user: str = Depends(auth.check_session)):
    return stack.snapshot()


@app.get("/")
def dashboard():
    return FileResponse(
        Path(__file__).with_name("dashboard.html"),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
