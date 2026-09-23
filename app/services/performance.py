"""Lightweight request performance metrics for Stremfin.

This module is instrumentation-only. It measures selected Jellyfin/Emby
requests and does not alter responses, cache behavior, playback, or metadata.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request

logger = logging.getLogger("stremfin.performance")

_INTERESTING_PATH_PARTS = (
    "/PlaybackInfo",
    "/MediaSegments/",
)

_INTERESTING_PREFIXES = (
    "/Items/",
    "/emby/Items/",
    "/Users/",
    "/emby/Users/",
)


def _should_measure(path: str) -> bool:
    return (
        any(part in path for part in _INTERESTING_PATH_PARTS)
        or path.startswith(_INTERESTING_PREFIXES)
    )


def install_performance_metrics(app: FastAPI) -> None:
    """Install lightweight request timing middleware."""

    @app.middleware("http")
    async def performance_metrics(request: Request, call_next):
        path = request.url.path

        if not _should_measure(path):
            return await call_next(request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "[PERF] %s %s status=ERROR total=%.2fms",
                request.method,
                path,
                duration_ms,
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "[PERF] %s %s status=%s total=%.2fms",
            request.method,
            path,
            response.status_code,
            duration_ms,
        )
        return response
