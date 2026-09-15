"""Live Stremio manifest, catalog, and metadata aggregation."""
import httpx
from app.config import Settings


class MetadataService:
    def __init__(self, settings: Settings): self.settings = settings

    async def manifests(self) -> list[dict]:
        results = []
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            for addon in self.settings.addon_urls:
                try:
                    response = await client.get(f"{addon.rstrip('/')}/manifest.json"); response.raise_for_status(); manifest = response.json()
                except (httpx.HTTPError, ValueError): continue
                for catalog in manifest.get("catalogs", []):
                    results.append({"addon_url": addon.rstrip("/"), "type": catalog.get("type", "movie"), "id": catalog.get("id"), "name": catalog.get("name") or catalog.get("id"), "extra": catalog.get("extra", [])})
        return results

    async def catalog(self, kind: str, limit: int, selected: list[dict]) -> list[dict]:
        catalogs = [c for c in selected if c.get("type") in (kind, "series" if kind == "tv" else kind)]
        results = []
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            for catalog in catalogs:
                try:
                    response = await client.get(f"{catalog['addon_url']}/catalog/{catalog['type']}/{catalog['id']}.json"); response.raise_for_status(); payload = response.json()
                except (httpx.HTTPError, ValueError, KeyError): continue
                results.extend(self._normalize(item, catalog["type"]) for item in payload.get("metas", []))
                if len(results) >= limit: break
        unique = {}; [unique.setdefault(item.get("id"), item) for item in results if item.get("id")]
        return list(unique.values())[:limit]

    async def details(self, item_id: str, kind: str, addon_urls: list[str]) -> dict | None:
        endpoint_type = "series" if kind in ("series", "tv") else "movie"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            for addon in addon_urls:
                try:
                    response = await client.get(f"{addon.rstrip('/')}/meta/{endpoint_type}/{item_id}.json")
                    if response.status_code == 404: continue
                    response.raise_for_status(); item = response.json().get("meta") or {}
                    if item: return self._normalize(item, endpoint_type)
                except (httpx.HTTPError, ValueError): continue
        return None

    def _normalize(self, item: dict, kind: str) -> dict:
        date = item.get("releaseInfo") or item.get("year") or ""
        return {"id": item.get("id"), "imdb_id": item.get("imdb_id") or (item.get("id") if str(item.get("id", "")).startswith("tt") else None), "name": item.get("name") or item.get("title"), "type": "Series" if kind in ("series", "tv") else "Movie", "overview": item.get("description") or item.get("overview") or "", "year": int(str(date)[:4]) if str(date)[:4].isdigit() else None, "poster": item.get("poster"), "backdrop": item.get("background") or item.get("backdrop"), "runtime": item.get("runtime"), "videos": item.get("videos", []), "raw": item}
