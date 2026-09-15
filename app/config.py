"""Environment-backed application settings."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Stremfin"
    app_version: str = "0.2.0"
    server_name: str = "Stremfin Jellyfin Bridge"
    server_id: str = "stremfin-local"
    public_base_url: str = "http://localhost:3000"
    stremio_addon_url: str | None = None
    stremio_addon_urls: str = ""
    debrid_provider: str = "none"
    real_debrid_api_key: str | None = None
    torbox_api_key: str | None = None
    tmdb_api_key: str | None = None
    database_path: str = "./data/stremfin.db"
    fallback_stream_url: str = "https://storage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
    request_timeout_seconds: float = 20.0
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def addon_urls(self) -> list[str]:
        values = [self.stremio_addon_url] if self.stremio_addon_url else []
        values.extend(item.strip() for item in self.stremio_addon_urls.split(","))
        return [item.rstrip("/") for item in values if item.strip()]


@lru_cache
def get_settings() -> Settings: return Settings()
