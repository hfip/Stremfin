"""Minimal Jellyfin Server API compatibility layer for media clients."""
from uuid import uuid4
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
import httpx
from app.config import Settings, get_settings
from app.services.debrid import DebridResolver
from app.services.metadata import MetadataService
from app.services.settings_store import SettingsStore
from app.services.stremio import StremioResolver
from app.services.subtitles import SubtitleResolver

router = APIRouter()
USER_ID = "stremfin-user"
TOKENS: set[str] = set()


def _runtime_settings(settings: Settings) -> Settings:
    configured = SettingsStore(settings.database_path).load()
    return settings.model_copy(update={"debrid_provider": configured.debrid_provider, "real_debrid_api_key": configured.debrid_api_key or settings.real_debrid_api_key, "torbox_api_key": configured.debrid_api_key or settings.torbox_api_key, "tmdb_api_key": configured.tmdb_api_key or settings.tmdb_api_key, "stremio_addon_urls": ",".join(configured.stream_addon_urls), "subtitle_addon_urls": ",".join(configured.subtitle_addon_urls)})


def _server_info(settings: Settings) -> dict:
    return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id, "StartupWizardCompleted": True, "OperatingSystem": "Linux", "OperatingSystemDisplayName": "Linux", "HasPendingRestart": False, "IsShuttingDown": False, "SupportsLibraryMonitor": False}


def _item(meta: dict, collection_type: str) -> dict:
    is_series = meta["type"] == "Series"
    item_id = meta["imdb_id"] or meta["id"]
    image_tags = {"Primary": "stremfin-primary", "Backdrop": "stremfin-backdrop"}
    dto = {"Name": meta["name"], "ServerId": "stremfin", "Id": item_id, "Type": "Series" if is_series else "Movie", "CollectionType": collection_type, "IsFolder": is_series, "RunTimeTicks": None if is_series else 72000000000, "ProductionYear": meta.get("year"), "Overview": meta.get("overview", ""), "ImageTags": image_tags, "PrimaryImageTag": "stremfin-primary", "BackdropImageTags": ["stremfin-backdrop"], "LocationType": "Remote", "ProviderIds": {"Imdb": meta.get("imdb_id", "")}, "MediaStreams": [], "MediaSources": [] if is_series else [{"Id": item_id, "Name": meta["name"], "Path": item_id, "Protocol": "Http", "Type": "Default", "SupportsDirectPlay": True, "SupportsDirectStream": True, "SupportsTranscoding": False, "IsRemote": True}]}
    if meta.get("poster"): dto["ImageSources"] = [{"Type": "Primary", "Url": meta["poster"]}]
    if meta.get("backdrop"): dto["BackdropImageSources"] = [{"Type": "Backdrop", "Url": meta["backdrop"]}]
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
    items = []
    for meta in metas:
        dto = _item(meta, "tvshows" if meta["type"] == "Series" else "movies")
        subtitles = await SubtitleResolver(runtime).resolve(meta.get("imdb_id") or meta["id"])
        dto["MediaStreams"] = [{"Type": "Subtitle", "Language": sub.language, "DisplayTitle": sub.title, "DeliveryMethod": "External", "DeliveryUrl": f"/Subtitles/{dto['Id']}/{index}/Stream.{sub.format}"} for index, sub in enumerate(subtitles)]
        items.append(dto)
    return {"Items": items, "TotalRecordCount": len(items), "StartIndex": 0}


async def _image(item_id: str, image_type: str):
    runtime = _runtime_settings(get_settings())
    meta = await MetadataService(runtime).details(item_id, "series") or await MetadataService(runtime).details(item_id, "movie")
    url = meta.get("backdrop" if image_type == "Backdrop" else "poster") if meta else None
    if not url: raise HTTPException(404, "Image not found")
    return RedirectResponse(url, status_code=302)


@router.get("/Items/{item_id}/Images/Primary")
async def primary_image(item_id: str): return await _image(item_id, "Primary")


@router.get("/Items/{item_id}/Images/Backdrop")
async def backdrop_image(item_id: str): return await _image(item_id, "Backdrop")


@router.get("/Subtitles/{item_id}/{index}/Stream.{format}")
async def subtitle_stream(item_id: str, index: int, format: str):
    parts = item_id.split(":"); content_id, season, episode = parts[0], None, None
    if len(parts) == 2 and parts[1].startswith("s1e"): season, episode = 1, int(parts[1][3:])
    runtime = _runtime_settings(get_settings()); candidates = await SubtitleResolver(runtime).resolve(content_id, season, episode)
    if index >= len(candidates): raise HTTPException(404, "Subtitle not found")
    async with httpx.AsyncClient(timeout=runtime.request_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(candidates[index].url); response.raise_for_status()
    media_type = "text/vtt" if format.lower() == "vtt" else "application/x-subrip"
    return Response(response.content, media_type=media_type, headers={"Content-Disposition": f'inline; filename="{item_id}.{format}"'})


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
