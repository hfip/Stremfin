"""Debrid link resolution with provider-specific extension points."""
import httpx
from app.config import Settings


class DebridResolver:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def resolve(self, url: str) -> str:
        """Return a playable URL.

        Providers can be extended here without changing Jellyfin routes. Direct
        HTTP(S) links already playable by a client pass through unchanged.
        """
        provider = self.settings.debrid_provider.lower()
        if provider == "none" or url.startswith(("http://", "https://")):
            return url
        if provider == "real-debrid":
            return await self._real_debrid(url)
        if provider == "torbox":
            return await self._torbox(url)
        return url

    async def _real_debrid(self, url: str) -> str:
        if not self.settings.real_debrid_api_key:
            return url
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post("https://api.real-debrid.com/rest/1.0/unrestrict/link", headers={"Authorization": f"Bearer {self.settings.real_debrid_api_key}"}, data={"link": url})
            response.raise_for_status()
            return response.json().get("download", url)

    async def _torbox(self, url: str) -> str:
        if not self.settings.torbox_api_key:
            return url
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post("https://api.torbox.app/v1/api/stream/resolve", headers={"Authorization": f"Bearer {self.settings.torbox_api_key}"}, json={"url": url})
            response.raise_for_status()
            payload = response.json()
            return payload.get("url") or payload.get("download_url") or url
