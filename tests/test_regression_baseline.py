import asyncio

import app.api.jellyfin as jellyfin
from app.services.metadata import MetadataService


def test_canonical_movie_identity_does_not_flip_to_series(monkeypatch):
    item_id = "tt-regression-movie"
    calls = []

    async def fake_movie_lookup(requested_id):
        calls.append(("movie", requested_id))
        return object(), {
            "id": requested_id,
            "imdb_id": requested_id,
            "name": "Regression Movie",
            "type": "Movie",
        }

    async def fake_series_lookup(requested_id):
        calls.append(("series", requested_id))
        return object(), {
            "id": requested_id,
            "imdb_id": requested_id,
            "name": "Wrong Series",
            "type": "Series",
        }

    monkeypatch.setattr(jellyfin, "_lookup_movie", fake_movie_lookup)
    monkeypatch.setattr(jellyfin, "_lookup_series", fake_series_lookup)
    monkeypatch.setitem(jellyfin._ENTITY_KIND_CACHE, item_id, "movie")

    _, meta = asyncio.run(jellyfin._lookup(item_id))

    assert meta["type"] == "Movie"
    assert calls == [("movie", item_id)]


def test_catalog_pagination_continues_beyond_100_and_finishes_exactly(monkeypatch):
    service = object.__new__(MetadataService)
    catalog = {
        "addon_url": "https://addon.test",
        "type": "movie",
        "id": "popular",
    }
    all_items = [
        {
            "id": f"tt{index:04d}",
            "imdb_id": f"tt{index:04d}",
            "name": f"Movie {index}",
            "type": "Movie",
        }
        for index in range(140)
    ]
    requested_skips = []

    async def fake_cached_catalog_page(catalog_arg, skip, use_skip):
        requested_skips.append((skip, use_skip))
        start = skip if use_skip else 0
        return all_items[start : start + 20]

    monkeypatch.setattr(
        service,
        "_cached_catalog_page",
        fake_cached_catalog_page,
    )

    selected = [catalog]

    page_after_100 = asyncio.run(
        service.catalog_page(
            kind="movie",
            start_index=100,
            limit=30,
            selected=selected,
        )
    )

    assert len(page_after_100["items"]) == 30
    assert page_after_100["items"][0]["id"] == "tt0100"
    assert page_after_100["items"][-1]["id"] == "tt0129"
    assert page_after_100["has_more"] is True
    assert any(skip >= 100 and use_skip for skip, use_skip in requested_skips)

    final_page = asyncio.run(
        service.catalog_page(
            kind="movie",
            start_index=130,
            limit=30,
            selected=selected,
        )
    )

    assert len(final_page["items"]) == 10
    assert final_page["items"][0]["id"] == "tt0130"
    assert final_page["items"][-1]["id"] == "tt0139"
    assert final_page["has_more"] is False
    assert final_page["is_total_exact"] is True
    assert final_page["total_record_count"] == 140


def test_episode_playback_keeps_all_sources_identity_and_subtitles(monkeypatch):
    item_id = "tt-regression-series:s1e2"
    runtime = object()

    async def fake_validate_playable_item(requested_id):
        assert requested_id == item_id
        return (
            runtime,
            {
                "_stremfin_entity": "episode",
                "_series_meta": {
                    "id": "tt-regression-series",
                    "type": "Series",
                },
                "_video": {
                    "season": 1,
                    "episode": 2,
                },
            },
            "tt-regression-series",
            1,
            2,
        )

    class FakePlaybackResolver:
        def __init__(self, settings):
            assert settings is runtime

        async def playback_info(self, content_id, season, episode):
            assert (content_id, season, episode) == (
                "tt-regression-series",
                1,
                2,
            )
            return {
                "MediaSources": [
                    {
                        "Id": "source-a",
                        "Name": "Source A",
                        "MediaStreams": [
                            {"Index": 0, "Type": "Video", "Codec": "h264"}
                        ],
                    },
                    {
                        "Id": "source-b",
                        "Name": "Source B",
                        "MediaStreams": [],
                    },
                ],
                "PlaySessionId": "regression-session",
                "ErrorCode": None,
            }

    async def fake_subtitle_streams(settings, content_id, season, episode):
        assert settings is runtime
        assert (content_id, season, episode) == (
            "tt-regression-series",
            1,
            2,
        )
        return [
            {
                "Index": 0,
                "Type": "Subtitle",
                "Codec": "srt",
                "Language": "eng",
                "Title": "English",
                "IsExternal": True,
                "DeliveryUrl": (
                    "/Subtitles/tt-regression-series:s1e2/0/Stream.srt"
                ),
            }
        ]

    monkeypatch.setattr(
        jellyfin,
        "_validate_playable_item",
        fake_validate_playable_item,
    )
    monkeypatch.setattr(jellyfin, "PlaybackResolver", FakePlaybackResolver)
    monkeypatch.setattr(
        jellyfin,
        "_subtitle_streams",
        fake_subtitle_streams,
    )

    result = asyncio.run(
        jellyfin._playback_response(
            item_id,
            user_id="regression-external-user",
        )
    )

    sources = result["MediaSources"]

    assert len(sources) == 2
    assert sources[0]["Id"] == item_id
    assert sources[0]["ETag"] == item_id
    assert sources[1]["Id"] == "source-b"
    assert sources[1]["ETag"] == "source-b"

    for source in sources:
        subtitle_tracks = [
            stream
            for stream in source["MediaStreams"]
            if stream.get("Type") == "Subtitle"
        ]
        assert len(subtitle_tracks) == 1
        assert subtitle_tracks[0]["IsExternal"] is True
        assert subtitle_tracks[0]["DeliveryUrl"].endswith(
            "/tt-regression-series:s1e2/0/Stream.srt"
        )


def test_series_and_season_are_not_playable(monkeypatch):
    async def fake_lookup_series(item_id):
        return object(), {
            "id": item_id,
            "imdb_id": item_id,
            "name": "Regression Series",
            "type": "Series",
            "videos": [
                {
                    "id": f"{item_id}:s1e1",
                    "season": 1,
                    "episode": 1,
                }
            ],
        }

    monkeypatch.setattr(jellyfin, "_lookup_series", fake_lookup_series)
    monkeypatch.setitem(
        jellyfin._ENTITY_KIND_CACHE,
        "tt-regression-series",
        "series",
    )

    try:
        asyncio.run(
            jellyfin._validate_playable_item("tt-regression-series")
        )
    except jellyfin.HTTPException as exc:
        assert exc.status_code == 400
        assert "Series" in str(exc.detail)
    else:
        raise AssertionError("Series unexpectedly became playable")

    try:
        asyncio.run(
            jellyfin._validate_playable_item("tt-regression-series:s1")
        )
    except jellyfin.HTTPException as exc:
        assert exc.status_code == 400
        assert "Season" in str(exc.detail)
    else:
        raise AssertionError("Season unexpectedly became playable")
