"""Metadata aggregation from TMDB (when configured) and Cinemeta fallback."""
import httpx
from app.config import Settings


class MetadataService:
    def __init__(self, settings: Settings): self.settings = settings

    async def catalog(self, kind: str, limit: int = 20) -> list[dict]:
        if self.settings.tmdb_api_key:
            try: return await self._tmdb_catalog(kind, limit)
            except httpx.HTTPError: pass
        try: return await self._cinemeta_catalog(kind, limit)
        except httpx.HTTPError: return self._offline_catalog(kind, limit)

    async def details(self, item_id: str, kind: str) -> dict | None:
        if self.settings.tmdb_api_key and item_id.isdigit():
            try: return await self._tmdb_details(item_id, kind)
            except httpx.HTTPError: pass
        try: return await self._cinemeta_details(item_id, kind)
        except httpx.HTTPError: return None

    async def _tmdb_catalog(self, kind, limit):
        endpoint = "tv" if kind in ("series", "tv") else "movie"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            response = await client.get(f"https://api.themoviedb.org/3/trending/{endpoint}/week", params={"api_key": self.settings.tmdb_api_key}); response.raise_for_status()
            return [self._tmdb_item(item, endpoint) for item in response.json().get("results", [])[:limit]]

    async def _tmdb_details(self, item_id, kind):
        endpoint = "tv" if kind in ("series", "tv") else "movie"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            response = await client.get(f"https://api.themoviedb.org/3/{endpoint}/{item_id}", params={"api_key": self.settings.tmdb_api_key, "append_to_response": "external_ids"}); response.raise_for_status()
            return self._tmdb_item(response.json(), endpoint)

    def _tmdb_item(self, item, endpoint):
        title = item.get("name") or item.get("title") or "Untitled"; date = item.get("first_air_date") or item.get("release_date") or ""
        return {"id": str(item.get("id")), "imdb_id": item.get("external_ids", {}).get("imdb_id") or item.get("imdb_id"), "name": title, "type": "Series" if endpoint == "tv" else "Movie", "overview": item.get("overview") or "", "year": int(date[:4]) if date[:4].isdigit() else None, "poster": f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get("poster_path") else None, "backdrop": f"https://image.tmdb.org/t/p/w1280{item['backdrop_path']}" if item.get("backdrop_path") else None}

    async def _cinemeta_catalog(self, kind, limit):
        endpoint = "series" if kind in ("series", "tv") else "movie"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            response = await client.get(f"https://v3-cinemeta.strem.io/meta/{endpoint}/top.json"); response.raise_for_status()
            return [self._cinemeta_item(item, endpoint) for item in response.json().get("meta", [])[:limit]]

    async def _cinemeta_details(self, item_id, kind):
        endpoint = "series" if kind in ("series", "tv") else "movie"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            response = await client.get(f"https://v3-cinemeta.strem.io/meta/{endpoint}/{item_id}.json")
            if response.status_code == 404: return None
            response.raise_for_status(); return self._cinemeta_item(response.json().get("meta", {}), endpoint)

    def _cinemeta_item(self, item, endpoint):
        return {"id": item.get("imdb_id") or item.get("id"), "imdb_id": item.get("imdb_id") or item.get("id"), "name": item.get("name") or "Untitled", "type": "Series" if endpoint == "series" else "Movie", "overview": item.get("description") or item.get("overview") or "", "year": item.get("year"), "poster": item.get("poster"), "backdrop": item.get("background") or item.get("backdrop")}

    def _offline_catalog(self, kind, limit):
        if kind in ("series", "tv"):
            return [{"id": "tt0944947", "imdb_id": "tt0944947", "name": "Game of Thrones", "type": "Series", "overview": "Nine noble families fight for control over the lands of Westeros.", "year": 2011, "poster": None, "backdrop": None}][:limit]
        return [{"id": "tt0111161", "imdb_id": "tt0111161", "name": "The Shawshank Redemption", "type": "Movie", "overview": "Two imprisoned men bond over a number of years.", "year": 1994, "poster": None, "backdrop": None}][:limit]
