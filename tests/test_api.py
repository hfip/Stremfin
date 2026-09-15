from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_and_system_info():
    assert client.get("/health").json()["status"] == "ok"
    response = client.get("/System/Info/Public")
    assert response.status_code == 200
    assert response.json()["ProductName"] == "Stremfin"


def test_authentication_and_user_profile():
    response = client.post("/Users/AuthenticateByName", json={"Username": "demo", "Pw": "anything"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["AccessToken"]
    user_id = payload["User"]["Id"]
    profile = client.get(f"/Users/{user_id}")
    assert profile.status_code == 200
    assert profile.json()["Id"] == user_id


def test_views_and_items():
    views = client.get("/Users/stremfin-user/Views")
    assert views.status_code == 200
    assert {item["Name"] for item in views.json()["Items"]} == {"Movies", "TV Shows"}
    items = client.get("/Items", params={"IncludeItemTypes": "Movie"})
    assert items.status_code == 200
    assert items.json()["Items"][0]["Type"] == "Movie"


def test_stream_redirect_uses_fallback():
    response = client.get("/Videos/tt0000002/stream", follow_redirects=False)
    assert response.status_code == 302
    assert "BigBuckBunny.mp4" in response.headers["location"]
