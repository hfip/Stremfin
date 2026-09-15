from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_and_system_info():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/System/Info/Public").json()["ProductName"] == "Stremfin"


def test_authentication_and_user_profile():
    response = client.post("/Users/AuthenticateByName", json={"Username": "demo", "Pw": "anything"})
    assert response.status_code == 200 and response.json()["AccessToken"]
    user_id = response.json()["User"]["Id"]
    assert client.get(f"/Users/{user_id}").json()["Id"] == user_id


def test_views_and_items(monkeypatch):
    async def catalog(self, kind, limit, selected):
        return [{"id":"tt-live","imdb_id":"tt-live","name":"Live Movie","type":"Movie","overview":"","year":2024,"poster":"https://img/live.jpg","backdrop":None,"runtime":120,"videos":[]}] if kind == "movie" else []
    monkeypatch.setattr("app.api.jellyfin.MetadataService.catalog", catalog)
    assert {item["Name"] for item in client.get("/Users/stremfin-user/Views").json()["Items"]} == {"Movies", "TV Shows"}
    items = client.get("/Items", params={"IncludeItemTypes":"Movie"})
    assert items.status_code == 200 and items.json()["Items"][0]["Id"] == "tt-live"


def test_stream_requires_a_live_addon_result(monkeypatch):
    async def no_stream(self, item_id, season=None, episode=None): return []
    monkeypatch.setattr("app.api.jellyfin.StremioResolver.resolve", no_stream)
    assert client.get("/Videos/tt-live/stream", follow_redirects=False).status_code == 404
