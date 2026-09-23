"""Read-only performance instrumentation for Stremfin.

Measures the user-visible Jellyfin/Emby request pipeline without changing
responses, playback, metadata, subtitles, or cache behavior.

Security: query strings and headers are intentionally never logged.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock

from fastapi import FastAPI, Request

# Reuse Uvicorn's configured INFO logger so metrics are visible in Docker logs.
logger = logging.getLogger("uvicorn.error")


@dataclass
class _ImageStats:
    count: int = 0
    total_ms: float = 0.0
    slowest_ms: float = 0.0
    first_ms: float | None = None
    window_started: float = field(default_factory=time.monotonic)


_image_stats: dict[str, _ImageStats] = defaultdict(_ImageStats)
_image_lock = Lock()
_IMAGE_SUMMARY_WINDOW_SECONDS = 5.0


def _category(method: str, path: str) -> str | None:
    lower = path.lower()

    if "/playbackinfo" in lower:
        return "PLAYBACK_INFO"

    if "/mediasegments/" in lower:
        return "MEDIA_SEGMENTS"

    if "/subtitles/" in lower or lower.startswith("/emby/subtitles/"):
        return "SUBTITLE"

    if "/images/" in lower:
        return "IMAGE"

    if "/shows/nextup" in lower:
        return "NEXT_UP"

    if "/resumeitems" in lower:
        return "RESUME"

    if lower.startswith("/api/catalog/"):
        return "CATALOG"

    if lower.startswith("/api/metadata/"):
        return "METADATA"

    if lower.startswith("/items") or lower.startswith("/emby/items"):
        return "ITEMS"

    if "/items/" in lower and (
        lower.startswith("/users/") or lower.startswith("/emby/users/")
    ):
        return "USER_ITEM"

    if lower in {
        "/system/info/public",
        "/emby/system/info/public",
        "/system/info",
        "/emby/system/info",
    }:
        return "SERVER_INFO"

    if "authenticatebyname" in lower:
        return "AUTH"

    return None


def _image_group(path: str) -> str:
    lower = path.lower()
    if "/backdrop" in lower:
        return "Backdrop"
    if "/primary" in lower:
        return "Primary"
    if "/thumb" in lower:
        return "Thumb"
    if "/logo" in lower:
        return "Logo"
    return "Other"


def _log_image_summary(path: str, duration_ms: float) -> None:
    group = _image_group(path)
    now = time.monotonic()

    with _image_lock:
        stats = _image_stats[group]
        if stats.first_ms is None:
            stats.first_ms = duration_ms

        stats.count += 1
        stats.total_ms += duration_ms
        stats.slowest_ms = max(stats.slowest_ms, duration_ms)

        elapsed = now - stats.window_started
        if elapsed < _IMAGE_SUMMARY_WINDOW_SECONDS:
            return

        average_ms = stats.total_ms / stats.count if stats.count else 0.0
        logger.info(
            "[PERF] IMAGE_SUMMARY type=%s count=%d first=%.2fms avg=%.2fms slowest=%.2fms window=%.1fs",
            group,
            stats.count,
            stats.first_ms or 0.0,
            average_ms,
            stats.slowest_ms,
            elapsed,
        )
        _image_stats[group] = _ImageStats()


def install_performance_metrics(app: FastAPI) -> None:
    """Install safe request timing middleware."""

    @app.middleware("http")
    async def performance_metrics(request: Request, call_next):
        path = request.url.path
        category = _category(request.method, path)

        if category is None:
            return await call_next(request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "[PERF] %s method=%s path=%s status=ERROR total=%.2fms",
                category,
                request.method,
                path,
                duration_ms,
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000

        if category == "IMAGE":
            _log_image_summary(path, duration_ms)
        else:
            logger.info(
                "[PERF] %s method=%s path=%s status=%s total=%.2fms",
                category,
                request.method,
                path,
                response.status_code,
                duration_ms,
            )

        return response
