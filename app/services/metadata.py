"""Live Stremio manifest, catalog, and metadata aggregation."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.services.cache import metadata_cache


class MetadataService:
    """
    Aggregate metadata from configured Stremio addons.

    Catalog pagination uses a cached complete snapshot. For addons advertising
    Stremio's `skip` extra, pages are followed until exhaustion, then selected
    catalogs are merged and de-duplicated in stable configured order.
    """

    DEFAULT_CATALOG_PAGE_SIZE = 20
    MAX_CATALOG_PAGES_PER_ADDON = 250
    MAX_CATALOG_ITEMS = 20000

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _base_url(addon_url: str) -> str:
        value = str(addon_url or "").strip()
        if not value:
            return ""
        return value.removesuffix("/manifest.json").rstrip("/")

    @staticmethod
    def _safe_segment(value: Any) -> str:
        return quote(str(value or ""), safe=":@._~-")

    async def manifests(self) -> list[dict]:
        addon_urls = [
            self._base_url(url)
            for url in self.settings.addon_urls
            if self._base_url(url)
        ]
        key = "manifests:" + "|".join(addon_urls)
        return await metadata_cache.get_or_set(key, self._fetch_manifests)

    async def _fetch_manifests(self) -> list[dict]:
        addon_urls = [
            self._base_url(url)
            for url in self.settings.addon_urls
            if self._base_url(url)
        ]
        if not addon_urls:
            return []

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            tasks = [
                self._fetch_manifest_from_addon(client, base_url)
                for base_url in addon_urls
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[dict] = []
        for response in responses:
            if isinstance(response, BaseException):
                continue
            results.extend(response)
        return self._deduplicate_catalog_definitions(results)

    async def _fetch_manifest_from_addon(
        self,
        client: httpx.AsyncClient,
        base_url: str,
    ) -> list[dict]:
        try:
            response = await client.get(f"{base_url}/manifest.json")
            response.raise_for_status()
            manifest = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return []

        if not isinstance(manifest, dict):
            return []

        raw_catalogs = manifest.get("catalogs", [])
        if isinstance(raw_catalogs, dict):
            raw_catalogs = (
                raw_catalogs.get("catalogs")
                or raw_catalogs.get("items")
                or raw_catalogs.get("metas")
                or []
            )
        if not isinstance(raw_catalogs, list):
            return []

        results: list[dict] = []
        for catalog in raw_catalogs:
            if not isinstance(catalog, dict):
                continue
            catalog_id = catalog.get("id")
            if not catalog_id:
                continue
            catalog_type = self._normalize_stremio_type(catalog.get("type"))
            if catalog_type not in {"movie", "series"}:
                continue
            extra = catalog.get("extra", [])
            if not isinstance(extra, list):
                extra = []
            results.append(
                {
                    "addon_url": base_url,
                    "type": catalog_type,
                    "id": str(catalog_id),
                    "name": catalog.get("name") or str(catalog_id),
                    "extra": extra,
                }
            )
        return results

    @staticmethod
    def _deduplicate_catalog_definitions(catalogs: list[dict]) -> list[dict]:
        unique: dict[tuple[str, str, str], dict] = {}
        for catalog in catalogs:
            key = (
                str(catalog.get("addon_url") or ""),
                str(catalog.get("type") or ""),
                str(catalog.get("id") or ""),
            )
            if not all(key):
                continue
            if key not in unique:
                unique[key] = catalog
        return list(unique.values())

    async def catalog(
        self,
        kind: str,
        limit: int,
        selected: list[dict],
    ) -> list[dict]:
        """
        Return a small, fast foreground catalog window.

        The request path intentionally does not build a complete catalog
        snapshot. This keeps Jellyfin/Emby clients responsive.
        """
        endpoint_type = "series" if kind in ("series", "tv") else "movie"

        try:
            requested_limit = int(limit)
        except (TypeError, ValueError):
            requested_limit = self.DEFAULT_CATALOG_PAGE_SIZE

        requested_limit = max(1, min(requested_limit, 100))
        catalogs = self._selected_catalogs(selected, endpoint_type)
        if not catalogs:
            return []

        # Fetch only each selected catalog's base page, concurrently.
        responses = await asyncio.gather(
            *(
                self._cached_catalog_page(
                    catalog,
                    skip=0,
                    use_skip=False,
                )
                for catalog in catalogs
            ),
            return_exceptions=True,
        )

        unique: dict[str, dict] = {}
        for response in responses:
            if isinstance(response, BaseException):
                continue
            self._merge_unique(unique, response)
            if len(unique) >= requested_limit:
                break

        return list(unique.values())[:requested_limit]

    async def catalog_page(
        self,
        kind: str,
        start_index: int,
        limit: int,
        selected: list[dict],
    ) -> dict:
        """
        Fast foreground page for Jellyfin/Emby clients.

        Advanced full-catalog pagination is deliberately deferred to the
        future background-cache stage. TotalRecordCount therefore describes
        the currently available foreground window and never triggers a costly
        synchronous crawl.
        """
        try:
            start = max(0, int(start_index))
        except (TypeError, ValueError):
            start = 0

        try:
            page_limit = max(1, int(limit))
        except (TypeError, ValueError):
            page_limit = self.DEFAULT_CATALOG_PAGE_SIZE

        page_limit = min(page_limit, 100)

        # The stable foreground window is intentionally capped at 100.
        foreground_limit = min(max(start + page_limit, page_limit), 100)
        items = await self.catalog(
            kind=kind,
            limit=foreground_limit,
            selected=selected,
        )

        total = len(items)
        end = min(start + page_limit, total)

        return {
            "items": items[start:end],
            "start_index": start,
            "limit": page_limit,
            "has_more": end < total,
            "total_record_count": total,
            "is_total_exact": False,
        }

    def _selected_catalogs(
        self,
        selected: list[dict],
        endpoint_type: str,
    ) -> list[dict]:
        if not isinstance(selected, list):
            return []

        results: list[dict] = []
        for catalog in selected:
            if not isinstance(catalog, dict):
                continue
            catalog_type = self._normalize_stremio_type(catalog.get("type"))
            if catalog_type != endpoint_type:
                continue
            addon_url = self._base_url(catalog.get("addon_url") or "")
            catalog_id = catalog.get("id")
            if not addon_url or not catalog_id:
                continue
            normalized = dict(catalog)
            normalized["addon_url"] = addon_url
            normalized["type"] = catalog_type
            normalized["id"] = str(catalog_id)
            results.append(normalized)

        return self._deduplicate_catalog_definitions(results)

    async def _catalog_window(
        self,
        catalog: dict,
        required_count: int,
    ) -> list[dict]:
        """Fast compatibility helper: base page only, never a full crawl."""
        try:
            requested = max(1, min(int(required_count), 100))
        except (TypeError, ValueError):
            requested = self.DEFAULT_CATALOG_PAGE_SIZE

        items = await self._cached_catalog_page(
            catalog,
            skip=0,
            use_skip=False,
        )
        return items[:requested]

    async def _cached_catalog_page(
        self,
        catalog: dict,
        skip: int,
        use_skip: bool,
    ) -> list[dict]:
        addon_url = self._base_url(catalog.get("addon_url") or "")
        catalog_type = self._normalize_stremio_type(catalog.get("type"))
        catalog_id = str(catalog.get("id") or "")
        key = (
            "catalog:"
            f"{addon_url}:"
            f"{catalog_type}:"
            f"{catalog_id}:"
            f"skip={skip if use_skip else 'base'}"
        )
        return await metadata_cache.get_or_set(
            key,
            lambda: self._fetch_catalog_page(
                catalog,
                skip=skip,
                use_skip=use_skip,
            ),
        )

    async def _fetch_catalog(self, catalog: dict) -> list[dict]:
        return await self._fetch_catalog_page(
            catalog,
            skip=0,
            use_skip=False,
        )

    async def _fetch_catalog_page(
        self,
        catalog: dict,
        skip: int = 0,
        use_skip: bool = False,
    ) -> list[dict]:
        addon_url = self._base_url(catalog.get("addon_url") or "")
        catalog_type = self._normalize_stremio_type(catalog.get("type"))
        catalog_id = str(catalog.get("id") or "")

        if not addon_url or not catalog_type or not catalog_id:
            return []

        type_segment = self._safe_segment(catalog_type)
        id_segment = self._safe_segment(catalog_id)

        if use_skip and skip > 0:
            url = (
                f"{addon_url}/catalog/"
                f"{type_segment}/{id_segment}/skip={int(skip)}.json"
            )
        else:
            url = f"{addon_url}/catalog/{type_segment}/{id_segment}.json"

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.get(url)
                if response.status_code == 404:
                    return []
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                return []

        raw_items = self._extract_catalog_items(payload)
        normalized: list[dict] = []

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            value = self._normalize(item, catalog_type)
            if not value.get("id"):
                continue
            normalized.append(value)

        return self._deduplicate_items(normalized)

    @staticmethod
    def _extract_catalog_items(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if not isinstance(payload, dict):
            return []

        for key in ("metas", "items", "results", "catalog"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                for nested_key in ("metas", "items", "results"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return [
                            item
                            for item in nested
                            if isinstance(item, dict)
                        ]
        return []

    @staticmethod
    def _supports_skip(catalog: dict) -> bool:
        extra = catalog.get("extra", [])
        if not isinstance(extra, list):
            return False

        for entry in extra:
            if isinstance(entry, str):
                if entry.strip().lower() == "skip":
                    return True
                continue

            if not isinstance(entry, dict):
                continue

            name = str(
                entry.get("name") or entry.get("id") or ""
            ).strip().lower()
            if name == "skip":
                return True

        return False

    async def details(
        self,
        item_id: str,
        kind: str,
        addon_urls: list[str],
    ) -> dict | None:
        endpoint_type = "series" if kind in ("series", "tv") else "movie"
        item_id = str(item_id or "").strip()
        if not item_id:
            return None

        clean_addons: list[str] = []
        for addon in addon_urls:
            base_url = self._base_url(addon)
            if base_url and base_url not in clean_addons:
                clean_addons.append(base_url)

        for base_url in clean_addons:
            key = f"meta:{base_url}:{endpoint_type}:{item_id}"
            result = await metadata_cache.get_or_set(
                key,
                lambda b=base_url: self._fetch_details(
                    b,
                    endpoint_type,
                    item_id,
                ),
            )
            if result:
                return result

        return None

    async def _fetch_details(
        self,
        base_url: str,
        endpoint_type: str,
        item_id: str,
    ) -> dict | None:
        type_segment = self._safe_segment(endpoint_type)
        item_segment = self._safe_segment(item_id)
        url = f"{base_url}/meta/{type_segment}/{item_segment}.json"

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.get(url)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                return None

        item = self._extract_meta(payload)
        if not item:
            return None
        return self._normalize(item, endpoint_type)

    @staticmethod
    def _extract_meta(payload: Any) -> dict | None:
        if not isinstance(payload, dict):
            return None

        meta = payload.get("meta")
        if isinstance(meta, dict):
            return meta

        if payload.get("id") and (payload.get("name") or payload.get("title")):
            return payload

        item = payload.get("item")
        if isinstance(item, dict):
            return item

        result = payload.get("result")
        if isinstance(result, dict):
            return result

        return None

    @staticmethod
    def _normalize_stremio_type(value: Any) -> str:
        raw = str(value or "").strip().lower()

        if raw in {
            "series", "tv", "show", "shows", "tvshow", "tvshows",
        }:
            return "series"

        if raw in {"movie", "movies", "film", "films"}:
            return "movie"

        return raw

    def _normalize(self, item: dict, kind: str) -> dict:
        endpoint_type = self._normalize_stremio_type(kind)
        raw_id = item.get("id")

        imdb_id = (
            item.get("imdb_id")
            or item.get("imdbId")
            or self._provider_imdb_id(item)
        )

        if not imdb_id and str(raw_id or "").startswith("tt"):
            imdb_id = str(raw_id)

        date = (
            item.get("releaseInfo")
            or item.get("year")
            or item.get("released")
            or ""
        )

        videos = item.get("videos", [])
        if not isinstance(videos, list):
            videos = []

        poster = item.get("poster") or item.get("posterUrl") or item.get("image")
        backdrop = (
            item.get("background")
            or item.get("backdrop")
            or item.get("fanart")
        )

        return {
            "id": raw_id,
            "imdb_id": imdb_id,
            "name": (
                item.get("name")
                or item.get("title")
                or str(raw_id or "")
            ),
            "type": "Series" if endpoint_type == "series" else "Movie",
            "overview": (
                item.get("description")
                or item.get("overview")
                or ""
            ),
            "year": self._extract_year(date),
            "poster": poster,
            "backdrop": backdrop,
            "runtime": item.get("runtime"),
            "videos": videos,
            "raw": item,
        }

    @staticmethod
    def _provider_imdb_id(item: dict) -> str | None:
        provider_ids = (
            item.get("providerIds")
            or item.get("provider_ids")
            or {}
        )

        if not isinstance(provider_ids, dict):
            return None

        value = (
            provider_ids.get("imdb")
            or provider_ids.get("Imdb")
            or provider_ids.get("IMDB")
        )

        if value:
            return str(value)

        return None

    @staticmethod
    def _extract_year(value: Any) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None

        first_four = text[:4]
        if first_four.isdigit():
            year = int(first_four)
            if 1800 <= year <= 3000:
                return year

        return None

    @staticmethod
    def _identity(item: dict) -> str | None:
        imdb_id = item.get("imdb_id")
        if imdb_id:
            return f"imdb:{imdb_id}"

        item_id = item.get("id")
        if item_id:
            return f"id:{item_id}"

        return None

    def _deduplicate_items(self, items: list[dict]) -> list[dict]:
        unique: dict[str, dict] = {}

        for item in items:
            identity = self._identity(item)
            if not identity:
                continue
            if identity not in unique:
                unique[identity] = item

        return list(unique.values())

    def _merge_unique(
        self,
        destination: dict[str, dict],
        items: list[dict],
    ) -> None:
        for item in items:
            identity = self._identity(item)
            if not identity:
                continue
            if identity not in destination:
                destination[identity] = item
