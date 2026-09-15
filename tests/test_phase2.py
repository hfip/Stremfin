from pathlib import Path
from fastapi.testclient import TestClient
from app.config import get_settings
from app.main import app, store

client = TestClient(app)


def test_dashboard_and_settings_persist(tmp_path):
    assert client.get("/").status_code == 200
    client.post("/api/login", json={"username": "admin", "password": "admin"})
    original = store.path
    store.path = Path(tmp_path) / "settings.db"
    store._init_db()
    payload = {"debrid_provider": "torbox", "debrid_api_key": "secret", "stremio_addon_urls": ["https://example.test"], "tmdb_api_key": "tmdb", "preferred_resolutions": ["4K"], "preferred_audio_formats": ["DTS"]}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    assert client.get("/api/settings").json()["debrid_provider"] == "torbox"
    store.path = original


def test_catalog_route_returns_normalized_items(monkeypatch):
    async def fake_catalog(self, kind, limit=20, selected=None):
        return [{"id": "tt1", "imdb_id": "tt1", "name": "Real Title", "type": "Series", "overview": "A story", "year": 2025, "poster": "https://img/p.jpg", "backdrop": "https://img/b.jpg"}]
    monkeypatch.setattr("app.main.MetadataService.catalog", fake_catalog)
    payload = client.get("/api/catalog/series").json()
    assert payload["Items"][0]["name"] == "Real Title"


def test_episode_id_is_accepted(monkeypatch):
    async def no_stream(self, item_id, season=None, episode=None):
        assert item_id == "tt1" and season == 1 and episode == 2
        return []
    monkeypatch.setattr("app.api.jellyfin.StremioResolver.resolve", no_stream)
    response = client.get("/Videos/tt1:s1e2/stream", follow_redirects=False)
    assert response.status_code == 404
