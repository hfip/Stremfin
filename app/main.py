"""Stremfin application entrypoint."""
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from app.config import get_settings
from app.api.jellyfin import router as jellyfin_router
from app.services.metadata import MetadataService
from app.services.settings_store import AppSettings, SettingsStore

settings = get_settings()
store = SettingsStore(settings.database_path)
app = FastAPI(title=settings.app_name, version=settings.app_version, description="Jellyfin-compatible bridge for Stremio and debrid streams")
app.include_router(jellyfin_router)


@app.get("/")
@app.get("/dashboard")
async def dashboard():
    return FileResponse(Path(__file__).with_name("dashboard.html"))


@app.get("/api/settings", response_model=AppSettings)
async def get_dashboard_settings():
    return store.load()


@app.put("/api/settings", response_model=AppSettings)
async def save_dashboard_settings(payload: AppSettings):
    return store.save(payload)


@app.get("/api/catalog/{kind}")
async def catalog(kind: str, limit: int = 20):
    configured = store.load()
    runtime = settings.model_copy(update={"tmdb_api_key": configured.tmdb_api_key or settings.tmdb_api_key})
    return {"Items": await MetadataService(runtime).catalog(kind, min(limit, 100))}


@app.get("/api/metadata/{kind}/{item_id}")
async def metadata(kind: str, item_id: str):
    configured = store.load()
    runtime = settings.model_copy(update={"tmdb_api_key": configured.tmdb_api_key or settings.tmdb_api_key})
    return await MetadataService(runtime).details(item_id, kind)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "version": settings.app_version}
