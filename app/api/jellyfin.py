"""Jellyfin-compatible routes backed exclusively by live Stremio addon data."""
from uuid import uuid4
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from app.config import Settings, get_settings
from app.services.debrid import DebridResolver
from app.services.metadata import MetadataService
from app.services.settings_store import SettingsStore
from app.services.stremio import StremioResolver
from app.services.subtitles import SubtitleResolver

router = APIRouter(); USER_ID = "stremfin-user"; TOKENS: set[str] = set()

def _runtime(settings):
    saved = SettingsStore(settings.database_path).load()
    return settings.model_copy(update={"stremio_addon_urls": ",".join(saved.stream_addon_urls), "subtitle_addon_urls": ",".join(saved.subtitle_addon_urls)}), saved

def _server_info(settings): return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id, "StartupWizardCompleted": True, "OperatingSystem": "Linux"}
def _userdata(): return {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "UnplayedItemCount": 0}
def _runtime_ticks(value, default=72000000000):
    try: return int(float(str(value).split()[0]) * 600000000) if value is not None else default
    except (ValueError, TypeError): return default

def _item(meta, collection):
    item_id = meta.get("imdb_id") or meta.get("id"); series = meta.get("type") == "Series"
    dto = {"Name": meta.get("name"), "ServerId": "stremfin", "Id": item_id, "Type": "Series" if series else "Movie", "CollectionType": collection, "IsFolder": series, "MediaType": "Unknown" if series else "Video", "RunTimeTicks": None if series else _runtime_ticks(meta.get("runtime")), "ProductionYear": meta.get("year"), "Overview": meta.get("overview", ""), "ImageTags": {}, "BackdropImageTags": [], "LocationType": "Remote", "ProviderIds": {"Imdb": meta.get("imdb_id")} if meta.get("imdb_id") else {}, "MediaStreams": [], "MediaSources": [] if series else [{"Id": item_id, "Name": meta.get("name"), "Path": f"/Videos/{item_id}/stream", "Protocol": "Http", "Type": "Default", "SupportsDirectPlay": True, "SupportsDirectStream": True, "SupportsTranscoding": False, "IsRemote": True}]}
    if meta.get("poster"): dto.update({"PrimaryImageTag": "live", "ImageTags": {"Primary": "live"}, "ImageSources": [{"Type": "Primary", "Url": f"/Items/{item_id}/Images/Primary"}]})
    if meta.get("backdrop"): dto.update({"BackdropImageTags": ["live"], "BackdropImageSources": [{"Type": "Backdrop", "Url": f"/Items/{item_id}/Images/Backdrop"}]})
    return dto

def _season_dto(series_id, series_name, number):
    season_id = f"{series_id}:s{number}"
    return {"Name": f"Season {number}", "ServerId": "stremfin", "Id": season_id, "Type": "Season", "IsFolder": True, "MediaType": "Unknown", "ParentId": series_id, "SeriesId": series_id, "SeriesName": series_name, "IndexNumber": int(number), "UserData": _userdata()}

def _episode_dto(series_id, series_name, video):
    season = int(video.get("season")); episode = int(video.get("episode")); season_id = f"{series_id}:s{season}"; episode_id = video.get("id") or f"{series_id}:s{season}e{episode}"
    return {"Name": video.get("name") or video.get("title"), "ServerId": "stremfin", "Id": episode_id, "Type": "Episode", "IsFolder": False, "MediaType": "Video", "SeriesId": series_id, "SeriesName": series_name, "SeasonId": season_id, "ParentId": season_id, "ParentIndexNumber": season, "IndexNumber": episode, "Overview": video.get("overview") or video.get("description") or "", "RunTimeTicks": _runtime_ticks(video.get("runtime")), "EnableMediaSourceDisplay": True, "UserData": {}, "MediaSources": [{"Id": episode_id, "Name": video.get("name") or video.get("title"), "Path": f"/Videos/{episode_id}/stream", "Protocol": "Http", "Type": "Default", "SupportsDirectPlay": True, "SupportsDirectStream": True, "SupportsTranscoding": False, "IsRemote": True}]}

