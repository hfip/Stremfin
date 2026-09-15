import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.services.cache import AsyncTTLCache

client = TestClient(app)


def test_exact_series_hierarchy_dtos(monkeypatch):
    async def details(self, item_id, kind, addon_urls):
        return {"id": item_id, "name": "Live Series", "type": "Series", "videos": [{"id": "tt-live:s2e3", "name": "Episode", "season": 2, "episode": 3, "runtime": "42"}]}
    monkeypatch.setattr("app.api.jellyfin.MetadataService.details", details)
    season = client.get('/Shows/tt-live/Seasons').json()['Items'][0]
    episode = client.get('/Shows/tt-live/Episodes', params={'Season': 2}).json()['Items'][0]
    assert season["Type"] == "Season" and season["IsFolder"] is True and season["MediaType"] == "Unknown"
    assert season["ParentId"] == "tt-live" and season["SeriesName"] == "Live Series" and season["UserData"]["Played"] is False
    assert episode["Type"] == "Episode" and episode["MediaType"] == "Video" and episode["SeasonId"] == "tt-live:s2"
    assert episode["ParentId"] == "tt-live:s2" and episode["ParentIndexNumber"] == 2 and episode["IndexNumber"] == 3
    assert episode["RunTimeTicks"] == 25200000000 and episode["EnableMediaSourceDisplay"] is True and episode["MediaSources"]


def test_async_ttl_cache_coalesces_requests():
    cache = AsyncTTLCache(ttl_seconds=30)
    calls = 0
    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"live": True}
    async def run():
        values = await asyncio.gather(*(cache.get_or_set("same", load) for _ in range(5)))
        return values
    values = asyncio.run(run())
    assert calls == 1 and all(value == {"live": True} for value in values)
