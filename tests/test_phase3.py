from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_dashboard_requires_login():
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/login").status_code == 200


def test_dashboard_login_protects_settings():
    assert client.get("/api/settings").status_code == 401
    response = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_addon_settings_and_image_routes(monkeypatch):
    client.put("/api/settings", json={"debrid_provider":"none","debrid_api_key":"","stream_addon_urls":["https://stream.example"],"subtitle_addon_urls":["https://sub.example"],"tmdb_api_key":"","preferred_resolutions":["1080p"],"preferred_audio_formats":["AAC"]})
    assert client.get("/api/settings").json()["subtitle_addon_urls"] == ["https://sub.example"]
    async def details(self, item_id, kind):
        return {"id": item_id, "imdb_id": item_id, "name": "Poster Test", "type": "Movie", "poster": "https://img.example/poster.jpg", "backdrop": "https://img.example/backdrop.jpg"}
    monkeypatch.setattr("app.api.jellyfin.MetadataService.details", details)
    assert client.get("/Items/tt1/Images/Primary", follow_redirects=False).headers["location"].endswith("poster.jpg")
    assert client.get("/Items/tt1/Images/Backdrop", follow_redirects=False).headers["location"].endswith("backdrop.jpg")
