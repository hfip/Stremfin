"""Stremfin application entrypoint."""
from fastapi import FastAPI
from app.config import get_settings
from app.api.jellyfin import router as jellyfin_router

settings = get_settings()
app = FastAPI(title=settings.app_name, version=settings.app_version, description="Jellyfin-compatible bridge for Stremio and debrid streams")
app.include_router(jellyfin_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "version": settings.app_version}
