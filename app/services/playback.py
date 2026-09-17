"""Jellyfin playback resolution backed by live Stremio stream data."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.config import Settings
from app.services.cache import AsyncTTLCache
from app.services.debrid import DebridResolutionError, DebridResolver
from app.services.stremio import StreamCandidate, StremioResolver


playback_cache = AsyncTTLCache(
    ttl_seconds=300,
    stale_seconds=600,
    maxsize=512,
)


@dataclass(slots=True)
class ResolvedMediaSource:
    """
    One resolved source ready to expose through Jellyfin PlaybackInfo.
    """

    id: str
    item_id: str
    url: str
    name: str
    container: str | None
    protocol: str
    source_type: str
    bitrate: int | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    behavior_hints: dict[str, Any] | None = None


class PlaybackResolver:
    """
    Resolve Stremio sources into Jellyfin-compatible MediaSources.

    Pipeline:

        Jellyfin Item
            ->
        Stremio stream candidates
            ->
        candidate ranking
            ->
        Debrid/direct resolution
            ->
        normalized MediaSource DTOs

    No fake streams or mock media are created.
    """

    MAX_MEDIA_SOURCES = 10

    def __init__(self, settings: Settings):
        self.settings = settings
        self.stremio = StremioResolver(settings)
        self.debrid = DebridResolver(settings)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[ResolvedMediaSource]:
        content_id = str(item_id or "").strip()

        if not content_id:
            return []

        cache_key = self._cache_key(
            content_id,
            season,
            episode,
        )

        return await playback_cache.get_or_set(
            cache_key,
            lambda: self._resolve_uncached(
                content_id,
                season,
                episode,
            ),
        )

    async def playback_info(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        """
        Build a Jellyfin PlaybackInfo response.

        This shape intentionally contains the fields most Jellyfin clients
        expect before they decide whether DirectPlay/DirectStream is usable.
        """

        sources = await self.resolve(
            item_id,
            season,
            episode,
        )

        return {
            "MediaSources": [
                self.media_source_dto(source)
                for source in sources
            ],
            "PlaySessionId": self._play_session_id(
                item_id,
                season,
                episode,
            ),
            "ErrorCode": None,
        }

    async def first_playable_url(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
        media_source_id: str | None = None,
    ) -> str | None:
        """
        Return one resolved URL for /Videos/{id}/stream.

        If Jellyfin supplies MediaSourceId, use the same source that was
        advertised by PlaybackInfo.
        """

        sources = await self.resolve(
            item_id,
            season,
            episode,
        )

        if not sources:
            return None

        if media_source_id:
            for source in sources:
                if source.id == media_source_id:
                    return source.url

        return sources[0].url

    # ------------------------------------------------------------------
    # Resolution pipeline
    # ------------------------------------------------------------------

    async def _resolve_uncached(
        self,
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> list[ResolvedMediaSource]:
        candidates = await self.stremio.resolve(
            item_id,
            season,
            episode,
        )

        if not candidates:
            return []

        ranked = self._rank_candidates(
            candidates
        )

        results: list[ResolvedMediaSource] = []
        seen_urls: set[str] = set()

        for candidate in ranked:
            if len(results) >= self.MAX_MEDIA_SOURCES:
                break

            resolved_url = await self._resolve_candidate(
                candidate
            )

            if not resolved_url:
                continue

            if not self._is_client_playable_url(
                resolved_url
            ):
                continue

            identity = self._url_identity(
                resolved_url
            )

            if identity in seen_urls:
                continue

            seen_urls.add(identity)

            results.append(
                self._resolved_source(
                    item_id=item_id,
                    candidate=candidate,
                    resolved_url=resolved_url,
                )
            )

        return results

    async def _resolve_candidate(
        self,
        candidate: StreamCandidate,
    ) -> str | None:
        source_url = str(
            candidate.url or ""
        ).strip()

        if not source_url:
            return None

        try:
            resolved = await self.debrid.resolve(
                source_url
            )

        except DebridResolutionError:
            return None

        except Exception:
            # A broken source must not prevent Stremfin from trying the next
            # stream candidate returned by another addon.
            return None

        value = str(
            resolved or ""
        ).strip()

        return value or None

    # ------------------------------------------------------------------
    # Candidate ranking
    # ------------------------------------------------------------------

    def _rank_candidates(
        self,
        candidates: list[StreamCandidate],
    ) -> list[StreamCandidate]:
        """
        Stable ranking.

        Direct HTTP sources are preferred because they do not require an
        additional provider operation.

        Torrent sources remain usable when Debrid is configured.

        Quality hints in titles are used only as secondary ordering.
        """

        indexed = list(
            enumerate(candidates)
        )

        indexed.sort(
            key=lambda pair: (
                self._source_priority(
                    pair[1]
                ),
                -self._quality_score(
                    pair[1]
                ),
                pair[0],
            )
        )

        return [
            candidate
            for _, candidate in indexed
        ]

    def _source_priority(
        self,
        candidate: StreamCandidate,
    ) -> int:
        source = str(
            candidate.source or ""
        ).lower()

        url = str(
            candidate.url or ""
        ).lower()

        if url.startswith(
            (
                "https://",
                "http://",
            )
        ):
            return 0

        if source == "external":
            return 1

        if (
            source == "torrent"
            or url.startswith("magnet:?")
        ):
            if self._debrid_enabled():
                return 2

            return 4

        return 3

    @staticmethod
    def _quality_score(
        candidate: StreamCandidate,
    ) -> int:
        text = " ".join(
            value
            for value in (
                candidate.title,
                candidate.name,
                candidate.description,
            )
            if value
        ).lower()

        if "2160p" in text or "4k" in text:
            return 400

        if "1440p" in text:
            return 300

        if "1080p" in text:
            return 200

        if "720p" in text:
            return 100

        if "480p" in text:
            return 50

        return 0

    # ------------------------------------------------------------------
    # Source normalization
    # ------------------------------------------------------------------

    def _resolved_source(
        self,
        item_id: str,
        candidate: StreamCandidate,
        resolved_url: str,
    ) -> ResolvedMediaSource:
        title = (
            candidate.title
            or candidate.name
            or "Stremfin"
        )

        container = self._container(
            resolved_url,
            title,
        )

        width, height = self._dimensions(
            title,
            candidate.description,
        )

        video_codec = self._video_codec(
            title,
            candidate.description,
        )

        audio_codec = self._audio_codec(
            title,
            candidate.description,
        )

        bitrate = self._bitrate(
            title,
            candidate.description,
        )

        source_id = self._source_id(
            item_id,
            resolved_url,
        )

        return ResolvedMediaSource(
            id=source_id,
            item_id=item_id,
            url=resolved_url,
            name=title,
            container=container,
            protocol="Http",
            source_type=candidate.source,
            bitrate=bitrate,
            width=width,
            height=height,
            video_codec=video_codec,
            audio_codec=audio_codec,
            behavior_hints=candidate.behavior_hints,
        )

    # ------------------------------------------------------------------
    # Jellyfin DTO
    # ------------------------------------------------------------------

    @staticmethod
    def media_source_dto(
        source: ResolvedMediaSource,
    ) -> dict[str, Any]:
        media_streams: list[dict[str, Any]] = []

        if (
            source.video_codec
            or source.width
            or source.height
        ):
            video_stream: dict[str, Any] = {
                "Codec": source.video_codec,
                "Type": "Video",
                "Index": 0,
                "IsDefault": True,
                "IsForced": False,
                "IsExternal": False,
                "Width": source.width,
                "Height": source.height,
                "BitRate": source.bitrate,
                "IsAVC": (
                    source.video_codec == "h264"
                ),
            }

            media_streams.append(
                video_stream
            )

        if source.audio_codec:
            media_streams.append(
                {
                    "Codec": source.audio_codec,
                    "Type": "Audio",
                    "Index": len(
                        media_streams
                    ),
                    "IsDefault": True,
                    "IsForced": False,
                    "IsExternal": False,
                }
            )

        return {
            "Protocol": source.protocol,
            "Id": source.id,
            "Path": source.url,
            "EncoderPath": None,
            "EncoderProtocol": None,
            "Type": "Default",
            "Container": source.container,
            "Size": None,
            "Name": source.name,
            "IsRemote": True,
            "ETag": None,
            "RunTimeTicks": None,
            "ReadAtNativeFramerate": False,
            "IgnoreDts": False,
            "IgnoreIndex": False,
            "GenPtsInput": False,
            "SupportsTranscoding": False,
            "SupportsDirectStream": True,
            "SupportsDirectPlay": True,
            "IsInfiniteStream": False,
            "RequiresOpening": False,
            "OpenToken": None,
            "RequiresClosing": False,
            "LiveStreamId": None,
            "BufferMs": None,
            "RequiresLooping": False,
            "SupportsProbing": False,
            "VideoType": "VideoFile",
            "IsoType": None,
            "Video3DFormat": None,
            "MediaStreams": media_streams,
            "MediaAttachments": [],
            "Formats": [],
            "Bitrate": source.bitrate,
            "Timestamp": None,
            "RequiredHttpHeaders": {},
            "TranscodingUrl": None,
            "TranscodingSubProtocol": None,
            "TranscodingContainer": None,
            "AnalyzeDurationMs": 0,
            "DefaultAudioStreamIndex": (
                next(
                    (
                        stream["Index"]
                        for stream
                        in media_streams
                        if stream["Type"]
                        == "Audio"
                    ),
                    None,
                )
            ),
            "DefaultSubtitleStreamIndex": None,
        }

    # ------------------------------------------------------------------
    # Media hint parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _container(
        url: str,
        title: str,
    ) -> str | None:
        path = urlparse(
            url
        ).path.lower()

        known = (
            "mkv",
            "mp4",
            "m4v",
            "avi",
            "mov",
            "webm",
            "ts",
            "m2ts",
        )

        for extension in known:
            if path.endswith(
                f".{extension}"
            ):
                return extension

        lowered_title = str(
            title or ""
        ).lower()

        for extension in known:
            if re.search(
                rf"\b{re.escape(extension)}\b",
                lowered_title,
            ):
                return extension

        return None

    @staticmethod
    def _dimensions(
        *values: str | None,
    ) -> tuple[int | None, int | None]:
        text = " ".join(
            value
            for value in values
            if value
        ).lower()

        if (
            "2160p" in text
            or "4k" in text
        ):
            return 3840, 2160

        if "1440p" in text:
            return 2560, 1440

        if "1080p" in text:
            return 1920, 1080

        if "720p" in text:
            return 1280, 720

        if "480p" in text:
            return 854, 480

        return None, None

    @staticmethod
    def _video_codec(
        *values: str | None,
    ) -> str | None:
        text = " ".join(
            value
            for value in values
            if value
        ).lower()

        if any(
            token in text
            for token in (
                "hevc",
                "h265",
                "h.265",
                "x265",
            )
        ):
            return "hevc"

        if any(
            token in text
            for token in (
                "avc",
                "h264",
                "h.264",
                "x264",
            )
        ):
            return "h264"

        if "av1" in text:
            return "av1"

        if "vp9" in text:
            return "vp9"

        return None

    @staticmethod
    def _audio_codec(
        *values: str | None,
    ) -> str | None:
        text = " ".join(
            value
            for value in values
            if value
        ).lower()

        if (
            "truehd" in text
            or "true hd" in text
        ):
            return "truehd"

        if (
            "eac3" in text
            or "e-ac3" in text
            or "dd+" in text
        ):
            return "eac3"

        if (
            "ac3" in text
            or "dolby digital" in text
        ):
            return "ac3"

        if "dts" in text:
            return "dts"

        if "aac" in text:
            return "aac"

        if "opus" in text:
            return "opus"

        return None

    @staticmethod
    def _bitrate(
        *values: str | None,
    ) -> int | None:
        text = " ".join(
            value
            for value in values
            if value
        ).lower()

        match = re.search(
            r"(\d+(?:\.\d+)?)\s*mbps",
            text,
        )

        if match:
            try:
                return int(
                    float(
                        match.group(1)
                    )
                    * 1_000_000
                )
            except ValueError:
                pass

        match = re.search(
            r"(\d+(?:\.\d+)?)\s*kbps",
            text,
        )

        if match:
            try:
                return int(
                    float(
                        match.group(1)
                    )
                    * 1_000
                )
            except ValueError:
                pass

        return None

    # ------------------------------------------------------------------
    # URL / identity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_client_playable_url(
        value: str,
    ) -> bool:
        return value.lower().startswith(
            (
                "http://",
                "https://",
            )
        )

    @staticmethod
    def _url_identity(
        url: str,
    ) -> str:
        return url.strip()

    @staticmethod
    def _source_id(
        item_id: str,
        url: str,
    ) -> str:
        digest = hashlib.sha256(
            (
                f"{item_id}\0{url}"
            ).encode(
                "utf-8",
                errors="ignore",
            )
        ).hexdigest()

        return digest[:32]

    @staticmethod
    def _play_session_id(
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> str:
        value = (
            f"{item_id}:"
            f"{season}:"
            f"{episode}"
        )

        return hashlib.sha256(
            value.encode(
                "utf-8",
                errors="ignore",
            )
        ).hexdigest()[:32]

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _debrid_enabled(
        self,
    ) -> bool:
        provider = str(
            self.settings.debrid_provider
            or "none"
        ).strip().lower()

        return provider not in {
            "",
            "none",
            "disabled",
            "off",
        }

    def _cache_key(
        self,
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> str:
        provider = str(
            self.settings.debrid_provider
            or "none"
        ).strip().lower()

        return (
            "playback:"
            f"{provider}:"
            f"{item_id}:"
            f"{season}:"
            f"{episode}"
        )
