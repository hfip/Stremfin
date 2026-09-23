"""Resolve Stremio streams into stable Jellyfin MediaSources."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.config import Settings
from app.services.cache import AsyncTTLCache
from app.services.debrid import DebridResolutionError, DebridResolver
from app.services.stremio import StreamCandidate, StremioResolver


# Item details and PlaybackInfo intentionally share this cache.  Infuse can
# therefore receive the same source set before and after pressing Play.
playback_cache = AsyncTTLCache(
    ttl_seconds=3600,
    stale_seconds=300,
    maxsize=1024,
    persistent_path=os.getenv(
        "DATABASE_PATH",
        "./data/stremfin.db",
    ),
    persistent_namespace="playback",
)

# Keep the URL that was actually issued for a stable MediaSource Id separate
# from the full source-list cache.  The mapping is refreshed whenever a client
# receives the source list, so pressing Play can reuse that exact URL without
# re-resolving the item.  The stable Id itself remains candidate-based and is
# never derived from an expiring Debrid URL.
issued_source_cache = AsyncTTLCache(
    ttl_seconds=3600,
    stale_seconds=0,
    maxsize=4096,
    persistent_path=os.getenv(
        "DATABASE_PATH",
        "./data/stremfin.db",
    ),
    persistent_namespace="playback-issued-source",
)


@dataclass(slots=True)
class ResolvedMediaSource:
    id: str
    item_id: str
    url: str
    name: str
    container: str | None
    protocol: str
    source_type: str
    addon_name: str | None = None
    addon_url: str | None = None
    quality: str | None = None
    release_type: str | None = None
    bitrate: int | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    behavior_hints: dict[str, Any] | None = None


class PlaybackResolver:
    """
    Resolve every usable Stremio candidate into Jellyfin MediaSources.

    Important compatibility rules:
    - no fixed MediaSources limit;
    - all configured stream addons get a chance before one addon can dominate;
    - Item details and PlaybackInfo use the exact same cached result;
    - MediaSource Id is based on the original candidate, never an expiring
      debrid URL.
    """

    MAX_PARALLEL_RESOLUTIONS = 8

    def __init__(self, settings: Settings):
        self.settings = settings
        self.stremio = StremioResolver(settings)
        self.debrid = DebridResolver(settings)

    async def resolve(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[ResolvedMediaSource]:
        content_id = str(item_id or "").strip()
        if not content_id:
            return []

        season_number = self._safe_int(season)
        episode_number = self._safe_int(episode)
        cache_key = self._cache_key(content_id, season_number, episode_number)

        cached = await playback_cache.get_or_set(
            cache_key,
            lambda: self._resolve_serializable(
                content_id,
                season_number,
                episode_number,
            ),
        )
        sources = self._deserialize_sources(cached)
        await self._remember_issued_sources(sources)
        return sources

    async def playback_info(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        sources = await self.resolve(item_id, season, episode)
        return {
            "MediaSources": [self.media_source_dto(source) for source in sources],
            "PlaySessionId": self._play_session_id(item_id, season, episode),
            "ErrorCode": None if sources else "NoCompatibleStream",
        }

    async def media_sources(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[dict[str, Any]]:
        sources = await self.resolve(item_id, season, episode)
        return [self.media_source_dto(source) for source in sources]

    async def first_playable_url(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
        media_source_id: str | None = None,
    ) -> str | None:
        content_id = str(item_id or "").strip()
        requested = str(media_source_id or "").strip()

        # A client may ask to play a MediaSource after the full source-list
        # cache has changed or expired.  Prefer the exact URL that Stremfin
        # previously issued for that stable MediaSource Id.
        if content_id and requested:
            issued = await issued_source_cache.get(
                self._issued_source_key(content_id, requested)
            )
            if isinstance(issued, str):
                issued_url = issued.strip()
                if self._is_client_playable_url(issued_url):
                    return issued_url

        sources = await self.resolve(content_id, season, episode)
        if not sources:
            return None

        if requested:
            for source in sources:
                if source.id == requested:
                    await self._remember_issued_source(source)
                    return source.url

        await self._remember_issued_source(sources[0])
        return sources[0].url

    async def _remember_issued_sources(
        self,
        sources: list[ResolvedMediaSource],
    ) -> None:
        if not sources:
            return

        await asyncio.gather(
            *(self._remember_issued_source(source) for source in sources),
            return_exceptions=True,
        )

    async def _remember_issued_source(
        self,
        source: ResolvedMediaSource,
    ) -> None:
        if (
            not source.id
            or not source.item_id
            or not self._is_client_playable_url(source.url)
        ):
            return

        await issued_source_cache.set(
            self._issued_source_key(source.item_id, source.id),
            source.url,
        )

    @staticmethod
    def _issued_source_key(
        item_id: str,
        media_source_id: str,
    ) -> str:
        return f"issued:v1:{item_id}:{media_source_id}"

    async def _resolve_serializable(
        self,
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> list[dict[str, Any]]:
        """Resolve sources into JSON-safe dictionaries for RAM + SQLite cache."""
        sources = await self._resolve_uncached(item_id, season, episode)
        return [self._serialize_source(source) for source in sources]

    @staticmethod
    def _serialize_source(source: ResolvedMediaSource) -> dict[str, Any]:
        return {
            "id": source.id,
            "item_id": source.item_id,
            "url": source.url,
            "name": source.name,
            "container": source.container,
            "protocol": source.protocol,
            "source_type": source.source_type,
            "addon_name": source.addon_name,
            "addon_url": source.addon_url,
            "quality": source.quality,
            "release_type": source.release_type,
            "bitrate": source.bitrate,
            "width": source.width,
            "height": source.height,
            "video_codec": source.video_codec,
            "audio_codec": source.audio_codec,
            "behavior_hints": source.behavior_hints,
        }

    @staticmethod
    def _deserialize_sources(value: Any) -> list[ResolvedMediaSource]:
        if not isinstance(value, list):
            return []

        output: list[ResolvedMediaSource] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            try:
                source = ResolvedMediaSource(
                    id=str(raw.get("id") or ""),
                    item_id=str(raw.get("item_id") or ""),
                    url=str(raw.get("url") or ""),
                    name=str(raw.get("name") or "Stremfin Source"),
                    container=raw.get("container"),
                    protocol=str(raw.get("protocol") or "Http"),
                    source_type=str(raw.get("source_type") or "direct"),
                    addon_name=raw.get("addon_name"),
                    addon_url=raw.get("addon_url"),
                    quality=raw.get("quality"),
                    release_type=raw.get("release_type"),
                    bitrate=raw.get("bitrate"),
                    width=raw.get("width"),
                    height=raw.get("height"),
                    video_codec=raw.get("video_codec"),
                    audio_codec=raw.get("audio_codec"),
                    behavior_hints=raw.get("behavior_hints") if isinstance(raw.get("behavior_hints"), dict) else None,
                )
            except (TypeError, ValueError):
                continue

            if source.id and source.item_id and source.url:
                output.append(source)

        return output

    async def _resolve_uncached(
        self,
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> list[ResolvedMediaSource]:
        # StremioResolver already queries configured addons concurrently.
        candidates = await self.stremio.resolve(item_id, season, episode)
        if not candidates:
            return []

        # Fair ordering is important.  The old implementation globally sorted
        # and truncated to 12 sources, so a prolific 4K addon could hide every
        # other addon.  We round-robin ranked per-addon queues and DO NOT
        # truncate the final source list.
        ordered = self._fair_candidates(candidates)

        semaphore = asyncio.Semaphore(self.MAX_PARALLEL_RESOLUTIONS)

        async def resolve_one(
            candidate: StreamCandidate,
        ) -> tuple[StreamCandidate, str | None]:
            async with semaphore:
                return candidate, await self._resolve_candidate(candidate)

        resolved_pairs = await asyncio.gather(
            *(resolve_one(candidate) for candidate in ordered),
            return_exceptions=False,
        )

        results: list[ResolvedMediaSource] = []
        seen_urls: set[str] = set()
        seen_sources: set[str] = set()

        for candidate, resolved_url in resolved_pairs:
            if not resolved_url or not self._is_client_playable_url(resolved_url):
                continue

            url_identity = self._url_identity(resolved_url)
            source_identity = self._candidate_identity(item_id, candidate)

            if url_identity in seen_urls or source_identity in seen_sources:
                continue

            seen_urls.add(url_identity)
            seen_sources.add(source_identity)
            results.append(
                self._resolved_source(
                    item_id=item_id,
                    candidate=candidate,
                    resolved_url=resolved_url,
                )
            )

        # Keep the final MediaSources order deterministic across clients.
        # Quality is the primary key; configured addon order is the secondary
        # key. Concurrency affects latency only, never the visible ordering.
        results.sort(key=self._resolved_source_sort_key)
        return results

    def _resolved_source_sort_key(
        self,
        source: ResolvedMediaSource,
    ) -> tuple[int, int, int, str, str]:
        quality_rank = {
            "4K": 0,
            "1440p": 1,
            "1080p": 2,
            "720p": 3,
            "576p": 4,
            "480p": 5,
            "360p": 6,
        }.get(str(source.quality or ""), 7)

        addon_order = {
            self._normalized_addon_url(url): index
            for index, url in enumerate(self._addon_urls())
        }
        addon_rank = addon_order.get(
            self._normalized_addon_url(source.addon_url),
            len(addon_order),
        )

        source_type = str(source.source_type or "").strip().lower()
        source_rank = {
            "direct": 0,
            "external": 1,
            "torrent": 2 if self._debrid_enabled() else 4,
        }.get(source_type, 3)

        return (
            quality_rank,
            addon_rank,
            source_rank,
            str(source.name or "").casefold(),
            str(source.id or ""),
        )

    @staticmethod
    def _normalized_addon_url(value: str | None) -> str:
        url = str(value or "").strip().rstrip("/")
        if url.endswith("/manifest.json"):
            url = url[:-len("/manifest.json")]
        return url.casefold()

    async def _resolve_candidate(self, candidate: StreamCandidate) -> str | None:
        source_url = str(candidate.url or "").strip()
        if not source_url:
            return None

        try:
            resolved = await self.debrid.resolve(source_url)
        except DebridResolutionError:
            return None
        except Exception:
            return None

        value = str(resolved or "").strip()
        return value or None

    def _fair_candidates(
        self,
        candidates: list[StreamCandidate],
    ) -> list[StreamCandidate]:
        """
        Rank inside each addon, then round-robin addons.

        This preserves quality preference while guaranteeing that addon B/C/D
        are not pushed out merely because addon A returned many more entries.
        No numerical source limit is applied.
        """
        groups: dict[str, list[StreamCandidate]] = {}
        group_order: list[str] = []

        for index, candidate in enumerate(candidates):
            key = str(candidate.addon_url or "").strip()
            if not key:
                key = f"__unknown__:{candidate.name or candidate.source or index}"

            if key not in groups:
                groups[key] = []
                group_order.append(key)

            groups[key].append(candidate)

        for key in group_order:
            groups[key] = self._rank_candidates(groups[key])

        positions = {key: 0 for key in group_order}
        output: list[StreamCandidate] = []

        while True:
            added = False
            for key in group_order:
                position = positions[key]
                group = groups[key]
                if position >= len(group):
                    continue

                output.append(group[position])
                positions[key] = position + 1
                added = True

            if not added:
                break

        return output

    def _rank_candidates(
        self,
        candidates: list[StreamCandidate],
    ) -> list[StreamCandidate]:
        indexed = list(enumerate(candidates))
        indexed.sort(
            key=lambda pair: (
                self._source_priority(pair[1]),
                -self._quality_score(pair[1]),
                pair[0],
            )
        )
        return [candidate for _, candidate in indexed]

    def _source_priority(self, candidate: StreamCandidate) -> int:
        source = str(candidate.source or "").strip().lower()
        url = str(candidate.url or "").strip().lower()

        if url.startswith(("https://", "http://")):
            return 0
        if source == "external":
            return 1
        if source == "torrent" or url.startswith("magnet:?"):
            return 2 if self._debrid_enabled() else 5
        return 3

    @classmethod
    def _quality_score(cls, candidate: StreamCandidate) -> int:
        text = cls._candidate_text(candidate).lower()
        if "2160p" in text or re.search(r"\b4k\b", text):
            return 500
        if "1440p" in text:
            return 400
        if "1080p" in text:
            return 300
        if "720p" in text:
            return 200
        if "576p" in text:
            return 120
        if "480p" in text:
            return 100
        if "360p" in text:
            return 50
        return 0

    def _resolved_source(
        self,
        item_id: str,
        candidate: StreamCandidate,
        resolved_url: str,
    ) -> ResolvedMediaSource:
        raw_title = candidate.title or candidate.name or "Stremfin"
        addon_name = self._addon_name(candidate)
        quality = self._quality_label(candidate)
        release_type = self._release_type(candidate)
        display_name = self._display_name(
            candidate,
            addon_name,
            quality,
            release_type,
        )
        container = self._container(
            resolved_url,
            raw_title,
            candidate.description,
        )
        width, height = self._dimensions(
            raw_title,
            candidate.name,
            candidate.description,
        )

        return ResolvedMediaSource(
            id=self._source_id(item_id, candidate),
            item_id=item_id,
            url=resolved_url,
            name=display_name,
            container=container,
            protocol="Http",
            source_type=str(candidate.source or "direct"),
            addon_name=addon_name,
            addon_url=candidate.addon_url,
            quality=quality,
            release_type=release_type,
            bitrate=self._bitrate(
                raw_title,
                candidate.name,
                candidate.description,
            ),
            width=width,
            height=height,
            video_codec=self._video_codec(
                raw_title,
                candidate.name,
                candidate.description,
            ),
            audio_codec=self._audio_codec(
                raw_title,
                candidate.name,
                candidate.description,
            ),
            behavior_hints=candidate.behavior_hints,
        )

    @staticmethod
    def media_source_dto(source: ResolvedMediaSource) -> dict[str, Any]:
        media_streams: list[dict[str, Any]] = []

        if source.video_codec or source.width or source.height or source.bitrate:
            media_streams.append(
                {
                    "Codec": source.video_codec,
                    "CodecTag": None,
                    "Language": None,
                    "ColorRange": None,
                    "ColorSpace": None,
                    "ColorTransfer": None,
                    "ColorPrimaries": None,
                    "DvVersionMajor": None,
                    "DvVersionMinor": None,
                    "DvProfile": None,
                    "DvLevel": None,
                    "RpuPresentFlag": None,
                    "ElPresentFlag": None,
                    "BlPresentFlag": None,
                    "DvBlSignalCompatibilityId": None,
                    "Rotation": None,
                    "Comment": None,
                    "TimeBase": None,
                    "CodecTimeBase": None,
                    "Title": source.quality,
                    "VideoRange": None,
                    "VideoRangeType": None,
                    "VideoDoViTitle": None,
                    "AudioSpatialFormat": "None",
                    "DisplayTitle": source.quality,
                    "NalLengthSize": None,
                    "IsInterlaced": False,
                    "IsAVC": source.video_codec == "h264",
                    "ChannelLayout": None,
                    "BitRate": source.bitrate,
                    "BitDepth": None,
                    "RefFrames": None,
                    "PacketLength": None,
                    "Channels": None,
                    "SampleRate": None,
                    "IsDefault": True,
                    "IsForced": False,
                    "Height": source.height,
                    "Width": source.width,
                    "AverageFrameRate": None,
                    "RealFrameRate": None,
                    "ReferenceFrameRate": None,
                    "Profile": None,
                    "Type": "Video",
                    "AspectRatio": None,
                    "Index": 0,
                    "Score": None,
                    "IsExternal": False,
                    "DeliveryMethod": None,
                    "DeliveryUrl": None,
                    "IsExternalUrl": False,
                    "IsTextSubtitleStream": False,
                    "SupportsExternalStream": False,
                    "Path": None,
                    "PixelFormat": None,
                    "Level": None,
                    "IsAnamorphic": None,
                }
            )

        if source.audio_codec:
            media_streams.append(
                {
                    "Codec": source.audio_codec,
                    "CodecTag": None,
                    "Language": None,
                    "TimeBase": None,
                    "CodecTimeBase": None,
                    "Title": source.audio_codec.upper(),
                    "DisplayTitle": source.audio_codec.upper(),
                    "AudioSpatialFormat": "None",
                    "ChannelLayout": None,
                    "BitRate": None,
                    "Channels": None,
                    "SampleRate": None,
                    "IsDefault": True,
                    "IsForced": False,
                    "Type": "Audio",
                    "Index": len(media_streams),
                    "IsExternal": False,
                    "DeliveryMethod": None,
                    "DeliveryUrl": None,
                    "IsExternalUrl": False,
                    "IsTextSubtitleStream": False,
                    "SupportsExternalStream": False,
                    "Path": None,
                }
            )

        default_audio_index = next(
            (
                stream["Index"]
                for stream in media_streams
                if stream.get("Type") == "Audio"
            ),
            None,
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
            "DefaultAudioStreamIndex": default_audio_index,
            "DefaultSubtitleStreamIndex": None,
            "DirectStreamUrl": None,
        }

    @classmethod
    def _display_name(
        cls,
        candidate: StreamCandidate,
        addon_name: str | None,
        quality: str | None,
        release_type: str | None,
    ) -> str:
        technical = " ".join(
            part for part in (quality, release_type) if part
        ).strip()

        if addon_name:
            if technical:
                return f"{technical} • {addon_name}"

            title = cls._clean_title(candidate.title or candidate.name or "")
            if title and title.lower() != addon_name.lower():
                return f"{title} • {addon_name}"
            return addon_name

        title = cls._clean_title(candidate.title or candidate.name or "")
        if technical and title:
            if technical.lower() in title.lower():
                return title
            return f"{technical} • {title}"
        return technical or title or "Stremfin Source"

    @staticmethod
    def _clean_title(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text if len(text) <= 100 else text[:97].rstrip() + "..."

    @staticmethod
    def _addon_name(candidate: StreamCandidate) -> str | None:
        name = re.sub(r"\s+", " ", str(candidate.name or "")).strip()
        if name and len(name) <= 60:
            return name

        addon_url = str(candidate.addon_url or "").strip()
        if addon_url:
            try:
                host = urlparse(addon_url).hostname or ""
                host = host.lower()
                if host.startswith("www."):
                    host = host[4:]
                first = host.split(".")[0]
                if first:
                    return first.replace("-", " ").replace("_", " ").title()
            except ValueError:
                pass
        return None

    @classmethod
    def _quality_label(cls, candidate: StreamCandidate) -> str | None:
        text = cls._candidate_text(candidate).lower()
        if "2160p" in text or re.search(r"\b4k\b", text):
            return "4K"
        for label in ("1440p", "1080p", "720p", "576p", "480p", "360p"):
            if label in text:
                return label
        return None

    @classmethod
    def _release_type(cls, candidate: StreamCandidate) -> str | None:
        text = cls._candidate_text(candidate).lower()
        checks = (
            ("remux", "REMUX"),
            ("web-dl", "WEB-DL"),
            ("webdl", "WEB-DL"),
            ("web dl", "WEB-DL"),
            ("web-rip", "WEBRip"),
            ("webrip", "WEBRip"),
            ("web rip", "WEBRip"),
            ("blu-ray", "BluRay"),
            ("bluray", "BluRay"),
            ("blu ray", "BluRay"),
            ("hdtv", "HDTV"),
            ("dvdrip", "DVDRip"),
        )
        for token, label in checks:
            if token in text:
                return label
        if "camrip" in text or re.search(r"\bcam\b", text):
            return "CAM"
        return None

    @staticmethod
    def _candidate_text(candidate: StreamCandidate) -> str:
        return " ".join(
            str(value)
            for value in (
                candidate.title,
                candidate.name,
                candidate.description,
            )
            if value
        )

    @staticmethod
    def _container(url: str, *values: str | None) -> str | None:
        path = urlparse(url).path.lower()
        known = ("mkv", "mp4", "m4v", "avi", "mov", "webm", "ts", "m2ts")
        for extension in known:
            if path.endswith(f".{extension}"):
                return extension

        text = " ".join(value for value in values if value).lower()
        for extension in known:
            if re.search(rf"\b{re.escape(extension)}\b", text):
                return extension
        return None

    @staticmethod
    def _dimensions(*values: str | None) -> tuple[int | None, int | None]:
        text = " ".join(value for value in values if value).lower()
        if "2160p" in text or re.search(r"\b4k\b", text):
            return 3840, 2160
        if "1440p" in text:
            return 2560, 1440
        if "1080p" in text:
            return 1920, 1080
        if "720p" in text:
            return 1280, 720
        if "576p" in text:
            return 1024, 576
        if "480p" in text:
            return 854, 480
        if "360p" in text:
            return 640, 360
        return None, None

    @staticmethod
    def _video_codec(*values: str | None) -> str | None:
        text = " ".join(value for value in values if value).lower()
        if any(token in text for token in ("hevc", "h265", "h.265", "x265")):
            return "hevc"
        if any(token in text for token in ("avc", "h264", "h.264", "x264")):
            return "h264"
        if "av1" in text:
            return "av1"
        if "vp9" in text:
            return "vp9"
        if "mpeg2" in text:
            return "mpeg2video"
        return None

    @staticmethod
    def _audio_codec(*values: str | None) -> str | None:
        text = " ".join(value for value in values if value).lower()
        if "truehd" in text or "true hd" in text:
            return "truehd"
        if any(token in text for token in ("eac3", "e-ac3", "dd+", "dolby digital plus")):
            return "eac3"
        if "ac3" in text or "dolby digital" in text:
            return "ac3"
        if "dts" in text:
            return "dts"
        if "flac" in text:
            return "flac"
        if "aac" in text:
            return "aac"
        if "opus" in text:
            return "opus"
        if "mp3" in text:
            return "mp3"
        return None

    @staticmethod
    def _bitrate(*values: str | None) -> int | None:
        text = " ".join(value for value in values if value).lower()
        match = re.search(r"(\d+(?:\.\d+)?)\s*mbps", text)
        if match:
            try:
                return int(float(match.group(1)) * 1_000_000)
            except ValueError:
                pass

        match = re.search(r"(\d+(?:\.\d+)?)\s*kbps", text)
        if match:
            try:
                return int(float(match.group(1)) * 1_000)
            except ValueError:
                pass
        return None

    @staticmethod
    def _is_client_playable_url(value: str) -> bool:
        return str(value or "").lower().startswith(("http://", "https://"))

    @staticmethod
    def _url_identity(url: str) -> str:
        return str(url or "").strip()

    @classmethod
    def _candidate_identity(
        cls,
        item_id: str,
        candidate: StreamCandidate,
    ) -> str:
        if candidate.info_hash:
            raw = (
                f"torrent:{str(candidate.info_hash).lower()}:"
                f"{candidate.file_idx}:{candidate.addon_url}"
            )
        else:
            raw = (
                f"{candidate.source}:{candidate.addon_url}:{candidate.url}:"
                f"{candidate.title}:{candidate.name}"
            )
        return f"{item_id}:{raw}"

    @classmethod
    def _source_id(cls, item_id: str, candidate: StreamCandidate) -> str:
        identity = cls._candidate_identity(item_id, candidate)
        return hashlib.sha256(
            identity.encode("utf-8", errors="ignore")
        ).hexdigest()[:32]

    @staticmethod
    def _play_session_id(
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> str:
        value = f"{item_id}:{season}:{episode}"
        return hashlib.sha256(
            value.encode("utf-8", errors="ignore")
        ).hexdigest()[:32]

    def _debrid_enabled(self) -> bool:
        provider = str(
            self.settings.debrid_provider or "none"
        ).strip().lower()
        return provider not in {"", "none", "disabled", "off"}

    def _cache_key(
        self,
        item_id: str,
        season: int | None,
        episode: int | None,
    ) -> str:
        provider = str(
            self.settings.debrid_provider or "none"
        ).strip().lower()

        addon_fingerprint = hashlib.sha256(
            "|".join(self._addon_urls()).encode("utf-8", errors="ignore")
        ).hexdigest()[:12]

        # v4 invalidates the previous ordering cache while preserving stable source IDs.
        return (
            f"playback:v4:{provider}:{addon_fingerprint}:"
            f"{item_id}:{season}:{episode}"
        )

    def _addon_urls(self) -> list[str]:
        configured = self.settings.addon_urls
        raw_addons = configured.split(",") if isinstance(configured, str) else configured or []

        addons: list[str] = []
        for addon in raw_addons:
            value = str(addon or "").strip().rstrip("/")
            if not value:
                continue
            if value.endswith("/manifest.json"):
                value = value[:-len("/manifest.json")]
            if value not in addons:
                addons.append(value)
        return addons

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
