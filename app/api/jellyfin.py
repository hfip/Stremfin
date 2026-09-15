"""Minimal Jellyfin Server API compatibility layer for media clients."""
from uuid import uuid4
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from app.config import Settings, get_settings
from app.services.debrid import DebridResolver
from app.services.stremio import StremioResolver

router = APIRouter()
USER_ID = "stremfin-user"
TOKENS: set[str] = set()


def _server_info(settings: Settings) -> dict:
    return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id, "StartupWizardCompleted": True, "OperatingSystem": "Linux", "OperatingSystemDisplayName": "Linux", "HasPendingRestart": False, "IsShuttingDown": False, "SupportsLibraryMonitor": False}


def _item(item_id: str, name: str, collection_type: str, path: str = "") -> dict:
    is_series = collection_type == "tvshows"
    return {"Name": name, "ServerId": "stremfin", "Id": item_id, "Type": "Series" if is_series else "Movie", "CollectionType": collection_type, "IsFolder": is_series, "RunTimeTicks": 72000000000 if not is_series else None, "ProductionYear": 2024, "Overview": f"Resolved by Stremfin from {path or 'configured Stremio addons'}.", "ImageTags": {}, "BackdropImageTags": [], "LocationType": "Remote", "MediaSources": [] if is_series else [{"Id": item_id, "Name": name, "Path": path, "Protocol": "Http", "Type": "Default", "SupportsDirectPlay": True, "SupportsDirectStream": True, "SupportsTranscoding": False, "IsRemote": True}]}


@router.get("/System/Info")
async def system_info(settings: Settings = Depends(get_settings)):
    return _server_info(settings)


@router.get("/System/Info/Public")
async def public_system_info(settings: Settings = Depends(get_settings)):
    return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id}


@router.post("/Users/AuthenticateByName")
async def authenticate(request: Request, settings: Settings = Depends(get_settings)):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    username = body.get("Username") or body.get("username") or "stremfin"
    token = uuid4().hex
    TOKENS.add(token)
    return {"User": {"Name": username, "ServerId": settings.server_id, "Id": USER_ID, "HasPassword": False, "HasConfiguredPassword": False, "Configuration": {"PlayDefaultAudioTrack": True, "SubtitleMode": "Default"}}, "SessionInfo": {"Id": uuid4().hex, "UserId": USER_ID, "UserName": username, "Client": "Stremfin", "DeviceName": "Stremfin", "DeviceId": "stremfin-client", "ApplicationVersion": settings.app_version, "IsActive": True, "SupportsRemoteControl": False, "PlayState": {}}, "AccessToken": token, "ServerId": settings.server_id}


@router.get("/Users/{user_id}")
async def get_user(user_id: str):
    if user_id != USER_ID:
        raise HTTPException(404, "User not found")
    return {"Name": "stremfin", "ServerId": "stremfin", "Id": USER_ID, "HasPassword": False, "HasConfiguredPassword": False, "EnableAutoLogin": True, "Configuration": {"PlayDefaultAudioTrack": True, "SubtitleMode": "Default"}, "Policy": {"IsAdministrator": True, "IsHidden": False, "IsDisabled": False, "EnableAllFolders": True, "EnableRemoteAccess": True}}


@router.get("/Users/{user_id}/Views")
async def get_views(user_id: str):
    if user_id != USER_ID:
        raise HTTPException(404, "User not found")
    return {"Items": [{"Name": "Movies", "ServerId": "stremfin", "Id": "movies", "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True, "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0}}, {"Name": "TV Shows", "ServerId": "stremfin", "Id": "tvshows", "Type": "CollectionFolder", "CollectionType": "tvshows", "IsFolder": True, "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0}}], "TotalRecordCount": 2, "StartIndex": 0}


@router.get("/Items")
@router.get("/Users/{user_id}/Items")
async def get_items(user_id: str | None = None, parent_id: str | None = Query(None, alias="ParentId"), include_item_types: str | None = Query(None, alias="IncludeItemTypes")):
    if user_id and user_id != USER_ID:
        raise HTTPException(404, "User not found")
    if parent_id == "tvshows" or (include_item_types and "series" in include_item_types.lower()):
        items = [_item("tt0000001", "Example Series", "tvshows")]
    elif parent_id == "movies" or (include_item_types and "movie" in include_item_types.lower()):
        items = [_item("tt0000002", "Example Movie", "movies")]
    else:
        items = [_item("tt0000002", "Example Movie", "movies"), _item("tt0000001", "Example Series", "tvshows")]
    return {"Items": items, "TotalRecordCount": len(items), "StartIndex": 0}


@router.get("/Videos/{item_id}/stream")
async def stream(item_id: str, settings: Settings = Depends(get_settings), user_agent: str | None = Header(None, alias="User-Agent")):
    stremio = StremioResolver(settings)
    candidates = await stremio.resolve(item_id)
    source = candidates[0].url if candidates else settings.fallback_stream_url
    playable = await DebridResolver(settings).resolve(source)
    return RedirectResponse(playable, status_code=302, headers={"X-Stremfin-Item": item_id})
