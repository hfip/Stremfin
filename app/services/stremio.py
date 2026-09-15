"""Resolve streams from Stremio-compatible addon HTTP APIs."""
from dataclasses import dataclass
import httpx
from app.config import Settings


@dataclass(slots=True)
class StreamCandidate:
    url: str
    title: str = "Stremio stream"
    behavior_hints: dict | None = None


class StremioResolver:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def resolve(self, item_id: str) -> list[StreamCandidate]:
        """Query configured addon `/stream/movie|series/{id}.json` endpoints.

        The Jellyfin item id is intentionally accepted as a Stremio content id so
        clients can pass IMDb/TMDB-style identifiers without extra translation.
        """
        candidates: list[StreamCandidate] = []
        content_type = "series" if item_id.lower().startswith(("tt", "tv", "series:")) else "movie"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            for addon in self.settings.addon_urls:
                url = f"{addon}/stream/{content_type}/{item_id}.json"
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                for stream in payload.get("streams", []):
                    stream_url = stream.get("url")
                    if stream_url:
                        candidates.append(StreamCandidate(stream_url, stream.get("title", "Stremio stream"), stream.get("behaviorHints")))
        return candidates
