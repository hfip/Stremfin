"""Stremfin application entrypoint."""
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from app.config import get_settings
from app.api.jellyfin import router as jellyfin_router
from app.services.dashboard_auth import credentials_match, dashboard_authenticated, make_cookie
from app.services.metadata import MetadataService
from app.services.settings_store import AppSettings, SettingsStore

settings = get_settings(); store = SettingsStore(settings.database_path)
app = FastAPI(title=settings.app_name, version=settings.app_version, description="Jellyfin-compatible bridge for Stremio and debrid streams")
app.include_router(jellyfin_router)


def _login_page(): return FileResponse(Path(__file__).with_name("login.html"))


@app.get("/")
@app.get("/dashboard")
async def dashboard(request: Request):
    return FileResponse(Path(__file__).with_name("dashboard.html")) if await dashboard_authenticated(request, settings) else RedirectResponse("/login", status_code=303)


@app.get("/login")
async def login_page(): return _login_page()


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if not credentials_match(settings, body.get("username", ""), body.get("password", "")): return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
    response = JSONResponse({"ok": True}); response.set_cookie("stremfin_admin", make_cookie(settings, settings.dashboard_username), httponly=True, samesite="lax", max_age=43200)
    return response


@app.post("/api/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303); response.delete_cookie("stremfin_admin"); return response


async def _require_dashboard(request: Request):
    if not await dashboard_authenticated(request, settings): return JSONResponse({"detail": "Dashboard authentication required"}, status_code=401)


@app.get("/api/settings", response_model=AppSettings)
async def get_dashboard_settings(request: Request):
    denied = await _require_dashboard(request)
    if denied: return denied
    return store.load()


@app.put("/api/settings", response_model=AppSettings)
async def save_dashboard_settings(request: Request, payload: AppSettings):
    denied = await _require_dashboard(request)
    if denied: return denied
    return store.save(payload)


@app.get("/api/catalog/{kind}")
async def catalog(kind: str, limit: int = 20):
    configured = store.load(); runtime = settings.model_copy(update={"tmdb_api_key": configured.tmdb_api_key or settings.tmdb_api_key})
    return {"Items": await MetadataService(runtime).catalog(kind, min(limit, 100))}


@app.get("/api/metadata/{kind}/{item_id}")
async def metadata(kind: str, item_id: str):
    configured = store.load(); runtime = settings.model_copy(update={"tmdb_api_key": configured.tmdb_api_key or settings.tmdb_api_key})
    return await MetadataService(runtime).details(item_id, kind)


@app.get("/health")
async def health() -> dict: return {"status": "ok", "service": settings.app_name, "version": settings.app_version}
