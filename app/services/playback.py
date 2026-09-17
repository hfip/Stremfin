"""Resolve Stremio streams into multiple Jellyfin MediaSources."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.config import Settings
from app.services.cache import AsyncTTLCache
from app.services.debrid import DebridResolutionError, DebridResolver
from app.services.stremio import StreamCandidate, StremioResolver


# Playback sources change more frequently than metadata.
#
# Fresh for 5 minutes, then stale-while-revalidate for another 10 minutes.
# Repeated PlaybackInfo calls from VidHub / Infuse therefore normally return
# immediately without contacting every Stremio addon again.
playback_cache = AsyncTTLCache(
    ttl_seconds=300,
    stale_seconds=600,
    maxsize=1024,
)


@dataclass(slots=True)
class ResolvedMediaSource:
    """One real Stremio source exposed as a Jellyfin MediaSource/version."""

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
    Convert Stremio stream candidates into Jellyfin MediaSources.

    Pipeline:

        Jellyfin Item
            ->
        Stremio stream candidates
            ->
        stable ranking
            ->
        parallel direct/debrid resolution
            ->
        multiple Jellyfin MediaSources

    Every returned MediaSource represents a real source from a configured
    Stremio addon. No mock/fake versions are generated.
    """

    MAX_MEDIA_SOURCES = 12

    # Do not start an unlimited number of debrid operations at once.
    MAX_PARALLEL_RESOLUTIONS = 4

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

        season_number = self._safe_int(season)
        episode_number = self._safe_int(episode)

        cache_key = self._cache_key(
            content_id,
            season_number,
            episode_number,
        )

        return await playback_cache.get_or_set(
            cache_key,
            lambda: self._resolve_uncached(
                content_id,
                season_number,
                episode_number,
            ),
        )

    async def playback_info(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        """
        Return Jellyfin PlaybackInfo containing all usable versions.

        VidHub/Infuse can use MediaSources as the available playback versions.
        Each MediaSource has a stable Id which is accepted later by
        /Videos/{id}/stream?MediaSourceId=...
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
            "ErrorCode": None if sources else "NoCompatibleStream",
        }

    async def first_playable_url(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
        media_source_id: str | None = None,
    ) -> str | None:
        """
        Resolve the exact source selected by the Jellyfin client.

        When MediaSourceId is supplied we NEVER silently switch to another
        version if that id exists in the resolved source set.
        """

        sources = await self.resolve(
            item_id,
            season,
            episode,
        )

        if not sources:
            return None

        requested_id = str(
            media_source_id or ""
        ).strip()

        if requested_id:
            for source in sources:
                if source.id == requested_id:
                    return source.url

        return sources[0].url

    async def media_sources(
        self,
        item_id: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Convenience API for Item DTO integration.

        The next Jellyfin API layer can call this method without rebuilding
        PlaybackInfo just to obtain MediaSources.
        """

        sources = await self.resolve(
            item_id,
            season,
            episode,
        )

        return [
            self.media_source_dto(source)
            for source in sources
        ]

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

        # We may need a few additional candidates because duplicate/broken
        # sources can disappear during normalization.
        candidate_window = ranked[
            : max(
                self.MAX_MEDIA_SOURCES * 2,
                self.MAX_MEDIA_SOURCES,
            )
        ]

        semaphore = asyncio.Semaphore(
            self.MAX_PARALLEL_RESOLUTIONS
        )

        async def resolve_one(
            candidate: StreamCandidate,
        ) -> tuple[
            StreamCandidate,
            str | None,
        ]:
            async with semaphore:
                resolved_url = await self._resolve_candidate(
                    candidate
                )

            return candidate, resolved_url

        resolved_pairs = await asyncio.gather(
            *[
                resolve_one(candidate)
                for candidate in candidate_window
            ],
            return_exceptions=False,
        )

        results: list[ResolvedMediaSource] = []

        seen_urls: set[str] = set()
        seen_sources: set[str] = set()

        for candidate, resolved_url in resolved_pairs:
            if len(results) >= self.MAX_MEDIA_SOURCES:
                break

            if not resolved_url:
                continue

            if not self._is_client_playable_url(
                resolved_url
            ):
                continue

            url_identity = self._url_identity(
                resolved_url
            )

            if url_identity in seen_urls:
                continue

            source_identity = self._candidate_identity(
                item_id,
                candidate,
            )

            if source_identity in seen_sources:
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
            # One bad addon/source must never make PlaybackInfo fail.
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
        Stable ordering.

        1. Immediately playable HTTP sources.
        2. External sources.
        3. Torrent/debrid sources.
        4. Unknown sources.

        Within the same source class, higher quality is shown first.
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
        ).strip().lower()

        url = str(
            candidate.url or ""
        ).strip().lower()

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

            return 5

        return 3

    @classmethod
    def _quality_score(
        cls,
        candidate: StreamCandidate,
    ) -> int:
        text = cls._candidate_text(
            candidate
        ).lower()

        if (
            "2160p" in text
            or re.search(
                r"\b4k\b",
                text,
            )
        ):
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

    # ------------------------------------------------------------------
    # Source normalization
    # ------------------------------------------------------------------

    def _resolved_source(
        self,
        item_id: str,
        candidate: StreamCandidate,
        resolved_url: str,
    ) -> ResolvedMediaSource:
        raw_title = (
            candidate.title
            or candidate.name
            or "Stremfin"
        )

        addon_name = self._addon_name(
            candidate
        )

        quality = self._quality_label(
            candidate
        )

        release_type = self._release_type(
            candidate
        )

        display_name = self._display_name(
            candidate=candidate,
            addon_name=addon_name,
            quality=quality,
            release_type=release_type,
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

        video_codec = self._video_codec(
            raw_title,
            candidate.name,
            candidate.description,
        )

        audio_codec = self._audio_codec(
            raw_title,
            candidate.name,
            candidate.description,
        )

        bitrate = self._bitrate(
            raw_title,
            candidate.name,
            candidate.description,
        )

        # IMPORTANT:
        # The source id is derived from the original Stremio candidate rather
        # than an expiring Real-Debrid/TorBox URL.
        #
        # This keeps MediaSourceId stable between PlaybackInfo and the later
        # /Videos/.../stream request.
        source_id = self._source_id(
            item_id,
            candidate,
        )

        return ResolvedMediaSource(
            id=source_id,
            item_id=item_id,
            url=resolved_url,
            name=display_name,
            container=container,
            protocol="Http",
            source_type=str(
                candidate.source or "direct"
            ),
            addon_name=addon_name,
            addon_url=candidate.addon_url,
            quality=quality,
            release_type=release_type,
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
        """
        Build one Jellyfin MediaSource/version.

        Name is deliberately human-readable because clients such as VidHub
        and Infuse may display it in their version/source picker.
        """

        media_streams: list[dict[str, Any]] = []

        if (
            source.video_codec
            or source.width
            or source.height
            or source.bitrate
        ):
            video_stream: dict[str, Any] = {
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
                "IsAVC": (
                    source.video_codec == "h264"
                ),
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

            media_streams.append(
                video_stream
            )

        if source.audio_codec:
            audio_index = len(
                media_streams
            )

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
                    "Index": audio_index,
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

            # PlaybackInfo advertises the real resolved URL. jellyfin.py also
            # accepts this MediaSourceId in the Stremfin /Videos/.../stream
            # route, allowing clients that use either Jellyfin strategy.
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

            # Stremfin currently exposes direct play/direct stream only.
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

            # Harmless Jellyfin-compatible metadata that is useful when a
            # client displays source/version information.
            "DirectStreamUrl": None,
        }

    # ------------------------------------------------------------------
    # Human-readable source/version names
    # ------------------------------------------------------------------

    @classmethod
    def _display_name(
        cls,
        candidate: StreamCandidate,
        addon_name: str | None,
        quality: str | None,
        release_type: str | None,
    ) -> str:
        """
        Prefer compact version names such as:

            4K REMUX • Torrentio
            1080p WEB-DL • MediaFusion
            720p • Example Addon

        If the addon already supplies a useful concise name/title it is
        retained as a fallback.
        """

        parts: list[str] = []

        if quality:
            parts.append(
                quality
            )

        if release_type:
            parts.append(
                release_type
            )

        technical = " ".join(
            parts
        ).strip()

        if addon_name:
            if technical:
                return (
                    f"{technical} • "
                    f"{addon_name}"
                )

            title = cls._clean_title(
                candidate.title
                or candidate.name
                or ""
            )

            if title and title.lower() != addon_name.lower():
                return (
                    f"{title} • "
                    f"{addon_name}"
                )

            return addon_name

        title = cls._clean_title(
            candidate.title
            or candidate.name
            or ""
        )

        if technical and title:
            if technical.lower() in title.lower():
                return title

            return (
                f"{technical} • "
                f"{title}"
            )

        if technical:
            return technical

        if title:
            return title

        return "Stremfin Source"

    @staticmethod
    def _clean_title(
        value: str,
    ) -> str:
        text = re.sub(
            r"\s+",
            " ",
            str(value or ""),
        ).strip()

        if len(text) > 100:
            text = (
                text[:97].rstrip()
                + "..."
            )

        return text

    @staticmethod
    def _addon_name(
        candidate: StreamCandidate,
    ) -> str | None:
        """
        Extract a readable addon label.

        Stremio streams frequently put the addon/service name in `name`.
        If not available, fall back to the addon hostname.
        """

        name = str(
            candidate.name or ""
        ).strip()

        if name:
            name = re.sub(
                r"\s+",
                " ",
                name,
            ).strip()

            if len(name) <= 60:
                return name

        addon_url = str(
            candidate.addon_url or ""
        ).strip()

        if addon_url:
            try:
                host = (
                    urlparse(
                        addon_url
                    ).hostname
                    or ""
                )

                if host:
                    host = host.lower()

                    if host.startswith("www."):
                        host = host[4:]

                    first = host.split(".")[0]

                    if first:
                        return first.replace(
                            "-",
                            " ",
                        ).replace(
                            "_",
                            " ",
                        ).title()
            except ValueError:
                pass

        return None

    @classmethod
    def _quality_label(
        cls,
        candidate: StreamCandidate,
    ) -> str | None:
        text = cls._candidate_text(
            candidate
        ).lower()

        if (
            "2160p" in text
            or re.search(
                r"\b4k\b",
                text,
            )
        ):
            return "4K"

        if "1440p" in text:
            return "1440p"

        if "1080p" in text:
            return "1080p"

        if "720p" in text:
            return "720p"

        if "576p" in text:
            return "576p"

        if "480p" in text:
            return "480p"

        if "360p" in text:
            return "360p"

        return None

    @classmethod
    def _release_type(
        cls,
        candidate: StreamCandidate,
    ) -> str | None:
        text = cls._candidate_text(
            candidate
        ).lower()

        if "remux" in text:
            return "REMUX"

        if (
            "web-dl" in text
            or "webdl" in text
            or "web dl" in text
        ):
            return "WEB-DL"

        if (
            "web-rip" in text
            or "webrip" in text
            or "web rip" in text
        ):
            return "WEBRip"

        if (
            "blu-ray" in text
            or "bluray" in text
            or "blu ray" in text
        ):
            return "BluRay"

        if "hdtv" in text:
            return "HDTV"

        if "dvdrip" in text:
            return "DVDRip"

        if (
            "camrip" in text
            or re.search(
                r"\bcam\b",
                text,
            )
        ):
            return "CAM"

        return None

    @staticmethod
    def _candidate_text(
        candidate: StreamCandidate,
    ) -> str:
        return " ".join(
            str(value)
            for value in (
                candidate.title,
                candidate.name,
                candidate.description,
            )
            if value
        )

    # ------------------------------------------------------------------
    # Media hint parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _container(
        url: str,
        *values: str | None,
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

        text = " ".join(
            value
            for value in values
            if value
        ).lower()

        for extension in known:
            if re.search(
                rf"\b{re.escape(extension)}\b",
                text,
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
            or re.search(
                r"\b4k\b",
                text,
            )
        ):
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

        if "mpeg2" in text:
            return "mpeg2video"

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
            or "dolby digital plus" in text
        ):
            return "eac3"

        if (
            "ac3" in text
            or "dolby digital" in text
        ):
            return "ac3"

        if "dts-hd" in text:
            return "dts"

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
        return str(
            value or ""
        ).lower().startswith(
            (
                "http://",
                "https://",
            )
        )

    @staticmethod
    def _url_identity(
        url: str,
    ) -> str:
        return str(
            url or ""
        ).strip()

    @classmethod
    def _candidate_identity(
        cls,
        item_id: str,
        candidate: StreamCandidate,
    ) -> str:
        """
        Stable identity before debrid resolution.

        Expiring provider URLs therefore do not create a new Jellyfin version
        every time the cache refreshes.
        """

        if candidate.info_hash:
            raw = (
                f"torrent:"
                f"{str(candidate.info_hash).lower()}:"
                f"{candidate.file_idx}"
            )
        else:
            raw = (
                f"{candidate.source}:"
                f"{candidate.addon_url}:"
                f"{candidate.url}:"
                f"{candidate.title}:"
                f"{candidate.name}"
            )

        return (
            f"{item_id}:"
            f"{raw}"
        )

    @classmethod
    def _source_id(
        cls,
        item_id: str,
        candidate: StreamCandidate,
    ) -> str:
        identity = cls._candidate_identity(
            item_id,
            candidate,
        )

        digest = hashlib.sha256(
            identity.encode(
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
    # Configuration / cache helpers
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

        addon_fingerprint = hashlib.sha256(
            "|".join(
                self._addon_urls()
            ).encode(
                "utf-8",
                errors="ignore",
            )
        ).hexdigest()[:12]

        return (
            "playback:v2:"
            f"{provider}:"
            f"{addon_fingerprint}:"
            f"{item_id}:"
            f"{season}:"
            f"{episode}"
        )

    def _addon_urls(
        self,
    ) -> list[str]:
        configured = self.settings.addon_urls

        if isinstance(configured, str):
            raw_addons = configured.split(",")
        else:
            raw_addons = configured or []

        addons: list[str] = []

        for addon in raw_addons:
            value = str(
                addon or ""
            ).strip()

            if not value:
                continue

            value = value.rstrip("/")

            if value.endswith(
                "/manifest.json"
            ):
                value = value[
                    : -len(
                        "/manifest.json"
                    )
                ]

            if value not in addons:
                addons.append(
                    value
                )

        return addons

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> int | None:
        if value is None:
            return None

        try:
            return int(value)
        except (TypeError, ValueError):
            return None
