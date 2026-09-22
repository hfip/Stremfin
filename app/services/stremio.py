"""Resolve streams from Stremio-compatible addon HTTP APIs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.config import Settings
from app.services.cache import AsyncTTLCache


# Two-level stream cache.
#
# 1) addon_stream_cache stores each addon's result independently.  This lets
#    Stremfin reuse already-resolved providers when the configured addon set or
#    ordering changes, instead of scraping every provider again.
#
# 2) stream_cache stores the final merged result for the exact addon
#    configuration.  It is intentionally shorter-lived than the provider cache.
#
# AsyncTTLCache already provides per-key request coalescing (single-flight), so
# concurrent Jellyfin clients requesting the same provider/item share one
# upstream request instead of starting duplicate scrapes.
addon_stream_cache = AsyncTTLCache(
    ttl_seconds=900,
    stale_seconds=900,
    maxsize=2048,
)

stream_cache = AsyncTTLCache(
    ttl_seconds=60,
    stale_seconds=120,
    maxsize=1024,
)


@dataclass(slots=True)
class StreamCandidate:
    """
    Canonical representation of one Stremio stream candidate.

    `url` is kept as the first field for backwards compatibility with the
    current Jellyfin playback layer and DebridResolver.

    source:
        direct
        external
        torrent

    info_hash/file_idx are retained for torrent-aware playback work.
    """

    url: str
    title: str = "Stremio stream"
    behavior_hints: dict[str, Any] | None = None
    source: str = "direct"
    addon_url: str | None = None
    name: str | None = None
    description: str | None = None
    info_hash: str | None = None
    file_idx: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class StremioResolver:
    """
    Resolve playable sources from configured Stremio addons.

    Responsibilities:

    - generate correct movie/series Stremio stream endpoints
    - query addons concurrently
    - tolerate individual addon failures
    - normalize different Stremio stream object shapes
    - preserve torrent information
    - remove duplicate results
    - cache short-lived stream results
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[StreamCandidate]:
        """
        Resolve all available stream candidates for an item.

        Stremio series stream IDs conventionally use:

            {item_id}:{season}:{episode}

        For example:

            tt0944947:1:1

        and are requested through:

            /stream/series/tt0944947:1:1.json

        Movies use:

            /stream/movie/tt1234567.json
        """

        clean_item_id = str(item_id or "").strip()

        if not clean_item_id:
            return []

        normalized_season = self._safe_int(season)
        normalized_episode = self._safe_int(episode)

        is_episode = (
            normalized_season is not None
            and normalized_episode is not None
        )

        content_type = (
            "series"
            if is_episode
            else "movie"
        )

        stream_id = self._stream_id(
            clean_item_id,
            normalized_season,
            normalized_episode,
        )

        addon_urls = self._addon_urls()

        if not addon_urls:
            return []

        cache_key = self._cache_key(
            addon_urls,
            content_type,
            stream_id,
        )

        return await stream_cache.get_or_set(
            cache_key,
            lambda: self._resolve_uncached(
                addon_urls=addon_urls,
                content_type=content_type,
                stream_id=stream_id,
            ),
        )

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def _resolve_uncached(
        self,
        addon_urls: list[str],
        content_type: str,
        stream_id: str,
    ) -> list[StreamCandidate]:
        """
        Query all configured addons concurrently.

        One unavailable addon must not delay/fail all other configured
        providers.
        """

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            tasks = [
                addon_stream_cache.get_or_set(
                    self._addon_cache_key(
                        addon_url=addon_url,
                        content_type=content_type,
                        stream_id=stream_id,
                    ),
                    lambda addon_url=addon_url: self._resolve_addon(
                        client=client,
                        addon_url=addon_url,
                        content_type=content_type,
                        stream_id=stream_id,
                    ),
                )
                for addon_url in addon_urls
            ]

            responses = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        candidates: list[StreamCandidate] = []

        for response in responses:
            if isinstance(response, BaseException):
                continue

            candidates.extend(response)

        return self._deduplicate(candidates)

    async def _resolve_addon(
        self,
        client: httpx.AsyncClient,
        addon_url: str,
        content_type: str,
        stream_id: str,
    ) -> list[StreamCandidate]:
        type_segment = quote(
            content_type,
            safe="",
        )

        id_segment = quote(
            stream_id,
            safe=":@._~-",
        )

        url = (
            f"{addon_url}/stream/"
            f"{type_segment}/"
            f"{id_segment}.json"
        )

        try:
            response = await client.get(url)

            if response.status_code == 404:
                return []

            response.raise_for_status()
            payload = response.json()

        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
        ):
            return []

        streams = self._extract_streams(payload)

        results: list[StreamCandidate] = []

        for stream in streams:
            candidate = self._normalize_stream(
                stream=stream,
                addon_url=addon_url,
            )

            if candidate is not None:
                results.append(candidate)

        return results

    # ------------------------------------------------------------------
    # Stremio response normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_streams(
        payload: Any,
    ) -> list[dict[str, Any]]:
        """
        Standard Stremio response:

            {"streams": [...]}

        Also tolerate a direct list and a few common wrapper variations.
        """

        if isinstance(payload, list):
            return [
                item
                for item in payload
                if isinstance(item, dict)
            ]

        if not isinstance(payload, dict):
            return []

        streams = payload.get("streams")

        if isinstance(streams, list):
            return [
                item
                for item in streams
                if isinstance(item, dict)
            ]

        if isinstance(streams, dict):
            nested = (
                streams.get("items")
                or streams.get("streams")
                or []
            )

            if isinstance(nested, list):
                return [
                    item
                    for item in nested
                    if isinstance(item, dict)
                ]

        items = payload.get("items")

        if isinstance(items, list):
            return [
                item
                for item in items
                if isinstance(item, dict)
            ]

        return []

    def _normalize_stream(
        self,
        stream: dict[str, Any],
        addon_url: str,
    ) -> StreamCandidate | None:
        """
        Convert one Stremio stream object to StreamCandidate.

        Supported source forms:

        1. Direct HTTP URL
        2. externalUrl
        3. Torrent infoHash + fileIdx

        Torrent streams are represented as magnet URIs so the current
        DebridResolver receives a usable canonical source instead of the
        stream being silently discarded.
        """

        title = self._stream_title(stream)

        behavior_hints = stream.get(
            "behaviorHints"
        )

        if not isinstance(
            behavior_hints,
            dict,
        ):
            behavior_hints = {}

        name = stream.get("name")

        if name is not None:
            name = str(name)

        description = (
            stream.get("description")
            or stream.get("desc")
        )

        if description is not None:
            description = str(description)

        direct_url = stream.get("url")

        if isinstance(direct_url, str):
            direct_url = direct_url.strip()

        if direct_url:
            return StreamCandidate(
                url=direct_url,
                title=title,
                behavior_hints=behavior_hints,
                source="direct",
                addon_url=addon_url,
                name=name,
                description=description,
                raw=stream,
            )

        external_url = (
            stream.get("externalUrl")
            or stream.get("external_url")
        )

        if isinstance(
            external_url,
            str,
        ):
            external_url = external_url.strip()

        if external_url:
            return StreamCandidate(
                url=external_url,
                title=title,
                behavior_hints=behavior_hints,
                source="external",
                addon_url=addon_url,
                name=name,
                description=description,
                raw=stream,
            )

        info_hash = (
            stream.get("infoHash")
            or stream.get("info_hash")
        )

        if isinstance(info_hash, str):
            info_hash = info_hash.strip()

        if not info_hash:
            return None

        file_idx = self._safe_int(
            stream.get("fileIdx")
        )

        if file_idx is None:
            file_idx = self._safe_int(
                stream.get("file_idx")
            )

        magnet_url = self._magnet_url(
            info_hash=info_hash,
            title=title,
            file_idx=file_idx,
            sources=stream.get("sources"),
        )

        return StreamCandidate(
            url=magnet_url,
            title=title,
            behavior_hints=behavior_hints,
            source="torrent",
            addon_url=addon_url,
            name=name,
            description=description,
            info_hash=info_hash,
            file_idx=file_idx,
            raw=stream,
        )

    # ------------------------------------------------------------------
    # Stream helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stream_title(
        stream: dict[str, Any],
    ) -> str:
        title = (
            stream.get("title")
            or stream.get("name")
            or stream.get("description")
            or stream.get("desc")
            or "Stremio stream"
        )

        return str(title).strip() or "Stremio stream"

    @staticmethod
    def _magnet_url(
        info_hash: str,
        title: str,
        file_idx: int | None,
        sources: Any,
    ) -> str:
        """
        Build a standard magnet URI while preserving Stremio tracker
        sources when present.

        fileIdx is appended as an auxiliary query parameter so downstream
        Stremfin/debrid code can retain the selected file information.
        """

        parameters: list[tuple[str, str]] = [
            (
                "xt",
                f"urn:btih:{info_hash}",
            )
        ]

        if title:
            parameters.append(
                (
                    "dn",
                    title,
                )
            )

        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, str):
                    continue

                value = source.strip()

                if not value:
                    continue

                if value.startswith("tracker:"):
                    tracker = value.removeprefix(
                        "tracker:"
                    ).strip()

                    if tracker:
                        parameters.append(
                            (
                                "tr",
                                tracker,
                            )
                        )

                elif value.startswith(
                    (
                        "http://",
                        "https://",
                        "udp://",
                    )
                ):
                    parameters.append(
                        (
                            "tr",
                            value,
                        )
                    )

        if file_idx is not None:
            parameters.append(
                (
                    "fileIdx",
                    str(file_idx),
                )
            )

        return (
            "magnet:?"
            + urlencode(
                parameters,
                doseq=True,
            )
        )

    @staticmethod
    def _stream_id(
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> str:
        if (
            season is not None
            and episode is not None
        ):
            return (
                f"{item_id}:"
                f"{season}:"
                f"{episode}"
            )

        return item_id

    # ------------------------------------------------------------------
    # Addon helpers
    # ------------------------------------------------------------------

    def _addon_urls(self) -> list[str]:
        """
        Normalize configured addon URLs.

        Settings may contain either:

            https://addon.example.com

        or:

            https://addon.example.com/manifest.json

        Stream routes always need the base addon URL.
        """

        results: list[str] = []

        for addon in self.settings.addon_urls:
            value = self._base_url(addon)

            if (
                value
                and value not in results
            ):
                results.append(value)

        return results

    @staticmethod
    def _base_url(
        addon_url: Any,
    ) -> str:
        value = str(
            addon_url or ""
        ).strip()

        if not value:
            return ""

        return (
            value
            .removesuffix("/manifest.json")
            .rstrip("/")
        )

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def _candidate_identity(
        candidate: StreamCandidate,
    ) -> str:
        """
        Prefer torrent identity by hash/file index; otherwise use URL.
        """

        if candidate.info_hash:
            return (
                "torrent:"
                f"{candidate.info_hash.lower()}:"
                f"{candidate.file_idx}"
            )

        return (
            f"{candidate.source}:"
            f"{candidate.url}"
        )

    def _deduplicate(
        self,
        candidates: list[StreamCandidate],
    ) -> list[StreamCandidate]:
        unique: dict[str, StreamCandidate] = {}

        for candidate in candidates:
            if not candidate.url:
                continue

            identity = self._candidate_identity(
                candidate
            )

            if identity not in unique:
                unique[identity] = candidate

        return list(unique.values())

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _addon_cache_key(
        addon_url: str,
        content_type: str,
        stream_id: str,
    ) -> str:
        return (
            "addon-streams:"
            f"{addon_url}:"
            f"{content_type}:"
            f"{stream_id}"
        )

    @staticmethod
    def _cache_key(
        addon_urls: list[str],
        content_type: str,
        stream_id: str,
    ) -> str:
        addons = "|".join(addon_urls)

        return (
            "streams:"
            f"{addons}:"
            f"{content_type}:"
            f"{stream_id}"
        )

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> int | None:
        if value is None:
            return None

        try:
            return int(value)

        except (
            TypeError,
            ValueError,
        ):
            return None
