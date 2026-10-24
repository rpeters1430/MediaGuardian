import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request

USERNAME = os.getenv("MG_USERNAME", "")
PASSWORD = os.getenv("MG_PASSWORD", "")
SECRET = os.getenv("MG_SESSION_SECRET", "")
TTL = 12 * 3600
ATTEMPTS = defaultdict(list)


def config_problems(username, password, secret):
    """Readable reasons the login settings can't be used. Lengths only, never the values."""
    problems = []
    if not username:
        problems.append("MG_USERNAME is empty")
    if len(password) < 12:
        problems.append(f"MG_PASSWORD is {len(password)} characters; it needs at least 12")
    if len(secret) < 32:
        problems.append(f"MG_SESSION_SECRET is {len(secret)} characters; it needs at least 32")
    if len(password) < 12 or len(secret) < 32:
        # Compose has already removed "$name" from the value by the time it gets here.
        problems.append("if the value you set is long enough, it probably contains $; write each $ as $$")
    return problems


def validate_config():
    problems = config_problems(USERNAME, PASSWORD, SECRET)
    if problems:
        raise RuntimeError("Login settings are not usable: " + "; ".join(problems))


def throttle(ip):
    now = time.monotonic()
    attempts = ATTEMPTS[ip]
    attempts[:] = [t for t in attempts if now - t < 300]
    if len(attempts) >= 8:
        raise HTTPException(429, "Too many login attempts. Retry later.")
    attempts.append(now)


def authenticate_login(username, password, ip):
    throttle(ip)
    user_ok = hmac.compare_digest(username.encode(), USERNAME.encode())
    pass_ok = hmac.compare_digest(
        hashlib.sha256(password.encode()).digest(),
        hashlib.sha256(PASSWORD.encode()).digest(),
    )
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid username or password")
    ATTEMPTS.pop(ip, None)


def make_session():
    expires = int(time.time()) + TTL
    nonce = secrets.token_urlsafe(18)
    payload = f"{USERNAME}:{expires}:{nonce}"
    signature = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def check_session(request: Request):
    cookie = request.cookies.get("mg_session", "")
    try:
        username, expires, nonce, signature = cookie.rsplit(":", 3)
        payload = f"{username}:{expires}:{nonce}"
        expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if (
            username != USERNAME
            or int(expires) < time.time()
            or not hmac.compare_digest(signature, expected)
        ):
            raise ValueError("Invalid session")
    except (ValueError, TypeError):
        raise HTTPException(401, "Login required") from None
    return username


def same_origin(request: Request):
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Invalid request origin")
