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


def validate_config():
    if not USERNAME or len(PASSWORD) < 12 or len(SECRET) < 32:
        raise RuntimeError("Set MG_USERNAME, MG_PASSWORD (12+ characters), and MG_SESSION_SECRET (32+ characters)")


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
