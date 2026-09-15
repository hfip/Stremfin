"""Resolve subtitle tracks from configured Stremio subtitle addons."""
from dataclasses import dataclass
import httpx
from app.config import Settings


@dataclass(slots=True)
class SubtitleCandidate:
    url: str
    language: str
    title: str = "Subtitle"
    format: str = "srt"


class SubtitleResolver:
    def __init__(self, settings: Settings): self.settings = settings

    async def resolve(self, item_id: str, season: int | None = None, episode: int | None = None) -> list[SubtitleCandidate]:
        candidates = []
        content_type = "series" if season is not None else "movie"
        suffix = f"/{season}:{episode}" if season is not None and episode is not None else ""
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, follow_redirects=True) as client:
            addons = self.settings.subtitle_addon_urls if isinstance(self.settings.subtitle_addon_urls, list) else [x.strip() for x in self.settings.subtitle_addon_urls.split(",") if x.strip()]
            for addon in addons:
                try:
                    response = await client.get(f"{addon.rstrip('/')}/subtitles/{content_type}/{item_id}{suffix}.json")
                    response.raise_for_status(); payload = response.json()
                except (httpx.HTTPError, ValueError): continue
                for subtitle in payload.get("subtitles", []):
                    if subtitle.get("url"):
                        language = subtitle.get("lang") or subtitle.get("language") or "eng"
                        language = {"ar": "ara", "arabic": "ara", "en": "eng", "english": "eng"}.get(language.lower(), language.lower())
                        candidates.append(SubtitleCandidate(subtitle["url"], language, subtitle.get("title", "Subtitle"), subtitle.get("format", "srt")))
        return candidates
