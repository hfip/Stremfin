"""Stremfin application entrypoint and dashboard management API."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.api.jellyfin import router as jellyfin_router
from app.config import get_settings
from app.services.dashboard_auth import (
    credentials_match,
    dashboard_authenticated,
    make_cookie,
)
from app.services.metadata import MetadataService
from app.services.settings_store import AppSettings, SettingsStore


settings = get_settings()
store = SettingsStore(settings.database_path)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Jellyfin-compatible bridge for live Stremio addons",
)
app.include_router(jellyfin_router)

_MANIFEST_CACHE_TTL = 300.0
_manifest_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _runtime():
    saved = store.load()
    return settings.model_copy(
        update={
            "stremio_addon_urls": ",".join(saved.stream_addon_urls),
            "subtitle_addon_urls": ",".join(saved.subtitle_addon_urls),
        }
    )


def _normalize_manifest_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    value = value.rstrip("/")
    if not value.lower().endswith("/manifest.json"):
        value += "/manifest.json"
    return value


def _addon_base_url(manifest_url: str) -> str:
    if manifest_url.lower().endswith("/manifest.json"):
        return manifest_url[: -len("/manifest.json")]
    return manifest_url.rstrip("/")


def _manifest_capabilities(manifest: dict[str, Any]) -> dict[str, Any]:
    resources = manifest.get("resources") or []
    resource_names: list[str] = []

    for resource in resources:
        if isinstance(resource, str):
            resource_names.append(resource)
        elif isinstance(resource, dict):
            name = resource.get("name")
            if name:
                resource_names.append(str(name))

    types = [str(item) for item in (manifest.get("types") or []) if item]
    catalogs = manifest.get("catalogs") or []

    return {
        "resources": list(dict.fromkeys(resource_names)),
        "types": list(dict.fromkeys(types)),
        "catalog_count": len(catalogs) if isinstance(catalogs, list) else 0,
        "supports_streams": "stream" in resource_names,
        "supports_subtitles": "subtitles" in resource_names,
        "supports_catalogs": bool(catalogs) or "catalog" in resource_names,
        "supports_meta": "meta" in resource_names,
    }


async def _inspect_manifest(raw_url: str, *, force: bool = False) -> dict[str, Any]:
    manifest_url = _normalize_manifest_url(raw_url)
    if not manifest_url:
        return {
            "ok": False,
            "online": False,
            "url": raw_url,
            "error": "Invalid manifest URL",
        }

    now = time.monotonic()
    cached = _manifest_cache.get(manifest_url)
    if cached and not force and now - cached[0] < _MANIFEST_CACHE_TTL:
        return {**cached[1], "cached": True}

    started = time.perf_counter()
    timeout = httpx.Timeout(
        connect=min(float(settings.request_timeout_seconds), 8.0),
        read=min(float(settings.request_timeout_seconds), 12.0),
        write=8.0,
        pool=8.0,
    )

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": f"{settings.app_name}/{settings.app_version}",
            },
        ) as client:
            response = await client.get(manifest_url)

        latency_ms = round((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            result = {
                "ok": False,
                "online": False,
                "url": manifest_url,
                "base_url": _addon_base_url(manifest_url),
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "error": f"Manifest returned HTTP {response.status_code}",
            }
            _manifest_cache[manifest_url] = (now, result)
            return result

        try:
            manifest = response.json()
        except ValueError:
            result = {
                "ok": False,
                "online": True,
                "url": manifest_url,
                "base_url": _addon_base_url(manifest_url),
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "error": "Manifest response is not valid JSON",
            }
            _manifest_cache[manifest_url] = (now, result)
            return result

        if not isinstance(manifest, dict):
            result = {
                "ok": False,
                "online": True,
                "url": manifest_url,
                "base_url": _addon_base_url(manifest_url),
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "error": "Manifest JSON must be an object",
            }
            _manifest_cache[manifest_url] = (now, result)
            return result

        name = str(manifest.get("name") or "").strip()
        addon_id = str(manifest.get("id") or "").strip()
        version = str(manifest.get("version") or "").strip()

        if not name and not addon_id:
            result = {
                "ok": False,
                "online": True,
                "url": manifest_url,
                "base_url": _addon_base_url(manifest_url),
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "error": "Response does not look like a Stremio manifest",
            }
            _manifest_cache[manifest_url] = (now, result)
            return result

        result = {
            "ok": True,
            "online": True,
            "url": manifest_url,
            "base_url": _addon_base_url(manifest_url),
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "name": name or addon_id,
            "id": addon_id,
            "version": version,
            "description": str(manifest.get("description") or "").strip(),
            "logo": manifest.get("logo"),
            "background": manifest.get("background"),
            **_manifest_capabilities(manifest),
        }
        _manifest_cache[manifest_url] = (now, result)
        return result

    except httpx.TimeoutException:
        result = {
            "ok": False,
            "online": False,
            "url": manifest_url,
            "base_url": _addon_base_url(manifest_url),
            "error": "Manifest request timed out",
        }
    except httpx.HTTPError as exc:
        result = {
            "ok": False,
            "online": False,
            "url": manifest_url,
            "base_url": _addon_base_url(manifest_url),
            "error": f"Manifest request failed: {exc.__class__.__name__}",
        }
    except Exception as exc:
        result = {
            "ok": False,
            "online": False,
            "url": manifest_url,
            "base_url": _addon_base_url(manifest_url),
            "error": f"Manifest inspection failed: {exc.__class__.__name__}",
        }

    _manifest_cache[manifest_url] = (now, result)
    return result


@app.get("/")
@app.get("/dashboard")
async def dashboard(request: Request):
    if await dashboard_authenticated(request, settings):
        return FileResponse(Path(__file__).with_name("dashboard.html"))
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
async def login_page():
    return FileResponse(Path(__file__).with_name("login.html"))


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if not credentials_match(
        settings,
        body.get("username", ""),
        body.get("password", ""),
    ):
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401)

    response = JSONResponse({"ok": True})
    response.set_cookie(
        "stremfin_admin",
        make_cookie(settings, settings.dashboard_username),
        httponly=True,
        samesite="lax",
        max_age=43200,
    )
    return response


@app.post("/api/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("stremfin_admin")
    return response


async def _require(request: Request):
    if not await dashboard_authenticated(request, settings):
        return JSONResponse(
            {"detail": "Dashboard authentication required"},
            status_code=401,
        )
    return None


@app.get("/api/settings", response_model=AppSettings)
async def get_dashboard_settings(request: Request):
    if (denied := await _require(request)):
        return denied
    return store.load()


@app.put("/api/settings", response_model=AppSettings)
async def save_dashboard_settings(request: Request, payload: AppSettings):
    if (denied := await _require(request)):
        return denied
    return store.save(payload)


@app.get("/api/addons/inspect")
async def inspect_addon(request: Request, url: str, force: bool = False):
    """Validate one Stremio manifest and return dashboard-friendly metadata."""
    if (denied := await _require(request)):
        return denied
    return await _inspect_manifest(url, force=force)


@app.post("/api/addons/inspect")
async def inspect_addon_post(request: Request):
    """POST variant useful for manifest URLs that are inconvenient in query strings."""
    if (denied := await _require(request)):
        return denied

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON body"}, status_code=400)

    url = str(body.get("url") or "")
    force = bool(body.get("force", False))
    return await _inspect_manifest(url, force=force)


@app.get("/api/addons/status")
async def addons_status(request: Request, force: bool = False):
    """Inspect all configured stream/subtitle addons concurrently."""
    if (denied := await _require(request)):
        return denied

    saved = store.load()
    entries: list[tuple[str, int, str]] = []

    for index, url in enumerate(saved.stream_addon_urls):
        entries.append(("stream", index, url))
    for index, url in enumerate(saved.subtitle_addon_urls):
        entries.append(("subtitle", index, url))

    if not entries:
        return {
            "ok": True,
            "total": 0,
            "online": 0,
            "offline": 0,
            "addons": [],
        }

    results = await asyncio.gather(
        *(_inspect_manifest(url, force=force) for _, _, url in entries),
        return_exceptions=True,
    )

    addons: list[dict[str, Any]] = []
    for (kind, index, url), result in zip(entries, results):
        if isinstance(result, Exception):
            item = {
                "ok": False,
                "online": False,
                "url": url,
                "error": result.__class__.__name__,
            }
        else:
            item = dict(result)

        item["kind"] = kind
        item["priority"] = index + 1
        addons.append(item)

    online = sum(1 for item in addons if item.get("online"))
    return {
        "ok": True,
        "total": len(addons),
        "online": online,
        "offline": len(addons) - online,
        "addons": addons,
    }


@app.get("/api/server/status")
async def server_status(request: Request):
    """Detailed local server state for the dashboard."""
    if (denied := await _require(request)):
        return denied

    saved = store.load()
    db_path = Path(settings.database_path)

    return {
        "ok": True,
        "status": "online",
        "service": settings.app_name,
        "version": settings.app_version,
        "server_name": settings.server_name,
        "server_id": settings.server_id,
        "public_base_url": settings.public_base_url,
        "database": {
            "path": str(db_path),
            "exists": db_path.exists(),
        },
        "counts": {
            "stream_addons": len(saved.stream_addon_urls),
            "subtitle_addons": len(saved.subtitle_addon_urls),
            "selected_catalogs": len(saved.selected_catalogs),
        },
    }


@app.get("/api/addons/catalogs")
async def discover_catalogs(request: Request):
    if (denied := await _require(request)):
        return denied
    return {"catalogs": await MetadataService(_runtime()).manifests()}


@app.put("/api/addons/catalogs")
async def save_catalogs(request: Request, catalogs: list[dict]):
    if (denied := await _require(request)):
        return denied

    current = store.load()
    current.selected_catalogs = catalogs
    store.save(current)
    return {"catalogs": catalogs}


@app.get("/api/catalog/{kind}")
async def catalog(kind: str, limit: int = 20):
    saved = store.load()
    return {
        "Items": await MetadataService(_runtime()).catalog(
            kind,
            min(limit, 100),
            saved.selected_catalogs,
        )
    }


@app.get("/api/metadata/{kind}/{item_id}")
async def metadata(kind: str, item_id: str):
    return await MetadataService(_runtime()).details(
        item_id,
        kind,
        _runtime().addon_urls,
    )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
    }
