"""Small SQLite settings repository used by the dashboard and API."""
import json, sqlite3
from pathlib import Path
from threading import Lock
from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    debrid_provider: str = "none"
    debrid_api_key: str = ""
    stream_addon_urls: list[str] = Field(default_factory=list)
    subtitle_addon_urls: list[str] = Field(default_factory=list)
    tmdb_api_key: str = ""
    preferred_resolutions: list[str] = Field(default_factory=lambda: ["1080p", "4K"])
    preferred_audio_formats: list[str] = Field(default_factory=lambda: ["EAC3", "AAC", "AC3"])

    @property
    def stremio_addon_urls(self) -> list[str]:
        """Backward-compatible alias for Phase 2 clients."""
        return self.stream_addon_urls


class SettingsStore:
    def __init__(self, path: str):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True); self._lock = Lock(); self._init_db()

    def _connect(self): return sqlite3.connect(self.path)
    def _init_db(self):
        with self._connect() as db: db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def load(self) -> AppSettings:
        with self._lock, self._connect() as db: rows = dict(db.execute("SELECT key,value FROM settings"))
        values = {}
        for key, raw in rows.items():
            try: values[key] = json.loads(raw)
            except json.JSONDecodeError: values[key] = raw
        if "stremio_addon_urls" in values and "stream_addon_urls" not in values: values["stream_addon_urls"] = values.pop("stremio_addon_urls")
        return AppSettings.model_validate(values)

    def save(self, settings: AppSettings) -> AppSettings:
        with self._lock, self._connect() as db:
            db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [(key, json.dumps(value)) for key, value in settings.model_dump().items()])
        return settings
