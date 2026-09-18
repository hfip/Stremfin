"""Jellyfin-compatible routes backed exclusively by live Stremio addon data."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.config import Settings, get_settings
from app.services.metadata import MetadataService
from app.services.playback import PlaybackResolver
from app.services.settings_store import SettingsStore
from app.services.subtitles import SubtitleResolver


router = APIRouter()

USER_ID = "stremfin-user"
TOKENS: set[str] = set()

MOVIES_VIEW_ID = "movies"
TVSHOWS_VIEW_ID = "tvshows"

MAX_PAGE_SIZE = 100
MAX_CATALOG_WINDOW = 500
DEFAULT_PAGE_SIZE = 20

_EPISODE_ID_RE = re.compile(r"^(?P<series>.+):s(?P<season>\d+)e(?P<episode>\d+)$")
_SEASON_ID_RE = re.compile(r"^(?P<series>.+):s(?P<season>\d+)$")


# ---------------------------------------------------------------------------
# Runtime / configuration
# ---------------------------------------------------------------------------


def _runtime(settings: Settings):
    saved = SettingsStore(settings.database_path).load()

    runtime = settings.model_copy(
        update={
            "stremio_addon_urls": ",".join(saved.stream_addon_urls),
            "subtitle_addon_urls": ",".join(saved.subtitle_addon_urls),
        }
    )

    return runtime, saved


def _server_info(settings: Settings) -> dict[str, Any]:
    return {
        "LocalAddress": settings.public_base_url,
        "ServerName": settings.server_name,
        "Version": settings.app_version,
        "ProductName": "Stremfin",
        "Id": settings.server_id,
        "StartupWizardCompleted": True,
        "OperatingSystem": "Linux",
    }


def _userdata() -> dict[str, Any]:
    return {
        "PlaybackPositionTicks": 0,
        "PlayCount": 0,
        "IsFavorite": False,
        "Played": False,
        "UnplayedItemCount": 0,
    }


def _runtime_ticks(value: Any, default: int = 72_000_000_000) -> int:
    try:
        if value is None:
            return default

        return int(float(str(value).split()[0]) * 600_000_000)

    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def _parse_season_id(item_id: str) -> tuple[str, int] | None:
    match = _SEASON_ID_RE.match(item_id)

    if not match:
        return None

    return match.group("series"), int(match.group("season"))


def _parse_episode_id(item_id: str) -> tuple[str, int, int] | None:
    match = _EPISODE_ID_RE.match(item_id)

    if not match:
        return None

    return (
        match.group("series"),
        int(match.group("season")),
        int(match.group("episode")),
    )


def _season_id(series_id: str, season_number: int) -> str:
    return f"{series_id}:s{season_number}"


def _episode_id(
    series_id: str,
    season_number: int,
    episode_number: int,
) -> str:
    return f"{series_id}:s{season_number}e{episode_number}"


# ---------------------------------------------------------------------------
# DTO helpers
# ---------------------------------------------------------------------------


def _media_source(
    item_id: str,
    name: str | None,
) -> dict[str, Any]:
    return {
        "Id": item_id,
        "Name": name or item_id,
        "Path": f"/Videos/{item_id}/stream",
        "Protocol": "Http",
        "Type": "Default",
        "Container": None,
        "Size": None,
        "Bitrate": None,
        "SupportsDirectPlay": True,
        "SupportsDirectStream": True,
        "SupportsTranscoding": False,
        "IsRemote": True,
        "ReadAtNativeFramerate": False,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsProbing": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsFmp4Transcoding": False,
        "MediaStreams": [],
    }


def _item(meta: dict[str, Any], collection: str) -> dict[str, Any]:
    item_id = meta.get("id") or meta.get("imdb_id")

    if not item_id:
        raise ValueError("Metadata item has no stable id")

    raw_type = str(meta.get("type") or "").lower()
    is_series = raw_type == "series"

    dto: dict[str, Any] = {
        "Name": meta.get("name") or item_id,
        "OriginalTitle": meta.get("name") or item_id,
        "ServerId": "stremfin",
        "Id": item_id,
        "Etag": f"{item_id}-stremfin",
        "Type": "Series" if is_series else "Movie",
        "CollectionType": collection,
        "IsFolder": is_series,
        "MediaType": "Unknown" if is_series else "Video",
        "RunTimeTicks": (
            None
            if is_series
            else _runtime_ticks(meta.get("runtime"))
        ),
        "ProductionYear": meta.get("year"),
        "Overview": meta.get("overview") or "",
        "ImageTags": {},
        "BackdropImageTags": [],
        "LocationType": "Remote",
        "CanDelete": False,
        "CanDownload": not is_series,
        "PlayAccess": "Full",
        "EnableMediaSourceDisplay": not is_series,
        "UserData": _userdata(),
        "ProviderIds": (
            {"Imdb": meta.get("imdb_id")}
            if meta.get("imdb_id")
            else {}
        ),
        "MediaStreams": [],
        "MediaSources": (
            []
            if is_series
            else [_media_source(item_id, meta.get("name"))]
        ),
    }

    if meta.get("poster"):
        dto.update(
            {
                "PrimaryImageTag": "live",
                "ImageTags": {
                    "Primary": "live",
                },
                "ImageSources": [
                    {
                        "Type": "Primary",
                        "Url": f"/Items/{item_id}/Images/Primary",
                    }
                ],
            }
        )

    if meta.get("backdrop"):
        dto.update(
            {
                "BackdropImageTags": ["live"],
                "BackdropImageSources": [
                    {
                        "Type": "Backdrop",
                        "Url": f"/Items/{item_id}/Images/Backdrop",
                    }
                ],
            }
        )

    return dto


def _season_dto(
    series_id: str,
    series_name: str | None,
    number: int,
) -> dict[str, Any]:
    season_id = _season_id(series_id, number)

    return {
        "Name": f"Season {number}",
        "ServerId": "stremfin",
        "Id": season_id,
        "Etag": f"{season_id}-stremfin",
        "Type": "Season",
        "IsFolder": True,
        "MediaType": "Unknown",
        "LocationType": "Remote",
        "ParentId": series_id,
        "SeriesId": series_id,
        "SeriesName": series_name or "",
        "IndexNumber": int(number),
        "SortName": f"{int(number):04d}",
        "ImageTags": {},
        "BackdropImageTags": [],
        "ProviderIds": {},
        "UserData": _userdata(),
        "CanDelete": False,
        "CanDownload": False,
        "PlayAccess": "Full",
    }


def _episode_dto(
    series_id: str,
    series_name: str | None,
    video: dict[str, Any],
) -> dict[str, Any]:
    try:
        season = int(video.get("season"))
        episode = int(video.get("episode"))

    except (TypeError, ValueError):
        raise ValueError("Episode is missing season or episode number")

    season_id = _season_id(series_id, season)

    # We intentionally generate our own deterministic episode id.
    #
    # Some Stremio addons return video["id"] values with different formats.
    # VidHub / Infuse need stable relationships between:
    #
    # SeriesId
    # SeasonId
    # ParentId
    # Episode Id
    #
    # so Stremfin owns the Jellyfin-facing identifier.
    episode_id = _episode_id(
        series_id,
        season,
        episode,
    )

    name = (
        video.get("name")
        or video.get("title")
        or f"Episode {episode}"
    )

    return {
        "Name": name,
        "OriginalTitle": name,
        "ServerId": "stremfin",
        "Id": episode_id,
        "Etag": f"{episode_id}-stremfin",
        "Type": "Episode",
        "IsFolder": False,
        "MediaType": "Video",
        "LocationType": "Remote",
        "SeriesId": series_id,
        "SeriesName": series_name or "",
        "SeasonId": season_id,
        "ParentId": season_id,
        "ParentIndexNumber": season,
        "IndexNumber": episode,
        "SortName": f"{season:04d}-{episode:04d}",
        "Overview": (
            video.get("overview")
            or video.get("description")
            or ""
        ),
        "RunTimeTicks": _runtime_ticks(video.get("runtime")),
        "EnableMediaSourceDisplay": True,
        "CanDelete": False,
        "CanDownload": True,
        "PlayAccess": "Full",
        "ImageTags": {},
        "BackdropImageTags": [],
        "ProviderIds": {},
        "UserData": _userdata(),
        "MediaStreams": [],
        "MediaSources": [
            _media_source(
                episode_id,
                name,
            )
        ],
    }


# ---------------------------------------------------------------------------
# Pagination / filtering helpers
# ---------------------------------------------------------------------------


def _normalize_page(
    start_index: int,
    limit: int,
) -> tuple[int, int]:
    start_index = max(0, start_index)
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    return start_index, limit


def _paginate(
    values: list[Any],
    start_index: int,
    limit: int,
) -> list[Any]:
    end_index = start_index + limit
    return values[start_index:end_index]


def _parse_item_types(value: str | None) -> set[str]:
    if not value:
        return set()

    return {
        part.strip().lower()
        for part in value.split(",")
        if part.strip()
    }


def _catalog_kind(
    parent_id: str | None,
    include_item_types: str | None,
) -> tuple[str, str]:
    requested = _parse_item_types(include_item_types)

    if parent_id == TVSHOWS_VIEW_ID:
        return "series", TVSHOWS_VIEW_ID

    if parent_id == MOVIES_VIEW_ID:
        return "movie", MOVIES_VIEW_ID

    has_series = "series" in requested
    has_movie = "movie" in requested

    if has_series and not has_movie:
        return "series", TVSHOWS_VIEW_ID

    if has_movie and not has_series:
        return "movie", MOVIES_VIEW_ID

    # Jellyfin clients commonly perform generic /Items requests.
    # Preserve the existing Movies default for compatibility.
    return "movie", MOVIES_VIEW_ID


def _deduplicate_metas(
    metas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}

    for meta in metas:
        item_id = meta.get("id") or meta.get("imdb_id")

        if not item_id:
            continue

        key = str(item_id)

        if key not in unique:
            unique[key] = meta

    return list(unique.values())


def _video_matches_season(
    video: dict[str, Any],
    season_number: int,
) -> bool:
    try:
        return int(video.get("season")) == season_number
    except (TypeError, ValueError):
        return False


def _sort_episodes(
    videos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    def sort_key(video: dict[str, Any]):
        try:
            season = int(video.get("season"))
        except (TypeError, ValueError):
            season = 0

        try:
            episode = int(video.get("episode"))
        except (TypeError, ValueError):
            episode = 0

        return season, episode

    return sorted(videos, key=sort_key)


# ---------------------------------------------------------------------------
# Views / authentication
# ---------------------------------------------------------------------------


@router.get("/emby/System/Info")
@router.get("/System/Info")
async def system_info(
    settings: Settings = Depends(get_settings),
):
    return _server_info(settings)


@router.get("/emby/System/Info/Public")
@router.get("/System/Info/Public")
async def public_system_info(
    settings: Settings = Depends(get_settings),
):
    return {
        "LocalAddress": settings.public_base_url,
        "ServerName": settings.server_name,
        "Version": settings.app_version,
        "ProductName": "Stremfin",
        "Id": settings.server_id,
    }


@router.post("/Users/AuthenticateByName")
@router.post("/emby/Users/AuthenticateByName")
async def authenticate(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    body = await request.json()

    username = (
        body.get("Username")
        or body.get("username")
        or "stremfin"
    )

    token = uuid4().hex
    TOKENS.add(token)

    return {
        "User": {
            "Name": username,
            "ServerId": settings.server_id,
            "Id": USER_ID,
            "HasPassword": False,
            "HasConfiguredPassword": False,
            "Configuration": {
                "PlayDefaultAudioTrack": True,
                "SubtitleMode": "Default",
            },
        },
        "SessionInfo": {
            "Id": uuid4().hex,
            "UserId": USER_ID,
            "UserName": username,
            "Client": "Stremfin",
            "DeviceName": "Stremfin",
            "DeviceId": "stremfin-client",
            "ApplicationVersion": settings.app_version,
            "IsActive": True,
            "PlayState": {},
        },
        "AccessToken": token,
        "ServerId": settings.server_id,
    }


@router.get("/emby/Users/{user_id}")
@router.get("/Users/{user_id}")
async def get_user(user_id: str):
    if user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    return {
        "Name": "stremfin",
        "ServerId": "stremfin",
        "Id": USER_ID,
        "HasPassword": False,
        "HasConfiguredPassword": False,
        "EnableAutoLogin": True,
        "Policy": {
            "IsAdministrator": True,
            "EnableAllFolders": True,
            "EnableRemoteAccess": True,
        },
    }


def _views():
    return {
        "Items": [
            {
                "Name": "Movies",
                "ServerId": "stremfin",
                "Id": MOVIES_VIEW_ID,
                "Type": "CollectionFolder",
                "CollectionType": "movies",
                "IsFolder": True,
                "MediaType": "Unknown",
                "LocationType": "Virtual",
                "ImageTags": {},
                "BackdropImageTags": [],
                "UserData": _userdata(),
            },
            {
                "Name": "TV Shows",
                "ServerId": "stremfin",
                "Id": TVSHOWS_VIEW_ID,
                "Type": "CollectionFolder",
                "CollectionType": "tvshows",
                "IsFolder": True,
                "MediaType": "Unknown",
                "LocationType": "Virtual",
                "ImageTags": {},
                "BackdropImageTags": [],
                "UserData": _userdata(),
            },
        ],
        "TotalRecordCount": 2,
        "StartIndex": 0,
    }


@router.get("/emby/Users/{user_id}/Views")
@router.get("/Users/{user_id}/Views")
async def get_views(user_id: str):
    if user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    return _views()


@router.get("/emby/UserViews")
@router.get("/UserViews")
async def user_views():
    return _views()


@router.get("/emby/Users/{user_id}/GroupingOptions")
@router.get("/Users/{user_id}/GroupingOptions")
async def grouping_options(user_id: str):
    """
    Jellyfin/Emby compatibility endpoint used by Infuse during library setup.

    Stremfin exposes Movies and TV Shows as fixed virtual views and does not
    currently offer alternate grouping modes, so an empty list is the correct
    non-error response.
    """
    if user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    return []


# ---------------------------------------------------------------------------
# Metadata lookup
# ---------------------------------------------------------------------------


async def _lookup_series(series_id: str):
    runtime, _ = _runtime(get_settings())

    service = MetadataService(runtime)

    meta = await service.details(
        series_id,
        "series",
        runtime.addon_urls,
    )

    return runtime, meta


async def _lookup_movie(movie_id: str):
    runtime, _ = _runtime(get_settings())

    service = MetadataService(runtime)

    meta = await service.details(
        movie_id,
        "movie",
        runtime.addon_urls,
    )

    return runtime, meta


async def _lookup(item_id: str):
    episode_parts = _parse_episode_id(item_id)

    if episode_parts:
        series_id, season_number, episode_number = episode_parts

        runtime, meta = await _lookup_series(series_id)

        if not meta:
            return runtime, None

        for video in meta.get("videos", []):
            try:
                video_season = int(video.get("season"))
                video_episode = int(video.get("episode"))
            except (TypeError, ValueError):
                continue

            if (
                video_season == season_number
                and video_episode == episode_number
            ):
                return runtime, {
                    "_stremfin_entity": "episode",
                    "_series_meta": meta,
                    "_video": video,
                }

        return runtime, None

    season_parts = _parse_season_id(item_id)

    if season_parts:
        series_id, season_number = season_parts

        runtime, meta = await _lookup_series(series_id)

        if not meta:
            return runtime, None

        exists = any(
            _video_matches_season(video, season_number)
            for video in meta.get("videos", [])
        )

        if not exists:
            return runtime, None

        return runtime, {
            "_stremfin_entity": "season",
            "_series_meta": meta,
            "_season_number": season_number,
        }

    runtime, series_meta = await _lookup_series(item_id)

    if series_meta:
        return runtime, series_meta

    runtime, movie_meta = await _lookup_movie(item_id)

    return runtime, movie_meta


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------


async def _subtitle_streams(
    runtime,
    item_id: str,
    season: int | None = None,
    episode: int | None = None,
):
    tracks = await SubtitleResolver(runtime).resolve(
        item_id,
        season,
        episode,
    )

    return [
        {
            "Index": index,
            "Type": "Subtitle",
            "Language": track.language,
            "DisplayTitle": track.title,
            "IsExternal": True,
            "IsTextSubtitleStream": True,
            "SupportsExternalStream": True,
            "DeliveryMethod": "External",
            "DeliveryUrl": (
                f"/Subtitles/{item_id}/{index}/Stream.{track.format}"
            ),
        }
        for index, track in enumerate(tracks)
    ]


async def _attach_playback_media(
    dto: dict[str, Any],
    runtime,
    content_id: str,
    season: int | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """
    Enrich one playable Item DTO with the same real MediaSources advertised by
    PlaybackInfo.

    This is intentionally used only for individual Movie/Episode detail
    requests. Catalog browsing remains lightweight and never resolves streams.
    """

    try:
        media_sources = await PlaybackResolver(runtime).media_sources(
            content_id,
            season,
            episode,
        )
    except Exception:
        media_sources = []

    try:
        subtitle_streams = await _subtitle_streams(
            runtime,
            content_id,
            season,
            episode,
        )
    except Exception:
        subtitle_streams = []

    for media_source in media_sources:
        existing_streams = list(
            media_source.get("MediaStreams") or []
        )

        next_index = (
            max(
                (
                    int(stream.get("Index", -1))
                    for stream in existing_streams
                    if stream.get("Index") is not None
                ),
                default=-1,
            )
            + 1
        )

        for subtitle_number, subtitle in enumerate(subtitle_streams):
            stream = dict(subtitle)
            stream["Index"] = next_index + subtitle_number
            existing_streams.append(stream)

        media_source["MediaStreams"] = existing_streams

    if media_sources:
        dto["MediaSources"] = media_sources

        # Jellyfin clients may inspect the top-level MediaStreams before
        # opening PlaybackInfo. Mirror the first source's streams there.
        dto["MediaStreams"] = list(
            media_sources[0].get("MediaStreams") or []
        )
    else:
        # Preserve subtitle discovery even if no playable stream was resolved.
        dto["MediaStreams"] = subtitle_streams

    return dto


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@router.get("/emby/Items")
@router.get("/Items")
@router.get("/emby/Users/{user_id}/Items")
@router.get("/Users/{user_id}/Items")
async def get_items(
    user_id: str | None = None,
    parent_id: str | None = Query(
        None,
        alias="ParentId",
    ),
    include_item_types: str | None = Query(
        None,
        alias="IncludeItemTypes",
    ),
    start_index: int = Query(
        0,
        alias="StartIndex",
        ge=0,
    ),
    limit: int = Query(
        DEFAULT_PAGE_SIZE,
        alias="Limit",
        ge=1,
    ),
):
    if user_id and user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    start_index, limit = _normalize_page(
        start_index,
        limit,
    )

    runtime, saved = _runtime(get_settings())
    service = MetadataService(runtime)

    # ------------------------------------------------------------------
    # Season -> Episodes
    # ------------------------------------------------------------------

    if parent_id:
        season_parts = _parse_season_id(parent_id)

        if season_parts:
            series_id, season_number = season_parts

            meta = await service.details(
                series_id,
                "series",
                runtime.addon_urls,
            )

            if not meta:
                raise HTTPException(
                    status_code=404,
                    detail="Series not found in configured addons",
                )

            videos = [
                video
                for video in meta.get("videos", [])
                if _video_matches_season(
                    video,
                    season_number,
                )
            ]

            videos = _sort_episodes(videos)

            episodes = [
                _episode_dto(
                    series_id,
                    meta.get("name"),
                    video,
                )
                for video in videos
                if video.get("season") is not None
                and video.get("episode") is not None
            ]

            page = _paginate(
                episodes,
                start_index,
                limit,
            )

            return {
                "Items": page,
                "TotalRecordCount": len(episodes),
                "StartIndex": start_index,
            }

    # ------------------------------------------------------------------
    # Series -> Seasons
    # ------------------------------------------------------------------

    if (
        parent_id
        and parent_id not in (
            MOVIES_VIEW_ID,
            TVSHOWS_VIEW_ID,
        )
    ):
        meta = await service.details(
            parent_id,
            "series",
            runtime.addon_urls,
        )

        if not meta:
            raise HTTPException(
                status_code=404,
                detail="Series not found in configured addons",
            )

        season_numbers: set[int] = set()

        for video in meta.get("videos", []):
            try:
                season_numbers.add(
                    int(video.get("season"))
                )
            except (TypeError, ValueError):
                continue

        ordered_seasons = sorted(season_numbers)

        seasons = [
            _season_dto(
                parent_id,
                meta.get("name"),
                number,
            )
            for number in ordered_seasons
        ]

        page = _paginate(
            seasons,
            start_index,
            limit,
        )

        return {
            "Items": page,
            "TotalRecordCount": len(seasons),
            "StartIndex": start_index,
        }

    # ------------------------------------------------------------------
    # Root catalog
    # ------------------------------------------------------------------

    kind, collection = _catalog_kind(
        parent_id,
        include_item_types,
    )

    # MetadataService currently accepts a requested item count rather
    # than an offset. Fetch enough data to cover the requested Jellyfin
    # window and perform the final slicing here.
    #
    # This fixes the old behaviour where every StartIndex returned the
    # first page again.
    required_count = min(
        start_index + limit,
        MAX_CATALOG_WINDOW,
    )

    metas = await service.catalog(
        kind,
        required_count,
        saved.selected_catalogs,
    )

    metas = _deduplicate_metas(metas)

    # Strictly isolate Movies and Series even when an upstream addon
    # returns mixed metadata.
    filtered_metas: list[dict[str, Any]] = []

    for meta in metas:
        raw_type = str(meta.get("type") or "").lower()

        if kind == "series":
            if raw_type == "series":
                filtered_metas.append(meta)
        else:
            if raw_type != "series":
                filtered_metas.append(meta)

    items = [
        _item(meta, collection)
        for meta in filtered_metas
    ]

    page = _paginate(
        items,
        start_index,
        limit,
    )

    # IMPORTANT:
    # Do not resolve subtitles here.
    #
    # VidHub/Infuse may request dozens of catalog items at once.
    # Resolving subtitle addons during browsing creates N extra network
    # operations and makes the home screen unnecessarily slow.
    #
    # Subtitle resolution is deferred until an individual media item /
    # playback path requires it.

    return {
        "Items": page,
        "TotalRecordCount": len(items),
        "StartIndex": start_index,
    }


# ---------------------------------------------------------------------------
# Latest items
# ---------------------------------------------------------------------------


@router.get("/emby/Items/Latest")
@router.get("/Items/Latest")
@router.get("/emby/Users/{user_id}/Items/Latest")
@router.get("/Users/{user_id}/Items/Latest")
async def latest_items(
    user_id: str | None = None,
    parent_id: str | None = Query(
        None,
        alias="ParentId",
    ),
    include_item_types: str | None = Query(
        None,
        alias="IncludeItemTypes",
    ),
    limit: int = Query(
        DEFAULT_PAGE_SIZE,
        alias="Limit",
        ge=1,
    ),
):
    if user_id and user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    _, limit = _normalize_page(0, limit)

    runtime, saved = _runtime(get_settings())
    service = MetadataService(runtime)

    kind, collection = _catalog_kind(
        parent_id,
        include_item_types,
    )

    metas = await service.catalog(
        kind,
        limit,
        saved.selected_catalogs,
    )

    metas = _deduplicate_metas(metas)

    filtered_metas: list[dict[str, Any]] = []

    for meta in metas:
        raw_type = str(meta.get("type") or "").lower()

        if kind == "series":
            if raw_type == "series":
                filtered_metas.append(meta)
        else:
            if raw_type != "series":
                filtered_metas.append(meta)

    return [
        _item(meta, collection)
        for meta in filtered_metas[:limit]
    ]


# ---------------------------------------------------------------------------
# Library counts
# ---------------------------------------------------------------------------


@router.get("/Items/Counts")
@router.get("/emby/Items/Counts")
async def item_counts(
    user_id: str | None = Query(
        None,
        alias="UserId",
    ),
):
    if user_id and user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    runtime, saved = _runtime(get_settings())
    service = MetadataService(runtime)

    # Stremio catalogs do not expose a universal Jellyfin-style count
    # endpoint. Ask the metadata layer for the largest supported deterministic
    # window and report only counts that Stremfin can establish from real
    # catalog data. We intentionally do not invent an EpisodeCount.
    movie_metas = await service.catalog(
        "movie",
        MAX_CATALOG_WINDOW,
        saved.selected_catalogs,
    )
    series_metas = await service.catalog(
        "series",
        MAX_CATALOG_WINDOW,
        saved.selected_catalogs,
    )

    movie_metas = _deduplicate_metas(movie_metas)
    series_metas = _deduplicate_metas(series_metas)

    movies = [
        meta
        for meta in movie_metas
        if str(meta.get("type") or "").lower() != "series"
    ]
    series = [
        meta
        for meta in series_metas
        if str(meta.get("type") or "").lower() == "series"
    ]

    movie_count = len(movies)
    series_count = len(series)

    return {
        "MovieCount": movie_count,
        "SeriesCount": series_count,
        "EpisodeCount": 0,
        "ArtistCount": 0,
        "ProgramCount": 0,
        "TrailerCount": 0,
        "SongCount": 0,
        "AlbumCount": 0,
        "MusicVideoCount": 0,
        "BoxSetCount": 0,
        "BookCount": 0,
        "ItemCount": movie_count + series_count,
    }


# ---------------------------------------------------------------------------
# Individual item
# ---------------------------------------------------------------------------


@router.get("/emby/Items/{item_id}")
@router.get("/Items/{item_id}")
@router.get("/emby/Users/{user_id}/Items/{item_id}")
@router.get("/Users/{user_id}/Items/{item_id}")
async def get_item(
    item_id: str,
    user_id: str | None = None,
):
    if user_id and user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    runtime, meta = await _lookup(item_id)

    if not meta:
        raise HTTPException(
            status_code=404,
            detail="Item not found in configured addons",
        )

    entity_type = meta.get("_stremfin_entity")

    if entity_type == "season":
        series_meta = meta["_series_meta"]
        season_number = meta["_season_number"]

        series_id = (
            series_meta.get("id")
            or series_meta.get("imdb_id")
        )

        return _season_dto(
            series_id,
            series_meta.get("name"),
            season_number,
        )

    if entity_type == "episode":
        series_meta = meta["_series_meta"]
        video = meta["_video"]

        series_id = (
            series_meta.get("id")
            or series_meta.get("imdb_id")
        )

        dto = _episode_dto(
            series_id,
            series_meta.get("name"),
            video,
        )

        try:
            season_number = int(video.get("season"))
            episode_number = int(video.get("episode"))
        except (TypeError, ValueError):
            return dto

        return await _attach_playback_media(
            dto,
            runtime,
            series_id,
            season_number,
            episode_number,
        )

    collection = (
        TVSHOWS_VIEW_ID
        if str(meta.get("type") or "").lower() == "series"
        else MOVIES_VIEW_ID
    )

    dto = _item(
        meta,
        collection,
    )

    # Resolve real playback versions and subtitles only for an individual
    # playable item. Catalog browsing remains network-light.
    if dto["Type"] == "Movie":
        content_id = (
            meta.get("id")
            or meta.get("imdb_id")
            or item_id
        )

        return await _attach_playback_media(
            dto,
            runtime,
            str(content_id),
        )

    return dto


# ---------------------------------------------------------------------------
# Series hierarchy
# ---------------------------------------------------------------------------


@router.get("/emby/Shows/{series_id}/Seasons")
@router.get("/Shows/{series_id}/Seasons")
async def seasons(
    series_id: str,
    start_index: int = Query(
        0,
        alias="StartIndex",
        ge=0,
    ),
    limit: int = Query(
        MAX_PAGE_SIZE,
        alias="Limit",
        ge=1,
    ),
):
    start_index, limit = _normalize_page(
        start_index,
        limit,
    )

    runtime, meta = await _lookup_series(series_id)

    if (
        not meta
        or str(meta.get("type") or "").lower() != "series"
    ):
        raise HTTPException(
            status_code=404,
            detail="Series not found in configured addons",
        )

    values: set[int] = set()

    for video in meta.get("videos", []):
        try:
            values.add(int(video.get("season")))
        except (TypeError, ValueError):
            continue

    ordered = sorted(values)

    items = [
        _season_dto(
            series_id,
            meta.get("name"),
            number,
        )
        for number in ordered
    ]

    page = _paginate(
        items,
        start_index,
        limit,
    )

    return {
        "Items": page,
        "TotalRecordCount": len(items),
        "StartIndex": start_index,
    }


@router.get("/emby/Shows/{series_id}/Episodes")
@router.get("/Shows/{series_id}/Episodes")
async def episodes(
    series_id: str,
    season: int | None = Query(
        None,
        alias="Season",
    ),
    season_id: str | None = Query(
        None,
        alias="SeasonId",
    ),
    start_index: int = Query(
        0,
        alias="StartIndex",
        ge=0,
    ),
    limit: int = Query(
        MAX_PAGE_SIZE,
        alias="Limit",
        ge=1,
    ),
):
    start_index, limit = _normalize_page(
        start_index,
        limit,
    )

    runtime, meta = await _lookup_series(series_id)

    if (
        not meta
        or str(meta.get("type") or "").lower() != "series"
    ):
        raise HTTPException(
            status_code=404,
            detail="Series not found in configured addons",
        )

    requested_season = season

    if season_id:
        parsed = _parse_season_id(season_id)

        if parsed:
            parsed_series_id, parsed_season = parsed

            if parsed_series_id != series_id:
                raise HTTPException(
                    status_code=404,
                    detail="Season does not belong to requested series",
                )

            requested_season = parsed_season

    values = list(meta.get("videos", []))

    if requested_season is not None:
        values = [
            video
            for video in values
            if _video_matches_season(
                video,
                requested_season,
            )
        ]

    values = _sort_episodes(values)

    items = [
        _episode_dto(
            series_id,
            meta.get("name"),
            video,
        )
        for video in values
        if video.get("season") is not None
        and video.get("episode") is not None
    ]

    page = _paginate(
        items,
        start_index,
        limit,
    )

    return {
        "Items": page,
        "TotalRecordCount": len(items),
        "StartIndex": start_index,
    }


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


@router.get("/emby/Items/{item_id}/Images/Primary")
@router.get("/Items/{item_id}/Images/Primary")
async def primary_image(item_id: str):
    _, meta = await _lookup(item_id)

    if not meta:
        raise HTTPException(
            status_code=404,
            detail="Image not found in configured addons",
        )

    entity_type = meta.get("_stremfin_entity")

    if entity_type in {"season", "episode"}:
        series_meta = meta["_series_meta"]
        image_url = series_meta.get("poster")
    else:
        image_url = meta.get("poster")

    if not image_url:
        raise HTTPException(
            status_code=404,
            detail="Image not found in configured addons",
        )

    return RedirectResponse(
        image_url,
        status_code=302,
    )


@router.get("/emby/Items/{item_id}/Images/Backdrop")
@router.get("/Items/{item_id}/Images/Backdrop")
async def backdrop_image(item_id: str):
    _, meta = await _lookup(item_id)

    if not meta:
        raise HTTPException(
            status_code=404,
            detail="Image not found in configured addons",
        )

    entity_type = meta.get("_stremfin_entity")

    if entity_type in {"season", "episode"}:
        series_meta = meta["_series_meta"]
        image_url = series_meta.get("backdrop")
    else:
        image_url = meta.get("backdrop")

    if not image_url:
        raise HTTPException(
            status_code=404,
            detail="Image not found in configured addons",
        )

    return RedirectResponse(
        image_url,
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------


@router.get("/emby/Subtitles/{item_id}/{index}/Stream.{format}")
@router.get("/Subtitles/{item_id}/{index}/Stream.{format}")
async def subtitle_stream(
    item_id: str,
    index: int,
    format: str,
):
    episode_parts = _parse_episode_id(item_id)

    if episode_parts:
        content, season, episode = episode_parts
    else:
        content = item_id
        season = None
        episode = None

    runtime, _ = _runtime(get_settings())

    tracks = await SubtitleResolver(runtime).resolve(
        content,
        season,
        episode,
    )

    if index < 0 or index >= len(tracks):
        raise HTTPException(
            status_code=404,
            detail="Subtitle not found",
        )

    track = tracks[index]

    async with httpx.AsyncClient(
        timeout=runtime.request_timeout_seconds,
        follow_redirects=True,
    ) as client:
        response = await client.get(track.url)
        response.raise_for_status()

    requested_format = format.lower()

    if requested_format == "vtt":
        media_type = "text/vtt"
    elif requested_format in {"srt", "subrip"}:
        media_type = "application/x-subrip"
    else:
        media_type = "text/plain"

    return Response(
        response.content,
        media_type=media_type,
    )


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


def _playback_identity(
    item_id: str,
) -> tuple[str, int | None, int | None]:
    """
    Convert a Jellyfin-facing item id back to the Stremio playback identity.

    Movies use their own id. Episodes use the owning series id plus season /
    episode numbers. Series and Season entities are intentionally rejected by
    the PlaybackInfo routes because they are folders, not playable media.
    """

    episode_parts = _parse_episode_id(item_id)

    if episode_parts:
        series_id, season_number, episode_number = episode_parts
        return series_id, season_number, episode_number

    return item_id, None, None


async def _validate_playable_item(
    item_id: str,
) -> tuple[Any, dict[str, Any], str, int | None, int | None]:
    runtime, meta = await _lookup(item_id)

    if not meta:
        raise HTTPException(
            status_code=404,
            detail="Item not found in configured addons",
        )

    entity_type = meta.get("_stremfin_entity")

    if entity_type == "season":
        raise HTTPException(
            status_code=400,
            detail="Season is not a playable media item",
        )

    if entity_type == "episode":
        series_meta = meta["_series_meta"]
        video = meta["_video"]

        series_id = (
            series_meta.get("id")
            or series_meta.get("imdb_id")
        )

        try:
            season_number = int(video.get("season"))
            episode_number = int(video.get("episode"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=404,
                detail="Episode playback identity is incomplete",
            )

        return (
            runtime,
            meta,
            str(series_id),
            season_number,
            episode_number,
        )

    if str(meta.get("type") or "").lower() == "series":
        raise HTTPException(
            status_code=400,
            detail="Series is not a playable media item",
        )

    content_id = (
        meta.get("id")
        or meta.get("imdb_id")
        or item_id
    )

    return runtime, meta, str(content_id), None, None


async def _playback_response(
    item_id: str,
) -> dict[str, Any]:
    (
        runtime,
        _,
        content_id,
        season_number,
        episode_number,
    ) = await _validate_playable_item(item_id)

    resolver = PlaybackResolver(runtime)

    result = await resolver.playback_info(
        content_id,
        season_number,
        episode_number,
    )

    media_sources = result.get("MediaSources") or []

    # Subtitle discovery is intentionally deferred until PlaybackInfo instead
    # of catalog browsing. This keeps VidHub/Infuse navigation fast while still
    # advertising external subtitle tracks when playback starts.
    try:
        subtitle_streams = await _subtitle_streams(
            runtime,
            content_id,
            season_number,
            episode_number,
        )
    except Exception:
        subtitle_streams = []

    for media_source in media_sources:
        existing_streams = list(
            media_source.get("MediaStreams") or []
        )

        next_index = (
            max(
                (
                    int(stream.get("Index", -1))
                    for stream in existing_streams
                    if stream.get("Index") is not None
                ),
                default=-1,
            )
            + 1
        )

        for subtitle_number, subtitle in enumerate(subtitle_streams):
            stream = dict(subtitle)
            stream["Index"] = next_index + subtitle_number
            existing_streams.append(stream)

        media_source["MediaStreams"] = existing_streams

    if not media_sources:
        # Jellyfin-compatible shape: PlaybackInfo itself is a valid response,
        # but ErrorCode tells the client that no playable source was resolved.
        result["ErrorCode"] = "NoCompatibleStream"

    return result


@router.get("/emby/Items/{item_id}/PlaybackInfo")
@router.get("/Items/{item_id}/PlaybackInfo")
async def playback_info_get(
    item_id: str,
):
    return await _playback_response(item_id)


@router.post("/emby/Items/{item_id}/PlaybackInfo")
@router.post("/Items/{item_id}/PlaybackInfo")
async def playback_info_post(
    item_id: str,
    request: Request,
):
    # Jellyfin clients send DeviceProfile / UserId / MaxStreamingBitrate and
    # other playback preferences in this body. Stremfin currently exposes only
    # direct-play sources, so these values do not alter source resolution yet.
    # Reading the body keeps the route compatible with both empty and standard
    # Jellyfin POST requests without binding to one client-specific schema.
    try:
        await request.json()
    except Exception:
        pass

    return await _playback_response(item_id)


@router.get("/emby/Videos/{item_id}/stream")
@router.get("/Videos/{item_id}/stream")
async def stream(
    item_id: str,
    settings: Settings = Depends(get_settings),
    media_source_id: str | None = Query(
        None,
        alias="MediaSourceId",
    ),
    user_agent: str | None = Header(
        None,
        alias="User-Agent",
    ),
):
    runtime, _ = _runtime(settings)

    episode_parts = _parse_episode_id(item_id)

    if episode_parts:
        content_id, season_number, episode_number = episode_parts
    else:
        # Validate movie ids before resolving. This also prevents Series /
        # Season folders from accidentally being sent to the Stremio stream
        # endpoint as if they were movies.
        (
            runtime,
            _,
            content_id,
            season_number,
            episode_number,
        ) = await _validate_playable_item(item_id)

    resolver = PlaybackResolver(runtime)

    resolved_url = await resolver.first_playable_url(
        content_id,
        season_number,
        episode_number,
        media_source_id=media_source_id,
    )

    if not resolved_url:
        raise HTTPException(
            status_code=404,
            detail="No playable stream found in configured addons",
        )

    return RedirectResponse(
        resolved_url,
        status_code=302,
        headers={
            "X-Stremfin-Item": item_id,
        },
    )
