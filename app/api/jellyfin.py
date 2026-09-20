"""Jellyfin-compatible routes backed exclusively by live Stremio addon data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.config import Settings, get_settings
from app.services.client_auth import (
    configured_username,
    credentials_are_valid,
    user_auth_flags,
)
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


def _metadata_etag(*values: Any) -> str:
    payload = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha1(payload).hexdigest()


def _virtual_media_filename(
    name: str | None,
    item_id: str,
) -> str:
    value = str(name or "").strip()

    if not value or value.lower() in {
        "stream",
        "stremio stream",
        "video",
    }:
        value = str(item_id or "media").strip()

    value = re.sub(
        r'[\\/:*?"<>|]+',
        " ",
        value,
    )
    value = re.sub(r"\s+", " ", value).strip(" .")

    if not value:
        value = str(item_id or "media")

    return f"{value}.mkv"


def _virtual_media_path(
    name: str | None,
    item_id: str,
) -> str:
    """
    A display-safe virtual path for Item DTOs.

    Infuse may derive the visible title from Item.Path.  Keep this path free
    from both the old literal "stream" and file extensions; the actual file
    style name remains available separately in FileName.
    """
    filename = _virtual_media_filename(
        name,
        item_id,
    )

    if filename.lower().endswith(".mkv"):
        return filename[:-4]

    return filename


def _media_source(
    item_id: str,
    name: str | None,
) -> dict[str, Any]:
    return {
        "Id": item_id,
        "Name": name or item_id,
        "Path": _virtual_media_path(
            name,
            item_id,
        ),
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


def _display_name(meta: dict[str, Any]) -> str:
    raw = meta.get("raw")
    if not isinstance(raw, dict):
        raw = {}

    candidates = [
        raw.get("title"),
        meta.get("name"),
        raw.get("name"),
        raw.get("originalTitle"),
        raw.get("original_title"),
    ]

    generic = {
        "",
        "stream",
        "stremio stream",
        "video",
        "movie",
        "series",
    }

    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value.lower() not in generic:
            return value

    return str(meta.get("imdb_id") or meta.get("id") or "Unknown")


def _provider_ids(meta: dict[str, Any]) -> dict[str, str]:
    raw = meta.get("raw")
    if not isinstance(raw, dict):
        raw = {}

    raw_provider_ids = (
        raw.get("providerIds")
        or raw.get("provider_ids")
        or raw.get("providerIDs")
        or raw.get("ids")
        or {}
    )
    if not isinstance(raw_provider_ids, dict):
        raw_provider_ids = {}

    def pick(*keys: str) -> str | None:
        for key in keys:
            value = raw_provider_ids.get(key)
            if value not in (None, ""):
                return str(value)

        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                return str(value)

        for key in keys:
            value = meta.get(key)
            if value not in (None, ""):
                return str(value)

        return None

    imdb = pick("Imdb", "IMDb", "IMDB", "imdb", "imdb_id", "imdbId")
    tmdb = pick("Tmdb", "TMDB", "tmdb", "tmdb_id", "tmdbId")
    tvdb = pick("Tvdb", "TVDB", "tvdb", "tvdb_id", "tvdbId")

    result: dict[str, str] = {}

    if imdb:
        result["Imdb"] = imdb
    if tmdb:
        result["Tmdb"] = tmdb
    if tvdb:
        result["Tvdb"] = tvdb

    return result


def _logo_url(meta: dict[str, Any]) -> str | None:
    raw = meta.get("raw")
    if not isinstance(raw, dict):
        raw = {}

    value = (
        meta.get("logo")
        or raw.get("logo")
        or raw.get("logoUrl")
        or raw.get("logo_url")
        or raw.get("clearLogo")
        or raw.get("clearLogoUrl")
        or raw.get("clearlogo")
        or raw.get("clear_logo")
    )

    return str(value).strip() if value else None


def _item(meta: dict[str, Any], collection: str) -> dict[str, Any]:
    item_id = meta.get("id") or meta.get("imdb_id")

    if not item_id:
        raise ValueError("Metadata item has no stable id")

    raw_type = str(meta.get("type") or "").lower()
    is_series = raw_type == "series"
    display_name = _display_name(meta)

    dto: dict[str, Any] = {
        "Name": display_name,
        "OriginalTitle": display_name,
        "SortName": display_name,
        "FileName": (
            None
            if is_series
            else _virtual_media_filename(
                display_name,
                str(item_id),
            )
        ),
        "ServerId": "stremfin",
        "Id": item_id,
        "Etag": _metadata_etag(
            item_id,
            display_name,
            meta.get("year"),
            meta.get("poster"),
            meta.get("backdrop"),
            _logo_url(meta),
            _provider_ids(meta),
        ),
        "Type": "Series" if is_series else "Movie",
        "CollectionType": collection,
        "Path": (
            None
            if is_series
            else _virtual_media_path(
                display_name,
                str(item_id),
            )
        ),
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
        "ProviderIds": _provider_ids(meta),
        "MediaStreams": [],
        "MediaSources": (
            []
            if is_series
            else [_media_source(item_id, display_name)]
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

    logo_url = _logo_url(meta)

    if logo_url:
        image_tags = dict(dto.get("ImageTags") or {})
        image_tags["Logo"] = "live"
        dto["ImageTags"] = image_tags

        image_sources = list(dto.get("ImageSources") or [])
        image_sources.append(
            {
                "Type": "Logo",
                "Url": f"/Items/{item_id}/Images/Logo",
            }
        )
        dto["ImageSources"] = image_sources

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

    generic_episode_names = {
        "",
        "stream",
        "stremio stream",
        "video",
        "episode",
    }

    name = ""

    for candidate in (
        video.get("name"),
        video.get("title"),
    ):
        candidate = str(candidate or "").strip()

        if (
            candidate
            and candidate.lower() not in generic_episode_names
        ):
            name = candidate
            break

    if not name:
        name = f"Episode {episode}"

    return {
        "Name": name,
        "OriginalTitle": name,
        "FileName": _virtual_media_filename(
            name,
            episode_id,
        ),
        "ServerId": "stremfin",
        "Id": episode_id,
        "Etag": _metadata_etag(
            episode_id,
            name,
            video.get("overview"),
            video.get("description"),
            video.get("released"),
            video.get("thumbnail"),
        ),
        "Type": "Episode",
        "IsFolder": False,
        "MediaType": "Video",
        "LocationType": "Remote",
        "Path": _virtual_media_path(
            name,
            episode_id,
        ),
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
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid authentication request",
        )

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail="Invalid authentication request",
        )

    # Jellyfin normally sends Username + Pw. Some Emby-compatible clients use
    # Password/password instead, so accept all common field names.
    username = (
        body.get("Username")
        or body.get("username")
        or "stremfin"
    )
    password = (
        body.get("Pw")
        if body.get("Pw") is not None
        else body.get("Password")
        if body.get("Password") is not None
        else body.get("password")
        if body.get("password") is not None
        else ""
    )

    if not credentials_are_valid(
        settings,
        username,
        password,
    ):
        # Keep the failure generic: never reveal whether the username or the
        # password was the mismatching value.
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
            headers={"Cache-Control": "no-store"},
        )

    # In compatibility mode preserve the client-supplied display name. When
    # protection is enabled, credentials_are_valid() guarantees that this is
    # the configured username.
    authenticated_username = str(username or "").strip() or configured_username(settings)
    auth_flags = user_auth_flags(settings)

    token = uuid4().hex
    TOKENS.add(token)

    return {
        "User": {
            "Name": authenticated_username,
            "ServerId": settings.server_id,
            "Id": USER_ID,
            **auth_flags,
            "Configuration": {
                "PlayDefaultAudioTrack": True,
                "SubtitleMode": "Default",
            },
        },
        "SessionInfo": {
            "Id": uuid4().hex,
            "UserId": USER_ID,
            "UserName": authenticated_username,
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
async def get_user(
    user_id: str,
    settings: Settings = Depends(get_settings),
):
    if user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    auth_flags = user_auth_flags(settings)

    return {
        "Name": configured_username(settings),
        "ServerId": settings.server_id,
        "Id": USER_ID,
        **auth_flags,
        "Policy": {
            "IsAdministrator": True,
            "EnableAllFolders": True,
            "EnableRemoteAccess": True,
        },
    }


def _collection_folder(item_id: str) -> dict[str, Any]:
    if item_id == MOVIES_VIEW_ID:
        name = "Movies"
        collection_type = "movies"
    elif item_id == TVSHOWS_VIEW_ID:
        name = "TV Shows"
        collection_type = "tvshows"
    else:
        raise ValueError("Unknown collection folder")

    return {
        "Name": name,
        "OriginalTitle": name,
        "ServerId": "stremfin",
        "Id": item_id,
        "Etag": f"{item_id}-stremfin",
        "Type": "CollectionFolder",
        "CollectionType": collection_type,
        "IsFolder": True,
        "MediaType": "Unknown",
        "LocationType": "Virtual",
        "Path": item_id,
        "ImageTags": {},
        "BackdropImageTags": [],
        "ProviderIds": {},
        "UserData": _userdata(),
        "CanDelete": False,
        "CanDownload": False,
        "PlayAccess": "Full",
        "ChildCount": 0,
    }


def _views():
    return {
        "Items": [
            _collection_folder(MOVIES_VIEW_ID),
            _collection_folder(TVSHOWS_VIEW_ID),
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
# Library / Emby compatibility
# ---------------------------------------------------------------------------


@router.get("/emby/Library/VirtualFolders")
@router.get("/Library/VirtualFolders")
async def virtual_folders():
    """
    Return Stremfin's two virtual libraries in Jellyfin/Emby VirtualFolder
    shape. Infuse uses this endpoint while adding a server.
    """

    return [
        {
            "Name": "Movies",
            "Locations": [],
            "CollectionType": "movies",
            "LibraryOptions": {
                "Enabled": True,
                "EnablePhotos": False,
                "EnableRealtimeMonitor": False,
                "EnableChapterImageExtraction": False,
                "ExtractChapterImagesDuringLibraryScan": False,
                "EnableInternetProviders": False,
                "SaveLocalMetadata": False,
                "EnableAutomaticSeriesGrouping": False,
                "EnableEmbeddedTitles": False,
                "EnableEmbeddedEpisodeInfos": False,
                "AutomaticRefreshIntervalDays": 0,
                "PreferredMetadataLanguage": None,
                "MetadataCountryCode": None,
                "SeasonZeroDisplayName": "Specials",
                "MetadataSavers": [],
                "DisabledLocalMetadataReaders": [],
                "LocalMetadataReaderOrder": [],
                "DisabledSubtitleFetchers": [],
                "SubtitleFetcherOrder": [],
                "SkipSubtitlesIfEmbeddedSubtitlesPresent": False,
                "SkipSubtitlesIfAudioTrackMatches": False,
                "SubtitleDownloadLanguages": [],
                "RequirePerfectSubtitleMatch": False,
                "SaveSubtitlesWithMedia": False,
                "AutomaticallyAddToCollection": False,
                "AllowEmbeddedSubtitles": "AllowAll",
                "TypeOptions": [],
            },
            "ItemId": MOVIES_VIEW_ID,
            "PrimaryImageItemId": None,
            "RefreshProgress": 0.0,
            "RefreshStatus": "Idle",
        },
        {
            "Name": "TV Shows",
            "Locations": [],
            "CollectionType": "tvshows",
            "LibraryOptions": {
                "Enabled": True,
                "EnablePhotos": False,
                "EnableRealtimeMonitor": False,
                "EnableChapterImageExtraction": False,
                "ExtractChapterImagesDuringLibraryScan": False,
                "EnableInternetProviders": False,
                "SaveLocalMetadata": False,
                "EnableAutomaticSeriesGrouping": False,
                "EnableEmbeddedTitles": False,
                "EnableEmbeddedEpisodeInfos": False,
                "AutomaticRefreshIntervalDays": 0,
                "PreferredMetadataLanguage": None,
                "MetadataCountryCode": None,
                "SeasonZeroDisplayName": "Specials",
                "MetadataSavers": [],
                "DisabledLocalMetadataReaders": [],
                "LocalMetadataReaderOrder": [],
                "DisabledSubtitleFetchers": [],
                "SubtitleFetcherOrder": [],
                "SkipSubtitlesIfEmbeddedSubtitlesPresent": False,
                "SkipSubtitlesIfAudioTrackMatches": False,
                "SubtitleDownloadLanguages": [],
                "RequirePerfectSubtitleMatch": False,
                "SaveSubtitlesWithMedia": False,
                "AutomaticallyAddToCollection": False,
                "AllowEmbeddedSubtitles": "AllowAll",
                "TypeOptions": [],
            },
            "ItemId": TVSHOWS_VIEW_ID,
            "PrimaryImageItemId": None,
            "RefreshProgress": 0.0,
            "RefreshStatus": "Idle",
        },
    ]


@router.get("/emby/System/Ext/ServerDomains")
@router.get("/System/Ext/ServerDomains")
async def server_domains():
    """
    Emby compatibility endpoint used by Rex during server discovery.

    Stremfin does not advertise additional server domains.
    """

    return []


# ---------------------------------------------------------------------------
# Display preferences
# ---------------------------------------------------------------------------


@router.get("/emby/DisplayPreferences/{display_id}")
@router.get("/DisplayPreferences/{display_id}")
async def display_preferences(
    display_id: str,
    user_id: str | None = Query(
        None,
        alias="userId",
    ),
    client: str | None = Query(
        None,
        alias="client",
    ),
):
    """
    Minimal Jellyfin/Emby DisplayPreferences response used by Infuse.

    Stremfin does not persist client-specific presentation settings yet, so we
    return stable defaults instead of inventing user preferences.
    """

    if user_id and user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    return {
        "Id": display_id,
        "ViewType": "Poster",
        "SortBy": "SortName",
        "IndexBy": "None",
        "RememberIndexing": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {},
        "ScrollDirection": "Horizontal",
        "ShowBackdrop": True,
        "RememberSorting": False,
        "SortOrder": "Ascending",
        "ShowSidebar": True,
        "Client": client or "emby",
    }


# ---------------------------------------------------------------------------
# Metadata lookup
# ---------------------------------------------------------------------------


def _metadata_addon_urls(
    runtime,
    saved,
    kind: str | None = None,
) -> list[str]:
    """
    Metadata must come from the addons that own the selected catalogs, not
    only from stream addons.

    The previous implementation queried runtime.addon_urls here. Those URLs
    are the configured stream addons and can legitimately return generic
    stream-oriented metadata such as Name="stream". That contaminated Movie
    and Episode DTOs even though the selected catalog addon knew the real
    title, provider IDs and artwork.

    Preserve selected-catalog order, optionally filtered by media type, then
    keep runtime addon URLs as fallbacks.
    """

    wanted = (
        "series"
        if str(kind or "").lower() in {"series", "tv", "tvshow", "tvshows"}
        else "movie"
        if str(kind or "").lower() in {"movie", "movies", "film", "films"}
        else None
    )

    urls: list[str] = []

    for catalog in saved.selected_catalogs:
        if not isinstance(catalog, dict):
            continue

        catalog_type = str(catalog.get("type") or "").strip().lower()

        if catalog_type in {"tv", "show", "shows", "tvshow", "tvshows"}:
            catalog_type = "series"
        elif catalog_type in {"movies", "film", "films"}:
            catalog_type = "movie"

        if wanted and catalog_type != wanted:
            continue

        value = str(catalog.get("addon_url") or "").strip()
        value = value.removesuffix("/manifest.json").rstrip("/")

        if value and value not in urls:
            urls.append(value)

    for value in runtime.addon_urls:
        value = str(value or "").strip()
        value = value.removesuffix("/manifest.json").rstrip("/")

        if value and value not in urls:
            urls.append(value)

    return urls


async def _lookup_series(series_id: str):
    runtime, saved = _runtime(get_settings())

    service = MetadataService(runtime)

    meta = await service.details(
        series_id,
        "series",
        _metadata_addon_urls(runtime, saved, "series"),
    )

    return runtime, meta


async def _lookup_movie(movie_id: str):
    runtime, saved = _runtime(get_settings())

    service = MetadataService(runtime)

    meta = await service.details(
        movie_id,
        "movie",
        _metadata_addon_urls(runtime, saved, "movie"),
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
    """
    Convert Stremio subtitle candidates to Jellyfin MediaStream DTOs.

    The delivery URL must use the Jellyfin-facing episode id so the subtitle
    endpoint can reconstruct the original series/season/episode identity.
    Movies keep their normal item id.
    """
    tracks = await SubtitleResolver(runtime).resolve(
        item_id,
        season,
        episode,
    )

    if season is not None and episode is not None:
        delivery_item_id = _episode_id(
            item_id,
            int(season),
            int(episode),
        )
    else:
        delivery_item_id = item_id

    streams: list[dict[str, Any]] = []

    for index, track in enumerate(tracks):
        codec = str(track.format or "srt").strip().lower()
        if codec == "subrip":
            codec = "srt"
        elif codec == "webvtt":
            codec = "vtt"

        streams.append(
            {
                "Index": index,
                "Type": "Subtitle",
                "Codec": codec,
                "Language": track.language,
                "Title": track.title,
                "DisplayTitle": track.title,
                "IsExternal": True,
                "IsTextSubtitleStream": True,
                "SupportsExternalStream": True,
                "DeliveryMethod": "External",
                "DeliveryUrl": (
                    f"/Subtitles/{delivery_item_id}/{index}/"
                    f"Stream.{codec}"
                ),
            }
        )

    return streams


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
        # Keep every resolved source/version on the Item DTO.  Some Emby-style
        # clients inspect MediaSources while others explicitly request
        # AlternateMediaSources before deciding whether to show the Versions
        # selector, so advertise both views of the same resolved set.
        dto["MediaSources"] = media_sources
        dto["AlternateMediaSources"] = list(media_sources[1:])

        # Jellyfin clients may inspect the top-level MediaStreams before
        # opening PlaybackInfo. Mirror the first source's streams there.
        dto["MediaStreams"] = list(
            media_sources[0].get("MediaStreams") or []
        )
    else:
        dto["AlternateMediaSources"] = []

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
    request: Request,
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

    # Jellyfin commonly uses PascalCase query names, while Infuse sends
    # lower-camel-case variants such as parentId/includeItemTypes/startIndex.
    # Starlette query keys are case-sensitive, so normalize them here.
    query = {
        str(key).lower(): value
        for key, value in request.query_params.multi_items()
    }

    parent_id = query.get("parentid", parent_id)
    include_item_types = query.get(
        "includeitemtypes",
        include_item_types,
    )

    try:
        start_index = int(query.get("startindex", start_index))
    except (TypeError, ValueError):
        start_index = 0

    try:
        limit = int(query.get("limit", limit))
    except (TypeError, ValueError):
        limit = DEFAULT_PAGE_SIZE

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
                _metadata_addon_urls(runtime, saved, "series"),
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
            _metadata_addon_urls(runtime, saved, "series"),
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
            {
                **_season_dto(
                    parent_id,
                    meta.get("name"),
                    number,
                ),
                "ProviderIds": _provider_ids(meta),
            }
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

    # Use MetadataService's progressive page probe instead of treating the
    # currently loaded window as the complete library. Infuse requests pages
    # such as StartIndex=0/50/100 and relies on TotalRecordCount to decide
    # whether another request is necessary.
    #
    # catalog_page() probes one item beyond the requested window. While more
    # data exists it reports a monotonic lower-bound total; on the final page
    # the total becomes exact. This avoids downloading the whole Stremio
    # catalog on the first Infuse request.
    catalog_result = await service.catalog_page(
        kind=kind,
        start_index=start_index,
        limit=limit,
        selected=saved.selected_catalogs,
    )

    metas = _deduplicate_metas(
        list(catalog_result.get("items") or [])
    )

    catalog_total = int(
        catalog_result.get("total_record_count")
        or (start_index + len(metas))
    )

    # Some Stremio movie catalogs expose a generic preview name such as
    # "stream" even though their /meta/movie/{id}.json endpoint contains the
    # real title. Enrich only those malformed/generic movie entries, and do it
    # concurrently so normal catalog browsing remains fast.
    if kind == "movie":
        generic_names = {
            "",
            "stream",
            "stremio stream",
            "video",
            "movie",
        }

        async def enrich_movie(meta: dict[str, Any]) -> dict[str, Any]:
            raw = meta.get("raw")
            if not isinstance(raw, dict):
                raw = {}

            catalog_name = str(
                meta.get("name")
                or raw.get("name")
                or raw.get("title")
                or ""
            ).strip()

            if catalog_name.lower() not in generic_names:
                return meta

            lookup_ids: list[str] = []

            for value in (
                meta.get("imdb_id"),
                meta.get("id"),
                raw.get("imdb_id"),
                raw.get("imdbId"),
                raw.get("id"),
            ):
                value = str(value or "").strip()
                if value and value not in lookup_ids:
                    lookup_ids.append(value)

            if not lookup_ids:
                return meta

            metadata_addons = _metadata_addon_urls(
                runtime,
                saved,
                "movie",
            )

            for lookup_id in lookup_ids:
                try:
                    detailed = await service.details(
                        lookup_id,
                        "movie",
                        metadata_addons,
                    )
                except Exception:
                    detailed = None

                if not detailed:
                    continue

                detailed_name = str(
                    detailed.get("name") or ""
                ).strip()

                if (
                    detailed_name
                    and detailed_name.lower() not in generic_names
                ):
                    return detailed

            return meta

        metas = list(
            await asyncio.gather(
                *(enrich_movie(meta) for meta in metas)
            )
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

    # `catalog_page()` already applied StartIndex/Limit. Do not paginate this
    # list a second time or StartIndex=50 would incorrectly slice an already
    # sliced 50-item page down to an empty list.
    page = items

    # Keep the advertised total consistent even if an upstream addon returned
    # an unexpected mixed-type entry that was filtered locally.
    catalog_total = max(
        catalog_total,
        start_index + len(page),
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
        "TotalRecordCount": catalog_total,
        "StartIndex": start_index,
    }


# ---------------------------------------------------------------------------
# Resume / Next Up compatibility
# ---------------------------------------------------------------------------


@router.get("/emby/Users/{user_id}/Items/Resume")
@router.get("/Users/{user_id}/Items/Resume")
async def resume_items(
    user_id: str,
    start_index: int = Query(0, alias="StartIndex", ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, alias="Limit", ge=1),
):
    if user_id != USER_ID:
        raise HTTPException(status_code=404, detail="User not found")

    start_index, _ = _normalize_page(start_index, limit)
    return {
        "Items": [],
        "TotalRecordCount": 0,
        "StartIndex": start_index,
    }


@router.get("/emby/Shows/NextUp")
@router.get("/Shows/NextUp")
async def next_up(
    user_id: str | None = Query(None, alias="UserId"),
    start_index: int = Query(0, alias="StartIndex", ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, alias="Limit", ge=1),
):
    if user_id and user_id != USER_ID:
        raise HTTPException(status_code=404, detail="User not found")

    start_index, _ = _normalize_page(start_index, limit)
    return {
        "Items": [],
        "TotalRecordCount": 0,
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
# Local trailers compatibility
# ---------------------------------------------------------------------------


@router.get("/emby/Users/{user_id}/Items/{item_id}/LocalTrailers")
@router.get("/Users/{user_id}/Items/{item_id}/LocalTrailers")
@router.get("/emby/Items/{item_id}/LocalTrailers")
@router.get("/Items/{item_id}/LocalTrailers")
async def local_trailers(
    item_id: str,
    user_id: str | None = None,
):
    if user_id and user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    # Stremfin does not expose local trailer files. A valid empty collection
    # is preferable to 404 and matches clients that treat trailers as optional.
    return []


# ---------------------------------------------------------------------------
# Optional media extras compatibility
# ---------------------------------------------------------------------------


@router.get("/emby/Users/{user_id}/Items/{item_id}/SpecialFeatures")
@router.get("/Users/{user_id}/Items/{item_id}/SpecialFeatures")
async def special_features(
    user_id: str,
    item_id: str,
):
    if user_id != USER_ID:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    return {
        "Items": [],
        "TotalRecordCount": 0,
    }


@router.get("/emby/MediaSegments/{item_id}")
@router.get("/MediaSegments/{item_id}")
async def media_segments(item_id: str):
    # Stremfin currently has no intro/credits/chapter segment database.
    # Return the empty Jellyfin-compatible envelope instead of 404.
    return {
        "Items": [],
        "TotalRecordCount": 0,
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

    if item_id in {MOVIES_VIEW_ID, TVSHOWS_VIEW_ID}:
        return _collection_folder(item_id)

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

        dto = _season_dto(
            series_id,
            series_meta.get("name"),
            season_number,
        )
        dto["ProviderIds"] = _provider_ids(series_meta)
        return dto

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

        # Episode matching benefits from the parent series provider IDs.
        # Overlay any episode-specific IDs returned by the Stremio video.
        episode_meta = {
            "raw": video,
            "imdb_id": video.get("imdb_id") or video.get("imdbId"),
        }
        provider_ids = _provider_ids(series_meta)
        provider_ids.update(_provider_ids(episode_meta))
        dto["ProviderIds"] = provider_ids

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
        {
            **_season_dto(
                series_id,
                meta.get("name"),
                number,
            ),
            "ProviderIds": _provider_ids(meta),
        }
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

    items = []

    for video in values:
        if (
            video.get("season") is None
            or video.get("episode") is None
        ):
            continue

        dto = _episode_dto(
            series_id,
            meta.get("name"),
            video,
        )

        episode_meta = {
            "raw": video,
            "imdb_id": video.get("imdb_id") or video.get("imdbId"),
        }
        provider_ids = _provider_ids(meta)
        provider_ids.update(_provider_ids(episode_meta))
        dto["ProviderIds"] = provider_ids

        items.append(dto)

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
        headers={
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get("/emby/Items/{item_id}/Images/Logo")
@router.get("/Items/{item_id}/Images/Logo")
async def logo_image(item_id: str):
    _, meta = await _lookup(item_id)

    if not meta:
        raise HTTPException(
            status_code=404,
            detail="Logo not found in configured addons",
        )

    entity_type = meta.get("_stremfin_entity")

    if entity_type in {"season", "episode"}:
        source_meta = meta["_series_meta"]
    else:
        source_meta = meta

    image_url = _logo_url(source_meta)

    if not image_url:
        raise HTTPException(
            status_code=404,
            detail="Logo not found in configured addons",
        )

    return RedirectResponse(
        image_url,
        status_code=302,
        headers={
            "Cache-Control": "public, max-age=86400",
        },
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
        headers={
            "Cache-Control": "public, max-age=86400",
        },
    )


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------


@router.get("/emby/Videos/{item_id}/{media_source_id}/Subtitles/{index}/Stream.{format}")
@router.get("/Videos/{item_id}/{media_source_id}/Subtitles/{index}/Stream.{format}")
@router.get("/emby/Videos/{item_id}/{media_source_id}/Subtitles/{index}/stream.{format}")
@router.get("/Videos/{item_id}/{media_source_id}/Subtitles/{index}/stream.{format}")
@router.get("/emby/Subtitles/{item_id}/{index}/Stream.{format}")
@router.get("/Subtitles/{item_id}/{index}/Stream.{format}")
async def subtitle_stream(
    item_id: str,
    index: int,
    format: str,
    media_source_id: str | None = None,
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

    # External subtitles are intentionally redirected to their original
    # Stremio URL. This matches Jellyfin clients that expect the video-scoped
    # subtitle endpoint to resolve to the external SRT/VTT/ASS resource and
    # avoids buffering the subtitle body through Stremfin.
    return RedirectResponse(
        track.url,
        status_code=302,
        headers={
            "Cache-Control": "public, max-age=900",
        },
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
