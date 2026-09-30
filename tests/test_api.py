from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_and_system_info():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/System/Info/Public").json()["ProductName"] == "Stremfin"


def test_authentication_and_user_profile():
    response = client.post(
        "/Users/AuthenticateByName",
        json={"Username": "demo", "Pw": "anything"},
    )
    assert response.status_code == 200 and response.json()["AccessToken"]
    user_id = response.json()["User"]["Id"]
    assert client.get(f"/Users/{user_id}").json()["Id"] == user_id


def test_views_and_items(monkeypatch):
    import app.api.jellyfin as jellyfin

    # A deterministic no-selection setup exposes the two default views.
    monkeypatch.setattr(
        jellyfin,
        "_selected_catalogs",
        lambda saved: [],
    )

    async def catalog_page(self, kind, start_index, limit, selected):
        items = (
            [
                {
                    "id": "tt-live",
                    "imdb_id": "tt-live",
                    "name": "Live Movie",
                    "type": "Movie",
                    "overview": "",
                    "year": 2024,
                    "poster": "https://img.example/live.jpg",
                    "backdrop": None,
                    "runtime": 120,
                    "videos": [],
                }
            ]
            if kind == "movie"
            else []
        )
        return {
            "items": items[start_index : start_index + limit],
            "has_more": False,
            "total_record_count": len(items),
            "is_total_exact": True,
        }

    monkeypatch.setattr(jellyfin.MetadataService, "catalog_page", catalog_page)
    views = client.get("/Users/stremfin-user/Views")
    assert views.status_code == 200
    assert {item["Name"] for item in views.json()["Items"]} == {
        "Movies",
        "TV Shows",
    }

    items = client.get("/Items", params={"IncludeItemTypes": "Movie"})
    assert items.status_code == 200
    assert items.json()["Items"][0]["Id"] == "tt-live"


def test_stream_requires_a_live_addon_result(monkeypatch):
    import app.api.jellyfin as jellyfin

    async def no_stream(
        self,
        item_id,
        season=None,
        episode=None,
        media_source_id=None,
    ):
        return None

    monkeypatch.setattr(
        jellyfin.PlaybackResolver,
        "first_playable_url",
        no_stream,
    )
    response = client.get(
        "/Videos/tt-live/stream",
        follow_redirects=False,
    )
    assert response.status_code == 404
