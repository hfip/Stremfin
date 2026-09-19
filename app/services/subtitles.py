"""Resolve external subtitle tracks from configured Stremio subtitle addons."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import Settings
from app.services.cache import AsyncTTLCache


# Subtitle results are stable enough to cache for a short period, while stale
# results can still be served immediately if an addon becomes temporarily slow.
subtitle_cache = AsyncTTLCache(
    ttl_seconds=900,
    stale_seconds=1800,
    maxsize=1024,
)


@dataclass(slots=True)
class SubtitleCandidate:
    """Canonical external subtitle track used by the Jellyfin bridge."""

    url: str
    language: str
    title: str = "Subtitle"
    format: str = "srt"
    addon_url: str | None = None
    addon_name: str | None = None
    subtitle_id: str | None = None
    raw_language: str | None = None
    raw: dict[str, Any] | None = None


class SubtitleResolver:
    """
    Resolve Arabic and English subtitle tracks from Stremio subtitle addons.

    Phase-one rules:
    - query all configured subtitle addons concurrently;
    - keep Arabic and English only;
    - Arabic tracks are ordered before English tracks;
    - preserve multiple releases/versions in the same language;
    - de-duplicate only genuinely identical tracks (same URL, or the same
      addon + explicit subtitle id);
    - never perform subtitle discovery during catalog browsing;
    - cache results so Item Details / PlaybackInfo do not repeatedly hit addons.
    """

    ARABIC_ALIASES = {
        "ar",
        "ara",
        "arabic",
        "العربية",
        "عربي",
        "العربيه",
    }

    ENGLISH_ALIASES = {
        "en",
        "eng",
        "english",
    }

    SUPPORTED_FORMATS = {
        "srt",
        "subrip",
        "vtt",
        "webvtt",
        "ass",
        "ssa",
        "sub",
        "txt",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    async def resolve(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[SubtitleCandidate]:
        content_id = str(item_id or "").strip()
        if not content_id:
            return []

        season_number = self._safe_int(season)
        episode_number = self._safe_int(episode)

        is_episode = (
            season_number is not None
            and episode_number is not None
        )
        content_type = "series" if is_episode else "movie"

        # Stremio subtitle endpoints use the same canonical series id shape as
        # stream resources: {id}:{season}:{episode}.
        resource_id = self._resource_id(
            content_id,
            season_number,
            episode_number,
        )

        addon_urls = self._addon_urls()
        if not addon_urls:
            return []

        key = self._cache_key(
            addon_urls,
            content_type,
            resource_id,
        )

        return await subtitle_cache.get_or_set(
            key,
            lambda: self._resolve_uncached(
                addon_urls,
                content_type,
                resource_id,
            ),
        )

    async def _resolve_uncached(
        self,
        addon_urls: list[str],
        content_type: str,
        resource_id: str,
    ) -> list[SubtitleCandidate]:
        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            responses = await asyncio.gather(
                *(
                    self._resolve_addon(
                        client,
                        addon_url,
                        content_type,
                        resource_id,
                    )
                    for addon_url in addon_urls
                ),
                return_exceptions=True,
            )

        tracks: list[SubtitleCandidate] = []

        # asyncio.gather preserves configured addon order.
        for response in responses:
            if isinstance(response, BaseException):
                continue
            tracks.extend(response)

        tracks = self._deduplicate(tracks)

        # Stable user-facing order: Arabic first, English second. Within each
        # language, configured addon/result order is preserved.
        tracks.sort(
            key=lambda track: (
                self._language_rank(track.language),
            )
        )

        return tracks

    async def _resolve_addon(
        self,
        client: httpx.AsyncClient,
        addon_url: str,
        content_type: str,
        resource_id: str,
    ) -> list[SubtitleCandidate]:
        type_segment = quote(content_type, safe="")
        id_segment = quote(resource_id, safe=":@._~-")
        url = (
            f"{addon_url}/subtitles/"
            f"{type_segment}/"
            f"{id_segment}.json"
        )

        try:
            response = await client.get(url)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return []

        subtitles = self._extract_subtitles(payload)
        addon_name = self._addon_name(addon_url)

        results: list[SubtitleCandidate] = []

        for subtitle in subtitles:
            candidate = self._normalize_subtitle(
                subtitle,
                addon_url=addon_url,
                addon_name=addon_name,
            )
            if candidate is not None:
                results.append(candidate)

        return results

    @staticmethod
    def _extract_subtitles(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [
                item
                for item in payload
                if isinstance(item, dict)
            ]

        if not isinstance(payload, dict):
            return []

        subtitles = payload.get("subtitles")
        if isinstance(subtitles, list):
            return [
                item
                for item in subtitles
                if isinstance(item, dict)
            ]

        if isinstance(subtitles, dict):
            for key in ("items", "subtitles", "results"):
                nested = subtitles.get(key)
                if isinstance(nested, list):
                    return [
                        item
                        for item in nested
                        if isinstance(item, dict)
                    ]

        for key in ("items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [
                    item
                    for item in value
                    if isinstance(item, dict)
                ]

        return []

    def _normalize_subtitle(
        self,
        subtitle: dict[str, Any],
        addon_url: str,
        addon_name: str,
    ) -> SubtitleCandidate | None:
        raw_url = (
            subtitle.get("url")
            or subtitle.get("src")
            or subtitle.get("file")
        )

        if not isinstance(raw_url, str):
            return None

        subtitle_url = raw_url.strip()
        if not subtitle_url.startswith(("http://", "https://")):
            return None

        raw_language = str(
            subtitle.get("lang")
            or subtitle.get("language")
            or subtitle.get("languageCode")
            or subtitle.get("language_code")
            or ""
        ).strip()

        language = self._normalize_language(raw_language)
        if language not in {"ara", "eng"}:
            return None

        subtitle_id_value = (
            subtitle.get("id")
            or subtitle.get("subtitleId")
            or subtitle.get("subtitle_id")
        )
        subtitle_id = (
            str(subtitle_id_value).strip()
            if subtitle_id_value is not None
            else None
        )
        if subtitle_id == "":
            subtitle_id = None

        subtitle_format = self._subtitle_format(
            subtitle,
            subtitle_url,
        )

        label = self._subtitle_label(subtitle)
        language_name = (
            "Arabic"
            if language == "ara"
            else "English"
        )

        # Preserve release/version information supplied by the addon. The
        # addon name is appended only when it adds useful context.
        title_parts = [language_name]

        if label and label.lower() not in {
            language_name.lower(),
            raw_language.lower(),
            "subtitle",
        }:
            title_parts.append(label)

        if addon_name:
            title_parts.append(addon_name)

        title = " • ".join(
            part
            for part in title_parts
            if part
        )

        return SubtitleCandidate(
            url=subtitle_url,
            language=language,
            title=title,
            format=subtitle_format,
            addon_url=addon_url,
            addon_name=addon_name,
            subtitle_id=subtitle_id,
            raw_language=raw_language or None,
            raw=dict(subtitle),
        )

    @classmethod
    def _normalize_language(cls, value: Any) -> str | None:
        raw = str(value or "").strip().lower()
        if not raw:
            return None

        # Common locale forms: ar-SA, ar_SA, en-US, en_GB.
        primary = (
            raw
            .replace("_", "-")
            .split("-", 1)[0]
        )

        if raw in cls.ARABIC_ALIASES or primary in cls.ARABIC_ALIASES:
            return "ara"

        if raw in cls.ENGLISH_ALIASES or primary in cls.ENGLISH_ALIASES:
            return "eng"

        return None

    @classmethod
    def _subtitle_format(
        cls,
        subtitle: dict[str, Any],
        subtitle_url: str,
    ) -> str:
        raw_format = str(
            subtitle.get("format")
            or subtitle.get("codec")
            or subtitle.get("type")
            or ""
        ).strip().lower().lstrip(".")

        aliases = {
            "subrip": "srt",
            "webvtt": "vtt",
        }

        if raw_format:
            raw_format = aliases.get(raw_format, raw_format)
            if raw_format in cls.SUPPORTED_FORMATS:
                return raw_format

        try:
            suffix = PurePosixPath(
                urlparse(subtitle_url).path
            ).suffix.lower().lstrip(".")
        except (TypeError, ValueError):
            suffix = ""

        suffix = aliases.get(suffix, suffix)

        if suffix in cls.SUPPORTED_FORMATS:
            return suffix

        # SRT is the most common Stremio external subtitle payload and keeps
        # compatibility with the existing Jellyfin subtitle bridge.
        return "srt"

    @staticmethod
    def _subtitle_label(subtitle: dict[str, Any]) -> str:
        value = (
            subtitle.get("label")
            or subtitle.get("title")
            or subtitle.get("name")
            or subtitle.get("release")
            or subtitle.get("releaseName")
            or subtitle.get("release_name")
            or ""
        )

        return " ".join(
            str(value).strip().split()
        )

    @staticmethod
    def _resource_id(
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> str:
        if season is not None and episode is not None:
            return f"{item_id}:{season}:{episode}"
        return item_id

    def _addon_urls(self) -> list[str]:
        raw_addons = getattr(
            self.settings,
            "subtitle_addon_urls",
            [],
        )

        if isinstance(raw_addons, str):
            values = [
                value.strip()
                for value in raw_addons.split(",")
                if value.strip()
            ]
        elif isinstance(raw_addons, (list, tuple, set)):
            values = list(raw_addons)
        else:
            values = []

        results: list[str] = []

        for addon in values:
            base_url = self._base_url(addon)
            if base_url and base_url not in results:
                results.append(base_url)

        return results

    @staticmethod
    def _base_url(addon_url: Any) -> str:
        value = str(addon_url or "").strip()
        if not value:
            return ""

        return (
            value
            .removesuffix("/manifest.json")
            .rstrip("/")
        )

    @staticmethod
    def _addon_name(addon_url: str) -> str:
        try:
            host = urlparse(addon_url).hostname or ""
        except (TypeError, ValueError):
            host = ""

        host = host.strip().lower()
        if host.startswith("www."):
            host = host[4:]

        if not host:
            return ""

        first = host.split(".", 1)[0]
        return first.replace("-", " ").replace("_", " ").strip().title()

    @staticmethod
    def _language_rank(language: str) -> int:
        if language == "ara":
            return 0
        if language == "eng":
            return 1
        return 2

    @staticmethod
    def _identity(track: SubtitleCandidate) -> tuple[str, ...]:
        # A literal duplicate URL is the same external track regardless of
        # label. This is the safest strong duplicate signal.
        normalized_url = track.url.strip()
        if normalized_url:
            return ("url", normalized_url)

        # Defensive fallback. In practice URL is required above, but explicit
        # addon subtitle IDs remain useful if that requirement changes later.
        if track.subtitle_id and track.addon_url:
            return (
                "id",
                track.addon_url,
                track.subtitle_id,
            )

        return (
            "track",
            track.addon_url or "",
            track.language,
            track.title,
        )

    def _deduplicate(
        self,
        tracks: list[SubtitleCandidate],
    ) -> list[SubtitleCandidate]:
        unique: dict[tuple[str, ...], SubtitleCandidate] = {}

        for track in tracks:
            identity = self._identity(track)
            if identity not in unique:
                unique[identity] = track

        return list(unique.values())

    @staticmethod
    def _cache_key(
        addon_urls: list[str],
        content_type: str,
        resource_id: str,
    ) -> str:
        return (
            "subtitles:v2:"
            f"{'|'.join(addon_urls)}:"
            f"{content_type}:"
            f"{resource_id}"
        )

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None

        try:
            return int(value)
        except (TypeError, ValueError):
            return None
