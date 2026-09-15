"""Minimal Jellyfin Server API compatibility layer for media clients."""
from uuid import uuid4
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from app.config import Settings, get_settings
from app.services.debrid import DebridResolver
from app.services.metadata import MetadataService
from app.services.settings_store import SettingsStore
from app.services.stremio import StremioResolver

router = APIRouter()
USER_ID = "stremfin-user"
TOKENS: set[str] = set()


def _runtime_settings(settings: Settings) -> Settings:
    configured = SettingsStore(settings.database_path).load()
    return settings.model_copy(update={"debrid_provider": configured.debrid_provider, "real_debrid_api_key": configured.debrid_api_key or settings.real_debrid_api_key, "torbox_api_key": configured.debrid_api_key or settings.torbox_api_key, "tmdb_api_key": configured.tmdb_api_key or settings.tmdb_api_key, "stremio_addon_urls": ",".join(configured.stremio_addon_urls)})


def _server_info(settings: Settings) -> dict:
    return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id, "StartupWizardCompleted": True, "OperatingSystem": "Linux", "OperatingSystemDisplayName": "Linux", "HasPendingRestart": False, "IsShuttingDown": False, "SupportsLibraryMonitor": False}


def _item(meta: dict, collection_type: str) -> dict:
    is_series = meta["type"] == "Series"
    item_id = meta["imdb_id"] or meta["id"]
    image_tags = {}
    dto = {"Name": meta["name"], "ServerId": "stremfin", "Id": item_id, "Type": "Series" if is_series else "Movie", "CollectionType": collection_type, "IsFolder": is_series, "RunTimeTicks": None if is_series else 72000000000, "ProductionYear": meta.get("year"), "Overview": meta.get("overview", ""), "ImageTags": image_tags, "BackdropImageTags": [], "LocationType": "Remote", "ProviderIds": {"Imdb": meta.get("imdb_id", "")}, "MediaSources": [] if is_series else [{"Id": item_id, "Name": meta["name"], "Path": item_id, "Protocol": "Http", "Type": "Default", "SupportsDirectPlay": True, "SupportsDirectStream": True, "SupportsTranscoding": False, "IsRemote": True}]}
    if meta.get("poster"): dto["ImageSources"] = [{"Type": "Primary", "Url": meta["poster"]}]
    if meta.get("backdrop"): dto.setdefault("BackdropImageSources", [{"Type": "Backdrop", "Url": meta["backdrop"]}])
    return dto


@router.get("/System/Info")
async def system_info(settings: Settings = Depends(get_settings)): return _server_info(settings)


@router.get("/System/Info/Public")
async def public_system_info(settings: Settings = Depends(get_settings)): return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id}


@router.post("/Users/AuthenticateByName")
async def authenticate(request: Request, settings: Settings = Depends(get_settings)):
    try: body = await request.json()
    except ValueError: body = {}
    username = body.get("Username") or body.get("username") or "stremfin"
    token = uuid4().hex; TOKENS.add(token)
    return {"User": {"Name": username, "ServerId": settings.server_id, "Id": USER_ID, "HasPassword": False, "HasConfiguredPassword": False, "Configuration": {"PlayDefaultAudioTrack": True, "SubtitleMode": "Default"}}, "SessionInfo": {"Id": uuid4().hex, "UserId": USER_ID, "UserName": username, "Client": "Stremfin", "DeviceName": "Stremfin", "DeviceId": "stremfin-client", "ApplicationVersion": settings.app_version, "IsActive": True, "SupportsRemoteControl": False, "PlayState": {}}, "AccessToken": token, "ServerId": settings.server_id}


@router.get("/Users/{user_id}")
async def get_user(user_id: str):
    if user_id != USER_ID: raise HTTPException(404, "User not found")
    return {"Name": "stremfin", "ServerId": "stremfin", "Id": USER_ID, "HasPassword": False, "HasConfiguredPassword": False, "EnableAutoLogin": True, "Configuration": {"PlayDefaultAudioTrack": True, "SubtitleMode": "Default"}, "Policy": {"IsAdministrator": True, "IsHidden": False, "IsDisabled": False, "EnableAllFolders": True, "EnableRemoteAccess": True}}


@router.get("/Users/{user_id}/Views")
async def get_views(user_id: str):
    if user_id != USER_ID: raise HTTPException(404, "User not found")
    return {"Items": [{"Name": "Movies", "ServerId": "stremfin", "Id": "movies", "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True}, {"Name": "TV Shows", "ServerId": "stremfin", "Id": "tvshows", "Type": "CollectionFolder", "CollectionType": "tvshows", "IsFolder": True}], "TotalRecordCount": 2, "StartIndex": 0}


@router.get("/Items")
@router.get("/Users/{user_id}/Items")
async def get_items(user_id: str | None = None, parent_id: str | None = Query(None, alias="ParentId"), include_item_types: str | None = Query(None, alias="IncludeItemTypes"), limit: int = Query(20, alias="Limit")):
    if user_id and user_id != USER_ID: raise HTTPException(404, "User not found")
    runtime = _runtime_settings(get_settings())
    kind = "series" if parent_id == "tvshows" or (include_item_types and "series" in include_item_types.lower()) else "movie"
    if parent_id not in (None, "movies", "tvshows") and parent_id:
        meta = await MetadataService(runtime).details(parent_id, "series")
        if meta and meta["type"] == "Series":
            episodes = [{"Name": f"Episode {number}", "ServerId": "stremfin", "Id": f"{parent_id}:s1e{number}", "Type": "Episode", "SeriesId": parent_id, "ParentIndexNumber": 1, "IndexNumber": number, "IsFolder": False} for number in range(1, 11)]
            return {"Items": episodes, "TotalRecordCount": len(episodes), "StartIndex": 0}
    metas = await MetadataService(runtime).catalog(kind, min(limit, 100))
    items = [_item(meta, "tvshows" if meta["type"] == "Series" else "movies") for meta in metas]
    return {"Items": items, "TotalRecordCount": len(items), "StartIndex": 0}


@router.get("/Videos/{item_id}/stream")
async def stream(item_id: str, settings: Settings = Depends(get_settings), user_agent: str | None = Header(None, alias="User-Agent")):
    settings = _runtime_settings(settings)
    parts = item_id.split(":")
    content_id, season, episode = parts[0], None, None
    if len(parts) == 2 and parts[1].startswith("s1e"): season, episode = 1, int(parts[1][3:])
    candidates = await StremioResolver(settings).resolve(content_id, season, episode)
    source = candidates[0].url if candidates else settings.fallback_stream_url
    playable = await DebridResolver(settings).resolve(source)
    return RedirectResponse(playable, status_code=302, headers={"X-Stremfin-Item": item_id})
