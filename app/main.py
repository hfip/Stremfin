"""Stremfin application entrypoint and dashboard management API."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

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


def _normalize_addon_kind(value: str | None) -> str | None:
    if value is None:
        return None
    return {"stream": "stream", "streams": "stream", "subtitle": "subtitle", "subtitles": "subtitle"}.get(value.strip().lower())


def _validate_addon_kind(result: dict[str, Any], kind: str | None) -> dict[str, Any]:
    normalized = _normalize_addon_kind(kind)
    if kind is not None and normalized is None:
        return {**result, "compatible": False, "requested_kind": str(kind), "compatibility_error": "Unsupported addon kind. Use 'stream' or 'subtitle'."}
    if normalized is None:
        return {**result, "compatible": bool(result.get("ok")), "requested_kind": None}
    if not result.get("ok"):
        return {**result, "compatible": False, "requested_kind": normalized}

    if normalized == "stream":
        compatible = bool(
            result.get("supports_streams")
            or result.get("supports_catalogs")
            or result.get("supports_meta")
        )
        message = (
            None
            if compatible
            else "This Stremio addon does not declare stream, catalog, or meta resources."
        )
    else:
        compatible = bool(result.get("supports_subtitles"))
        message = None if compatible else "This Stremio addon does not declare the subtitles resource."

    annotated = {**result, "compatible": compatible, "requested_kind": normalized}
    if message:
        annotated["compatibility_error"] = message
    return annotated


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
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36 "
                    f"Stremfin/{settings.app_version}"
                ),
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
                "inspection_state": "http_error",
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
                "inspection_state": "invalid_manifest",
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
                "inspection_state": "invalid_manifest",
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
                "inspection_state": "invalid_manifest",
                "error": "Response does not look like a Stremio manifest",
            }
            _manifest_cache[manifest_url] = (now, result)
            return result

        result = {
            "ok": True,
            "online": True,
            "inspection_state": "valid",
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
            "inspection_state": "timeout",
            "error": "Manifest request timed out",
        }
    except httpx.HTTPError as exc:
        result = {
            "ok": False,
            "online": False,
            "url": manifest_url,
            "base_url": _addon_base_url(manifest_url),
            "inspection_state": "network_error",
            "error": f"Manifest request failed: {exc.__class__.__name__}",
        }
    except Exception as exc:
        result = {
            "ok": False,
            "online": False,
            "url": manifest_url,
            "base_url": _addon_base_url(manifest_url),
            "inspection_state": "inspection_error",
            "error": f"Manifest inspection failed: {exc.__class__.__name__}",
        }

    _manifest_cache[manifest_url] = (now, result)
    return result


_BACKUP_FORMAT = "stremfin-settings-backup"
_BACKUP_SCHEMA_VERSION = 1


def _backup_document(saved: AppSettings) -> dict[str, Any]:
    """Create a portable settings-only backup. Server secrets are intentionally excluded."""
    return {
        "format": _BACKUP_FORMAT,
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stremfin_version": settings.app_version,
        "settings": saved.model_dump(),
    }


def _parse_backup_document(payload: Any) -> AppSettings:
    """Validate a Stremfin backup before writing anything to SQLite."""
    if not isinstance(payload, dict):
        raise ValueError("Backup must be a JSON object")
    if payload.get("format") != _BACKUP_FORMAT:
        raise ValueError("Unsupported backup format")
    if payload.get("schema_version") != _BACKUP_SCHEMA_VERSION:
        raise ValueError("Unsupported backup schema version")

    raw_settings = payload.get("settings")
    if not isinstance(raw_settings, dict):
        raise ValueError("Backup does not contain settings")

    # Only fields owned by AppSettings are restored. Dashboard/server credentials
    # and other .env-level secrets are never imported through this endpoint.
    allowed = set(AppSettings.model_fields)
    cleaned = {key: value for key, value in raw_settings.items() if key in allowed}
    return AppSettings.model_validate(cleaned)


def _database_diagnostics() -> dict[str, Any]:
    """Read-only SQLite health check without mutating dashboard data."""
    db_path = Path(settings.database_path)
    result: dict[str, Any] = {
        "ok": False,
        "exists": db_path.exists(),
        "path": str(db_path),
        "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }

    if not db_path.exists():
        result["error"] = "Database file does not exist"
        return result

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            result["integrity"] = integrity[0] if integrity else "unknown"
            result["ok"] = result["integrity"] == "ok"
        finally:
            connection.close()
    except sqlite3.Error as exc:
        result["error"] = f"{exc.__class__.__name__}: {exc}"

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


@app.get("/api/settings/backup")
async def backup_dashboard_settings(request: Request):
    """Download a portable JSON backup of dashboard-managed settings."""
    if (denied := await _require(request)):
        return denied

    document = _backup_document(store.load())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"stremfin-backup-{stamp}.json"
    body = json.dumps(document, ensure_ascii=False, indent=2)

    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/settings/restore", response_model=AppSettings)
async def restore_dashboard_settings(request: Request):
    """Validate and restore a Stremfin settings backup atomically."""
    if (denied := await _require(request)):
        return denied

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Backup is not valid JSON"}, status_code=400)

    try:
        restored = _parse_backup_document(payload)
    except Exception as exc:
        return JSONResponse(
            {"detail": f"Invalid Stremfin backup: {exc}"},
            status_code=400,
        )

    saved = store.save(restored)
    _manifest_cache.clear()
    return saved


@app.get("/api/addons/inspect")
async def inspect_addon(request: Request, url: str, force: bool = False, kind: str | None = None):
    """Validate a Stremio manifest and optionally check stream/subtitle compatibility."""
    if (denied := await _require(request)):
        return denied
    return _validate_addon_kind(await _inspect_manifest(url, force=force), kind)


@app.post("/api/addons/inspect")
async def inspect_addon_post(request: Request):
    """POST manifest inspection with optional dashboard-section compatibility validation."""
    if (denied := await _require(request)):
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON body"}, status_code=400)

    url = str(body.get("url") or "")
    force = bool(body.get("force", False))
    kind_value = body.get("kind")
    kind = str(kind_value) if kind_value is not None else None
    return _validate_addon_kind(await _inspect_manifest(url, force=force), kind)


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

        item = _validate_addon_kind(item, kind)
        item["kind"] = kind
        item["priority"] = index + 1
        addons.append(item)

    online = sum(1 for item in addons if item.get("online"))
    incompatible = sum(1 for item in addons if item.get("ok") and item.get("compatible") is False)
    return {
        "ok": True,
        "total": len(addons),
        "online": online,
        "offline": len(addons) - online,
        "incompatible": incompatible,
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


@app.get("/api/server/connection")
async def server_connection(request: Request):
    """Connection Wizard credentials for an authenticated dashboard session."""
    if (denied := await _require(request)):
        return denied

    protected = bool(settings.client_auth_enabled)
    response = JSONResponse(
        {
            "server_url": settings.public_base_url,
            "client_auth_enabled": protected,
            "username": str(settings.client_username or "").strip() or "stremfin",
            "password": str(settings.client_password or "") if protected else "",
            "password_required": protected and bool(str(settings.client_password or "")),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/diagnostics")
async def diagnostics(request: Request, force: bool = True):
    """Run safe read-only diagnostics for the dashboard."""
    if (denied := await _require(request)):
        return denied

    started = time.perf_counter()
    saved = store.load()

    entries: list[tuple[str, int, str]] = []
    for index, url in enumerate(saved.stream_addon_urls):
        entries.append(("stream", index, url))
    for index, url in enumerate(saved.subtitle_addon_urls):
        entries.append(("subtitle", index, url))

    manifest_results = await asyncio.gather(
        *(_inspect_manifest(url, force=force) for _, _, url in entries),
        return_exceptions=True,
    ) if entries else []

    addon_rows: list[dict[str, Any]] = []
    for (kind, index, url), raw in zip(entries, manifest_results):
        if isinstance(raw, Exception):
            item: dict[str, Any] = {
                "ok": False,
                "online": False,
                "url": url,
                "error": raw.__class__.__name__,
            }
        else:
            item = dict(raw)

        item = _validate_addon_kind(item, kind)
        state = str(item.get("inspection_state") or ("valid" if item.get("ok") else "unknown"))
        addon_rows.append({
            "kind": kind,
            "priority": index + 1,
            "name": item.get("name") or urlparse(url).netloc or url,
            "url": item.get("url") or url,
            "online": bool(item.get("online")),
            "valid_manifest": True if item.get("ok") else (False if state == "invalid_manifest" else None),
            "compatible": bool(item.get("compatible")) if item.get("ok") else None,
            "inspection_state": state,
            "status_code": item.get("status_code"),
            "latency_ms": item.get("latency_ms"),
            "supports_streams": bool(item.get("supports_streams")),
            "supports_catalogs": bool(item.get("supports_catalogs")),
            "supports_meta": bool(item.get("supports_meta")),
            "supports_subtitles": bool(item.get("supports_subtitles")),
            "error": item.get("compatibility_error") or item.get("error"),
        })

    online = sum(1 for item in addon_rows if item["online"])
    invalid = sum(1 for item in addon_rows if item["valid_manifest"] is False)
    unreachable = sum(
        1 for item in addon_rows
        if item["inspection_state"] in {"timeout", "network_error", "inspection_error"}
    )
    http_errors = sum(1 for item in addon_rows if item["inspection_state"] == "http_error")
    incompatible = sum(
        1 for item in addon_rows
        if item["valid_manifest"] is True and item["compatible"] is False
    )

    database = _database_diagnostics()
    jellyfin_api = {
        "ok": True,
        "product": "Stremfin",
        "server_name": settings.server_name,
        "server_id": settings.server_id,
        "version": settings.app_version,
        "public_info_endpoint": "/System/Info/Public",
        "emby_public_info_endpoint": "/emby/System/Info/Public",
        "authentication_endpoint": "/Users/AuthenticateByName",
    }

    overall_ok = (
        bool(database.get("ok"))
        and invalid == 0
        and incompatible == 0
        and unreachable == 0
        and http_errors == 0
    )

    return {
        "ok": overall_ok,
        "status": "healthy" if overall_ok else "attention",
        "service": {
            "name": settings.app_name,
            "version": settings.app_version,
            "server_name": settings.server_name,
            "server_id": settings.server_id,
            "public_base_url": settings.public_base_url,
            "process_id": os.getpid(),
        },
        "jellyfin_emby_api": jellyfin_api,
        "database": database,
        "addons": {
            "total": len(addon_rows),
            "online": online,
            "offline": len(addon_rows) - online,
            "invalid": invalid,
            "unreachable": unreachable,
            "http_errors": http_errors,
            "incompatible": incompatible,
            "stream": len(saved.stream_addon_urls),
            "subtitle": len(saved.subtitle_addon_urls),
            "items": addon_rows,
        },
        "catalogs": {
            "selected": len(saved.selected_catalogs),
        },
        "checks": {
            "database": bool(database.get("ok")),
            "jellyfin_emby_api": True,
            "addons_valid": invalid == 0,
            "addons_reachable": unreachable == 0,
            "addons_http_ok": http_errors == 0,
            "addons_compatible": incompatible == 0,
        },
        "duration_ms": round((time.perf_counter() - started) * 1000),
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
