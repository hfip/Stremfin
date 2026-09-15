"""Live Stremio manifest, catalog, and metadata aggregation."""
import httpx
from app.config import Settings
from app.services.cache import metadata_cache


class MetadataService:
    def __init__(self, settings: Settings): self.settings = settings

    async def manifests(self) -> list[dict]:
        key = "manifests:" + "|".join(self.settings.addon_urls)
        return await metadata_cache.get_or_set(key, self._fetch_manifests)

    async def _fetch_manifests(self) -> list[dict]:
        results = []
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            for addon in self.settings.addon_urls:
                base_url = addon.removesuffix("/manifest.json").rstrip("/")
                try:
                    response = await client.get(f"{base_url}/manifest.json"); response.raise_for_status(); manifest = response.json()
                except (httpx.HTTPError, ValueError): continue
                for catalog in manifest.get("catalogs", []):
                    results.append({"addon_url": base_url, "type": catalog.get("type", "movie"), "id": catalog.get("id"), "name": catalog.get("name") or catalog.get("id"), "extra": catalog.get("extra", [])})
        return results

    async def catalog(self, kind: str, limit: int, selected: list[dict]) -> list[dict]:
        catalogs = [c for c in selected if c.get("type") == ("series" if kind in ("series", "tv") else "movie")]
        result = []
        for catalog in catalogs:
            key = f"catalog:{catalog.get('addon_url')}:{catalog.get('type')}:{catalog.get('id')}"
            items = await metadata_cache.get_or_set(key, lambda c=catalog: self._fetch_catalog(c))
            result.extend(items)
            if len(result) >= limit: break
        unique = {}; [unique.setdefault(item.get("id"), item) for item in result if item.get("id")]
        return list(unique.values())[:limit]

    async def _fetch_catalog(self, catalog: dict) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            try:
                response = await client.get(f"{catalog['addon_url'].removesuffix('/manifest.json').rstrip('/')}/catalog/{catalog['type']}/{catalog['id']}.json"); response.raise_for_status(); payload = response.json()
            except (httpx.HTTPError, ValueError, KeyError): return []
        return [self._normalize(item, catalog["type"]) for item in payload.get("metas", [])]

    async def details(self, item_id: str, kind: str, addon_urls: list[str]) -> dict | None:
        endpoint_type = "series" if kind in ("series", "tv") else "movie"
        for addon in addon_urls:
            base_url = addon.removesuffix("/manifest.json").rstrip("/")
            key = f"meta:{base_url}:{endpoint_type}:{item_id}"
            result = await metadata_cache.get_or_set(key, lambda b=base_url: self._fetch_details(b, endpoint_type, item_id))
            if result: return result
        return None

    async def _fetch_details(self, base_url: str, endpoint_type: str, item_id: str) -> dict | None:
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            try:
                response = await client.get(f"{base_url}/meta/{endpoint_type}/{item_id}.json")
                if response.status_code == 404: return None
                response.raise_for_status(); item = response.json().get("meta") or {}
            except (httpx.HTTPError, ValueError): return None
        return self._normalize(item, endpoint_type) if item else None

    def _normalize(self, item: dict, kind: str) -> dict:
        date = item.get("releaseInfo") or item.get("year") or ""
        return {"id": item.get("id"), "imdb_id": item.get("imdb_id") or (item.get("id") if str(item.get("id", "")).startswith("tt") else None), "name": item.get("name") or item.get("title"), "type": "Series" if kind in ("series", "tv") else "Movie", "overview": item.get("description") or item.get("overview") or "", "year": int(str(date)[:4]) if str(date)[:4].isdigit() else None, "poster": item.get("poster"), "backdrop": item.get("background") or item.get("backdrop"), "runtime": item.get("runtime"), "videos": item.get("videos", []), "raw": item}
