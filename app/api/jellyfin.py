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


def _runtime(settings: Settings) -> tuple[Settings, object]:
    saved = SettingsStore(settings.database_path).load()
    return settings.model_copy(update={"stremio_addon_urls": ",".join(saved.stream_addon_urls), "subtitle_addon_urls": ",".join(saved.subtitle_addon_urls)}), saved


def _server_info(settings): return {"LocalAddress": settings.public_base_url, "ServerName": settings.server_name, "Version": settings.app_version, "ProductName": "Stremfin", "Id": settings.server_id, "StartupWizardCompleted": True, "OperatingSystem": "Linux"}


def _item(meta, collection):
    item_id = meta.get("id"); series = meta.get("type") == "Series"
    dto = {"Name": meta.get("name"), "ServerId": "stremfin", "Id": item_id, "Type": "Series" if series else "Movie", "CollectionType": collection, "IsFolder": series, "RunTimeTicks": int(float(meta["runtime"]) * 600000000) if str(meta.get("runtime", "")).replace(".", "", 1).isdigit() else None, "ProductionYear": meta.get("year"), "Overview": meta.get("overview", ""), "ImageTags": {}, "BackdropImageTags": [], "LocationType": "Remote", "ProviderIds": {"Imdb": meta.get("imdb_id")} if meta.get("imdb_id") else {}, "MediaStreams": [], "MediaSources": [] if series else [{"Id": item_id, "Name": meta.get("name"), "Path": item_id, "Protocol": "Http", "Type": "Default", "SupportsDirectPlay": True, "SupportsDirectStream": True, "SupportsTranscoding": False, "IsRemote": True}]}
    if meta.get("poster"): dto.update({"PrimaryImageTag": "live", "ImageTags": {"Primary": "live"}, "ImageSources": [{"Type": "Primary", "Url": meta["poster"]}]})
    if meta.get("backdrop"): dto.update({"BackdropImageTags": ["live"], "BackdropImageSources": [{"Type": "Backdrop", "Url": meta["backdrop"]}]})
    return dto


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
@router.get("/Users/{user_id}/Views")
async def get_views(user_id: str):
    if user_id != USER_ID: raise HTTPException(404, "User not found")
    return {"Items": [{"Name": "Movies", "ServerId": "stremfin", "Id": "movies", "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True}, {"Name": "TV Shows", "ServerId": "stremfin", "Id": "tvshows", "Type": "CollectionFolder", "CollectionType": "tvshows", "IsFolder": True}], "TotalRecordCount": 2, "StartIndex": 0}


async def _subtitle_streams(runtime, item_id, season=None, episode=None):
    tracks = await SubtitleResolver(runtime).resolve(item_id, season, episode)
    return [{"Type": "Subtitle", "Language": x.language, "DisplayTitle": x.title, "DeliveryMethod": "External", "DeliveryUrl": f"/Subtitles/{item_id}/{i}/Stream.{x.format}"} for i, x in enumerate(tracks)]


@router.get("/Items")
@router.get("/Users/{user_id}/Items")
async def get_items(user_id: str | None = None, parent_id: str | None = Query(None, alias="ParentId"), include_item_types: str | None = Query(None, alias="IncludeItemTypes"), limit: int = Query(20, alias="Limit")):
    if user_id and user_id != USER_ID: raise HTTPException(404, "User not found")
    runtime, saved = _runtime(get_settings()); service = MetadataService(runtime)
    if parent_id and parent_id not in ("movies", "tvshows"):
        meta = await service.details(parent_id, "series", runtime.addon_urls)
        if not meta: raise HTTPException(404, "Series not found in configured addons")
        videos = meta.get("videos", []); episodes = []
        for video in videos:
            episode_id = video.get("id") or f"{parent_id}:s{video.get('season')}e{video.get('episode')}"
            episodes.append({"Name": video.get("name") or video.get("title"), "ServerId": "stremfin", "Id": episode_id, "Type": "Episode", "SeriesId": parent_id, "ParentIndexNumber": video.get("season"), "IndexNumber": video.get("episode"), "Overview": video.get("overview") or "", "IsFolder": False})
        return {"Items": episodes[:min(limit, 100)], "TotalRecordCount": len(episodes), "StartIndex": 0}
    kind = "series" if parent_id == "tvshows" or (include_item_types and "series" in include_item_types.lower()) else "movie"
    metas = await service.catalog(kind, min(limit, 100), saved.selected_catalogs)
    items = []
    for meta in metas:
        dto = _item(meta, "tvshows" if meta["type"] == "Series" else "movies"); dto["MediaStreams"] = await _subtitle_streams(runtime, meta["id"]); items.append(dto)
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
    values = sorted({v.get("season") for v in meta.get("videos", []) if v.get("season") is not None})
    return {"Items": [{"Name": f"Season {number}", "ServerId": "stremfin", "Id": f"{series_id}:s{number}", "SeriesId": series_id, "Type": "Season", "IndexNumber": number, "IsFolder": True} for number in values], "TotalRecordCount": len(values), "StartIndex": 0}


@router.get("/Shows/{series_id}/Episodes")
async def episodes(series_id: str, season: int | None = Query(None, alias="Season")):
    runtime, meta = await _lookup(series_id)
    if not meta or meta["type"] != "Series": raise HTTPException(404, "Series not found in configured addons")
    values = [v for v in meta.get("videos", []) if season is None or v.get("season") == season]
    return {"Items": [{"Name": v.get("name") or v.get("title"), "ServerId": "stremfin", "Id": v.get("id") or f"{series_id}:s{v.get('season')}e{v.get('episode')}", "Type": "Episode", "SeriesId": series_id, "ParentIndexNumber": v.get("season"), "IndexNumber": v.get("episode"), "Overview": v.get("overview") or "", "IsFolder": False} for v in values], "TotalRecordCount": len(values), "StartIndex": 0}


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
