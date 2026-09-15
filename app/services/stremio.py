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
    def __init__(self, settings: Settings): self.settings = settings

    async def resolve(self, item_id: str, season: int | None = None, episode: int | None = None) -> list[StreamCandidate]:
        candidates: list[StreamCandidate] = []
        content_type = "series" if season is not None or item_id.lower().startswith(("tt", "tv", "series:")) else "movie"
        suffix = f"/{season}:{episode}" if season is not None and episode is not None else ""
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            for addon in self.settings.addon_urls:
                try:
                    response = await client.get(f"{addon}/stream/{content_type}/{item_id}{suffix}.json")
                    response.raise_for_status(); payload = response.json()
                except (httpx.HTTPError, ValueError): continue
                for stream in payload.get("streams", []):
                    if stream.get("url"): candidates.append(StreamCandidate(stream["url"], stream.get("title", "Stremio stream"), stream.get("behaviorHints")))
        return candidates
