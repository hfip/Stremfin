"""Minimal HMAC-signed cookie authentication for the local admin dashboard."""
from datetime import datetime, timedelta, timezone
import base64, hashlib, hmac, secrets
from fastapi import Request
from app.config import Settings


def _secret(settings: Settings) -> bytes:
    return (settings.dashboard_session_secret or settings.server_id).encode()


def make_cookie(settings: Settings, username: str) -> str:
    expires = int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp())
    payload = f"{username}:{expires}".encode()
    signature = hmac.new(_secret(settings), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + signature).decode()


def valid_cookie(settings: Settings, token: str | None) -> bool:
    if not token: return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()); payload, signature = raw.rsplit(b".", 1); username, expires = payload.decode().split(":", 1)
        expected = hmac.new(_secret(settings), payload, hashlib.sha256).digest()
        return bool(username) and hmac.compare_digest(signature, expected) and int(expires) > int(datetime.now(timezone.utc).timestamp())
    except (ValueError, TypeError, base64.binascii.Error): return False


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    return secrets.compare_digest(username, settings.dashboard_username) and secrets.compare_digest(password, settings.dashboard_password)


async def dashboard_authenticated(request: Request, settings: Settings) -> bool:
    return valid_cookie(settings, request.cookies.get("stremfin_admin"))
