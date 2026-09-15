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
app = FastAPI(title=settings.app_name, version=settings.app_version, description="Jellyfin-compatible bridge for live Stremio addons")
app.include_router(jellyfin_router)


def _runtime():
    saved = store.load()
    return settings.model_copy(update={"stremio_addon_urls": ",".join(saved.stream_addon_urls), "subtitle_addon_urls": ",".join(saved.subtitle_addon_urls)})


@app.get("/")
@app.get("/dashboard")
async def dashboard(request: Request): return FileResponse(Path(__file__).with_name("dashboard.html")) if await dashboard_authenticated(request, settings) else RedirectResponse("/login", status_code=303)
@app.get("/login")
async def login_page(): return FileResponse(Path(__file__).with_name("login.html"))
@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if not credentials_match(settings, body.get("username", ""), body.get("password", "")): return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
    response = JSONResponse({"ok": True}); response.set_cookie("stremfin_admin", make_cookie(settings, settings.dashboard_username), httponly=True, samesite="lax", max_age=43200); return response
@app.post("/api/logout")
async def logout(): response = RedirectResponse("/login", status_code=303); response.delete_cookie("stremfin_admin"); return response


async def _require(request: Request):
    if not await dashboard_authenticated(request, settings): return JSONResponse({"detail": "Dashboard authentication required"}, status_code=401)


@app.get("/api/settings", response_model=AppSettings)
async def get_dashboard_settings(request: Request):
    if (denied := await _require(request)): return denied
    return store.load()
@app.put("/api/settings", response_model=AppSettings)
async def save_dashboard_settings(request: Request, payload: AppSettings):
    if (denied := await _require(request)): return denied
    return store.save(payload)
@app.get("/api/addons/catalogs")
async def discover_catalogs(request: Request):
    if (denied := await _require(request)): return denied
    return {"catalogs": await MetadataService(_runtime()).manifests()}
@app.put("/api/addons/catalogs")
async def save_catalogs(request: Request, catalogs: list[dict]):
    if (denied := await _require(request)): return denied
    current = store.load(); current.selected_catalogs = catalogs; store.save(current); return {"catalogs": catalogs}


@app.get("/api/catalog/{kind}")
async def catalog(kind: str, limit: int = 20):
    saved = store.load(); return {"Items": await MetadataService(_runtime()).catalog(kind, min(limit, 100), saved.selected_catalogs)}
@app.get("/api/metadata/{kind}/{item_id}")
async def metadata(kind: str, item_id: str): return await MetadataService(_runtime()).details(item_id, kind, _runtime().addon_urls)
@app.get("/health")
async def health() -> dict: return {"status": "ok", "service": settings.app_name, "version": settings.app_version}