@router.get("/System/Info")
async def system_info(settings: Settings = Depends(get_settings)): return _server_info(settings)
@router.get("/System/Info/Public")
async def public_system_info(settings: Settings = Depends(get_settings)): return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id}
@router.post("/Users/AuthenticateByName")
async def authenticate(request: Request, settings: Settings = Depends(get_settings)):
    body = await request.json(); username = body.get("Username") or body.get("username") or "stremfin"; token = uuid4().hex; TOKENS.add(token)
    return {"User": {"Name": username, "ServerId": settings.server_id, "Id": USER_ID, "HasPassword": False, "HasConfiguredPassword": False, "Configuration": {"PlayDefaultAudioTrack": True, "SubtitleMode": "Default"}}, "SessionInfo": {"Id": uuid4().hex, "UserId": USER_ID, "UserName": username, "Client": "Stremfin", "DeviceName": "Stremfin", "DeviceId": "stremfin-client", "ApplicationVersion": settings.app_version, "IsActive": True, "PlayState": {}}, "AccessToken": token, "ServerId": settings.server_id}
@router.get("/Users/{user_id}")
async def get_user(user_id: str):
    if user_id != USER_ID: raise HTTPException(404, "User not found")
    return {"Name": "stremfin", "ServerId": "stremfin", "Id": USER_ID, "HasPassword": False, "HasConfiguredPassword": False, "EnableAutoLogin": True, "Policy": {"IsAdministrator": True, "EnableAllFolders": True, "EnableRemoteAccess": True}}

