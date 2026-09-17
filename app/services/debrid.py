"""Debrid link resolution with provider-specific extension points."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import Settings
from app.services.cache import AsyncTTLCache


# Resolved debrid links are generally reusable for a limited period.
#
# Keep them fresh for 10 minutes. A stale resolved URL may still be returned
# for another 10 minutes while Stremfin refreshes it in the background.
debrid_cache = AsyncTTLCache(
    ttl_seconds=600,
    stale_seconds=600,
    maxsize=512,
)


class DebridResolutionError(RuntimeError):
    """Raised when a configured debrid provider cannot resolve a source."""


class DebridResolver:
    """
    Resolve Stremio stream candidates into client-playable URLs.

    Current provider integrations:

    - none
    - Real-Debrid
    - TorBox

    Design goals:

    - direct HTTP(S) playback remains fast
    - debrid API calls are cached
    - provider failures do not corrupt the Jellyfin API response
    - magnet/torrent sources remain available for provider resolution
    - secrets are never included in cache keys or returned errors
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(self, url: str) -> str:
        """
        Return a playable URL for one stream source.

        Direct HTTP(S) links are already playable by Jellyfin-compatible
        clients and are therefore passed through unchanged.

        Magnet/torrent/debrid links are sent to the configured provider.
        """

        source = str(url or "").strip()

        if not source:
            raise DebridResolutionError(
                "Cannot resolve an empty stream URL"
            )

        provider = self._provider()

        # A normal web URL does not need an unnecessary debrid round-trip.
        if self._is_direct_http(source):
            return source

        # With debrid disabled, preserve the source exactly as Stremio
        # returned it. The playback layer can then decide whether the client
        # can consume that protocol.
        if provider == "none":
            return source

        cache_key = self._cache_key(
            provider,
            source,
        )

        return await debrid_cache.get_or_set(
            cache_key,
            lambda: self._resolve_uncached(
                provider,
                source,
            ),
        )

    # ------------------------------------------------------------------
    # Provider routing
    # ------------------------------------------------------------------

    async def _resolve_uncached(
        self,
        provider: str,
        source: str,
    ) -> str:
        if provider == "real-debrid":
            return await self._real_debrid(
                source
            )

        if provider == "torbox":
            return await self._torbox(
                source
            )

        # Unknown providers should not silently trigger arbitrary network
        # behaviour. Preserve the original source for backwards
        # compatibility with existing Stremfin settings.
        return source

    # ------------------------------------------------------------------
    # Real-Debrid
    # ------------------------------------------------------------------

    async def _real_debrid(
        self,
        source: str,
    ) -> str:
        """
        Resolve a link through Real-Debrid.

        Real-Debrid's unrestrict endpoint works with supported host links.
        Magnet links require torrent workflow APIs rather than the simple
        unrestrict endpoint, so they are handled separately.
        """

        api_key = str(
            self.settings.real_debrid_api_key
            or ""
        ).strip()

        if not api_key:
            return source

        if self._is_magnet(source):
            return await self._real_debrid_magnet(
                source,
                api_key,
            )

        endpoint = (
            "https://api.real-debrid.com/"
            "rest/1.0/unrestrict/link"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        data = {
            "link": source,
        }

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    data=data,
                )

                response.raise_for_status()
                payload = response.json()

            except (
                httpx.HTTPError,
                ValueError,
                TypeError,
            ) as exc:
                raise DebridResolutionError(
                    "Real-Debrid could not resolve the stream"
                ) from exc

        resolved = self._extract_url(
            payload,
            keys=(
                "download",
                "url",
                "link",
            ),
        )

        if not resolved:
            raise DebridResolutionError(
                "Real-Debrid returned no playable URL"
            )

        return resolved

    async def _real_debrid_magnet(
        self,
        magnet: str,
        api_key: str,
    ) -> str:
        """
        Resolve a magnet using the Real-Debrid torrent workflow:

        1. Add magnet
        2. Select files
        3. Read torrent information
        4. Unrestrict the selected generated link

        Stremio may append fileIdx to the magnet. When available, Stremfin
        tries to select the corresponding torrent file rather than blindly
        selecting an unrelated file.
        """

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        base_url = (
            "https://api.real-debrid.com/"
            "rest/1.0"
        )

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            try:
                add_response = await client.post(
                    f"{base_url}/torrents/addMagnet",
                    headers=headers,
                    data={
                        "magnet": magnet,
                    },
                )

                add_response.raise_for_status()
                add_payload = add_response.json()

                torrent_id = add_payload.get("id")

                if not torrent_id:
                    raise DebridResolutionError(
                        "Real-Debrid returned no torrent id"
                    )

                info_response = await client.get(
                    f"{base_url}/torrents/info/{torrent_id}",
                    headers=headers,
                )

                info_response.raise_for_status()
                info_payload = info_response.json()

                selected_file_ids = self._real_debrid_file_ids(
                    magnet,
                    info_payload,
                )

                if not selected_file_ids:
                    raise DebridResolutionError(
                        "Real-Debrid torrent contains no selectable files"
                    )

                select_response = await client.post(
                    f"{base_url}/torrents/selectFiles/{torrent_id}",
                    headers=headers,
                    data={
                        "files": ",".join(
                            str(file_id)
                            for file_id in selected_file_ids
                        )
                    },
                )

                select_response.raise_for_status()

                info_response = await client.get(
                    f"{base_url}/torrents/info/{torrent_id}",
                    headers=headers,
                )

                info_response.raise_for_status()
                info_payload = info_response.json()

                links = info_payload.get(
                    "links",
                    [],
                )

                if not isinstance(links, list):
                    links = []

                links = [
                    str(link).strip()
                    for link in links
                    if str(link or "").strip()
                ]

                if not links:
                    raise DebridResolutionError(
                        "Real-Debrid torrent is not ready for playback"
                    )

                # When only one file was selected there should normally be
                # one generated host link.
                restricted_link = links[0]

                unrestrict_response = await client.post(
                    f"{base_url}/unrestrict/link",
                    headers=headers,
                    data={
                        "link": restricted_link,
                    },
                )

                unrestrict_response.raise_for_status()
                unrestrict_payload = (
                    unrestrict_response.json()
                )

            except DebridResolutionError:
                raise

            except (
                httpx.HTTPError,
                ValueError,
                TypeError,
            ) as exc:
                raise DebridResolutionError(
                    "Real-Debrid could not resolve the torrent"
                ) from exc

        resolved = self._extract_url(
            unrestrict_payload,
            keys=(
                "download",
                "url",
                "link",
            ),
        )

        if not resolved:
            raise DebridResolutionError(
                "Real-Debrid returned no playable torrent URL"
            )

        return resolved

    def _real_debrid_file_ids(
        self,
        magnet: str,
        info_payload: Any,
    ) -> list[int]:
        """
        Choose torrent files.

        If Stremio supplied fileIdx, attempt to map that zero-based index to
        the provider's file list. Otherwise select the largest video-like
        file, which is normally the main movie/episode.
        """

        if not isinstance(info_payload, dict):
            return []

        files = info_payload.get(
            "files",
            [],
        )

        if not isinstance(files, list):
            return []

        normalized_files = [
            item
            for item in files
            if isinstance(item, dict)
            and item.get("id") is not None
        ]

        if not normalized_files:
            return []

        file_idx = self._magnet_file_idx(
            magnet
        )

        if (
            file_idx is not None
            and 0 <= file_idx < len(normalized_files)
        ):
            selected = normalized_files[
                file_idx
            ]

            try:
                return [
                    int(selected["id"])
                ]
            except (
                TypeError,
                ValueError,
            ):
                pass

        video_extensions = (
            ".mkv",
            ".mp4",
            ".m4v",
            ".avi",
            ".mov",
            ".webm",
            ".ts",
            ".m2ts",
        )

        video_files = []

        for item in normalized_files:
            path = str(
                item.get("path")
                or ""
            ).lower()

            if path.endswith(
                video_extensions
            ):
                video_files.append(item)

        candidates = (
            video_files
            if video_files
            else normalized_files
        )

        def size_of(
            item: dict,
        ) -> int:
            try:
                return int(
                    item.get("bytes")
                    or 0
                )
            except (
                TypeError,
                ValueError,
            ):
                return 0

        selected = max(
            candidates,
            key=size_of,
        )

        try:
            return [
                int(selected["id"])
            ]

        except (
            TypeError,
            ValueError,
        ):
            return []

    # ------------------------------------------------------------------
    # TorBox
    # ------------------------------------------------------------------

    async def _torbox(
        self,
        source: str,
    ) -> str:
        api_key = str(
            self.settings.torbox_api_key
            or ""
        ).strip()

        if not api_key:
            return source

        endpoint = (
            "https://api.torbox.app/"
            "v1/api/stream/resolve"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        payload = {
            "url": source,
        }

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                )

                response.raise_for_status()
                body = response.json()

            except (
                httpx.HTTPError,
                ValueError,
                TypeError,
            ) as exc:
                raise DebridResolutionError(
                    "TorBox could not resolve the stream"
                ) from exc

        resolved = self._extract_url(
            body,
            keys=(
                "url",
                "download_url",
                "download",
                "link",
            ),
        )

        if not resolved:
            raise DebridResolutionError(
                "TorBox returned no playable URL"
            )

        return resolved

    # ------------------------------------------------------------------
    # Provider / protocol helpers
    # ------------------------------------------------------------------

    def _provider(self) -> str:
        value = str(
            self.settings.debrid_provider
            or "none"
        ).strip().lower()

        aliases = {
            "realdebrid": "real-debrid",
            "real_debrid": "real-debrid",
            "rd": "real-debrid",
            "tor-box": "torbox",
            "tor_box": "torbox",
        }

        return aliases.get(
            value,
            value,
        )

    @staticmethod
    def _is_direct_http(
        value: str,
    ) -> bool:
        lowered = value.lower()

        return lowered.startswith(
            (
                "http://",
                "https://",
            )
        )

    @staticmethod
    def _is_magnet(
        value: str,
    ) -> bool:
        return value.lower().startswith(
            "magnet:?"
        )

    # ------------------------------------------------------------------
    # Magnet helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _magnet_file_idx(
        magnet: str,
    ) -> int | None:
        try:
            parsed = urlparse(
                magnet
            )

            query = parse_qs(
                parsed.query
            )

            values = (
                query.get("fileIdx")
                or query.get("fileidx")
                or []
            )

            if not values:
                return None

            return int(values[0])

        except (
            TypeError,
            ValueError,
        ):
            return None

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_url(
        cls,
        payload: Any,
        keys: tuple[str, ...],
    ) -> str | None:
        """
        Extract a playable URL from provider responses.

        Provider APIs occasionally wrap the useful object in data/result.
        """

        if not isinstance(payload, dict):
            return None

        for key in keys:
            value = payload.get(key)

            if isinstance(value, str):
                value = value.strip()

                if value:
                    return value

        for wrapper in (
            "data",
            "result",
        ):
            nested = payload.get(wrapper)

            if isinstance(nested, dict):
                resolved = cls._extract_url(
                    nested,
                    keys,
                )

                if resolved:
                    return resolved

        return None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(
        provider: str,
        source: str,
    ) -> str:
        """
        Cache keys contain provider + source only.

        API keys are intentionally never included.
        """

        return (
            "debrid:"
            f"{provider}:"
            f"{source}"
        )