def _views(): return {"Items": [{"Name": "Movies", "ServerId": "stremfin", "Id": "movies", "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True}, {"Name": "TV Shows", "ServerId": "stremfin", "Id": "tvshows", "Type": "CollectionFolder", "CollectionType": "tvshows", "IsFolder": True}], "TotalRecordCount": 2, "StartIndex": 0}
@router.get("/Users/{user_id}/Views")
async def get_views(user_id: str):
    if user_id != USER_ID: raise HTTPException(404, "User not found")
    return _views()
@router.get("/UserViews")
async def user_views(): return _views()

async def _subtitle_streams(runtime, item_id, season=None, episode=None):
    tracks = await SubtitleResolver(runtime).resolve(item_id, season, episode)
    return [{"Type": "Subtitle", "Language": x.language, "DisplayTitle": x.title, "DeliveryMethod": "External", "DeliveryUrl": f"/Subtitles/{item_id}/{i}/Stream.{x.format}"} for i, x in enumerate(tracks)]

@router.get("/Items")
@router.get("/Users/{user_id}/Items")
async def get_items(user_id: str | None = None, parent_id: str | None = Query(None, alias="ParentId"), include_item_types: str | None = Query(None, alias="IncludeItemTypes"), limit: int = Query(20, alias="Limit")):
    if user_id and user_id != USER_ID: raise HTTPException(404, "User not found")
    runtime, saved = _runtime(get_settings()); service = MetadataService(runtime)
    if parent_id and parent_id not in ("movies", "tvshows"):
        meta = await service.details(parent_id.split(":")[0], "series", runtime.addon_urls)
        if not meta: raise HTTPException(404, "Series not found in configured addons")
        episodes = [_episode_dto(meta["id"], meta.get("name"), v) for v in meta.get("videos", [])]
        return {"Items": episodes[:min(limit, 100)], "TotalRecordCount": len(episodes), "StartIndex": 0}
    requested = (include_item_types or "").lower()
    if parent_id == "tvshows" or "series" in requested: kind, collection = "series", "tvshows"
    elif parent_id == "movies" or "movie" in requested: kind, collection = "movie", "movies"
    else: kind, collection = "movie", "movies"
    metas = await service.catalog(kind, min(limit, 100), saved.selected_catalogs)
    items = []
    for meta in metas:
        dto = _item(meta, collection); dto["MediaStreams"] = await _subtitle_streams(runtime, meta["id"]); items.append(dto)
    return {"Items": items, "TotalRecordCount": len(items), "StartIndex": 0}

async def _lookup(item_id):
    runtime, _ = _runtime(get_settings()); return runtime, await MetadataService(runtime).details(item_id, "series", runtime.addon_urls) or await MetadataService(runtime).details(item_id, "movie", runtime.addon_urls)
@router.get("/Items/{item_id}")
async def get_item(item_id: str):
    runtime, meta = await _lookup(item_id)
    if not meta: raise HTTPException(404, "Item not found in configured addons")
    dto = _item(meta, "tvshows" if meta["type"] == "Series" else "movies"); dto["MediaStreams"] = await _subtitle_streams(runtime, item_id); return dto
@router.get("/Shows/{series_id}/Seasons")
async def seasons(series_id: str):
    runtime, meta = await _lookup(series_id)
    if not meta or meta["type"] != "Series": raise HTTPException(404, "Series not found in configured addons")
    values = sorted({int(v["season"]) for v in meta.get("videos", []) if v.get("season") is not None})
    return {"Items": [_season_dto(series_id, meta.get("name"), n) for n in values], "TotalRecordCount": len(values), "StartIndex": 0}
@router.get("/Shows/{series_id}/Episodes")
async def episodes(series_id: str, season: int | None = Query(None, alias="Season")):
    runtime, meta = await _lookup(series_id)
    if not meta or meta["type"] != "Series": raise HTTPException(404, "Series not found in configured addons")
    values = [v for v in meta.get("videos", []) if season is None or int(v.get("season")) == season]
    return {"Items": [_episode_dto(series_id, meta.get("name"), v) for v in values], "TotalRecordCount": len(values), "StartIndex": 0}
@router.get("/Items/{item_id}/Images/Primary")
async def primary_image(item_id: str):
    _, meta = await _lookup(item_id)
    if not meta or not meta.get("poster"): raise HTTPException(404, "Image not found in configured addons")
    return RedirectResponse(meta["poster"], status_code=302)
@router.get("/Items/{item_id}/Images/Backdrop")
async def backdrop_image(item_id: str):
    _, meta = await _lookup(item_id)
    if not meta or not meta.get("backdrop"): raise HTTPException(404, "Image not found in configured addons")
    return RedirectResponse(meta["backdrop"], status_code=302)
@router.get("/Subtitles/{item_id}/{index}/Stream.{format}")
async def subtitle_stream(item_id: str, index: int, format: str):
    parts = item_id.split(":"); content, season, episode = parts[0], None, None
    if len(parts) == 2 and parts[1].startswith("s") and "e" in parts[1]: season, episode = map(int, parts[1][1:].split("e"))
    runtime, _ = _runtime(get_settings()); tracks = await SubtitleResolver(runtime).resolve(content, season, episode)
    if index >= len(tracks): raise HTTPException(404, "Subtitle not found")
    async with httpx.AsyncClient(timeout=runtime.request_timeout_seconds, follow_redirects=True) as client: response = await client.get(tracks[index].url); response.raise_for_status()
    return Response(response.content, media_type="text/vtt" if format.lower() == "vtt" else "application/x-subrip")
@router.get("/Videos/{item_id}/stream")
async def stream(item_id: str, settings: Settings = Depends(get_settings), user_agent: str | None = Header(None, alias="User-Agent")):
    runtime, _ = _runtime(settings); parts = item_id.split(":"); content, season, episode = parts[0], None, None
    if len(parts) == 2 and parts[1].startswith("s") and "e" in parts[1]: season, episode = map(int, parts[1][1:].split("e"))
    candidates = await StremioResolver(runtime).resolve(content, season, episode)
    if not candidates: raise HTTPException(404, "No stream found in configured addons")
    return RedirectResponse(await DebridResolver(runtime).resolve(candidates[0].url), status_code=302, headers={"X-Stremfin-Item": item_id})
