"""Async multi-language TMDB metadata provider for Stremfin.

This service is intentionally independent from Jellyfin DTOs and Stremio
catalog routing. It resolves TMDB IDs, fetches localized movie/TV metadata,
and provides English field-level fallback when localized text is missing.
"""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from typing import Any

import httpx


TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"

_IMDB_RE = re.compile(r"^tt\d+$", re.IGNORECASE)
_TMDB_RE = re.compile(r"^(?:tmdb:)?(\d+)$", re.IGNORECASE)


def normalize_language(value: str | None, default: str = "en-US") -> str:
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return default

    first = raw.split(",", 1)[0].split(";", 1)[0].strip()
    if not first:
        return default

    parts = first.split("-")
    language = parts[0].lower()
    if len(language) != 2 or not language.isalpha():
        return default

    if len(parts) > 1 and len(parts[1]) == 2 and parts[1].isalpha():
        return f"{language}-{parts[1].upper()}"

    if language == "ar":
        return "ar-SA"
    if language == "en":
        return "en-US"
    return language


def image_url(path: str | None) -> str | None:
    value = str(path or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    if not value.startswith("/"):
        value = "/" + value
    return TMDB_IMAGE_BASE + value


def _missing_text(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def _merge_localized(
    localized: dict[str, Any],
    fallback: dict[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    result = dict(localized)
    for field in fields:
        if _missing_text(result.get(field)) and not _missing_text(
            fallback.get(field)
        ):
            result[field] = fallback[field]
    return result


class TMDBProvider:
    def __init__(
        self,
        api_key: str = "",
        access_token: str = "",
        timeout_seconds: float = 12.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.access_token = str(access_token or "").strip()
        self.timeout_seconds = float(timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key or self.access_token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Stremfin/0.3.0",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _params(self, **params: Any) -> dict[str, Any]:
        clean = {
            key: value
            for key, value in params.items()
            if value is not None and value != ""
        }
        if self.api_key and not self.access_token:
            clean["api_key"] = self.api_key
        return clean

    async def _get(
        self,
        path: str,
        **params: Any,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=self._headers(),
            ) as client:
                response = await client.get(
                    f"{TMDB_API_BASE}{path}",
                    params=self._params(**params),
                )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def resolve_id(
        self,
        external_id: str,
        media_type: str | None = None,
        language: str = "en-US",
    ) -> tuple[str, int] | None:
        value = str(external_id or "").strip()
        if not value:
            return None

        requested_type = str(media_type or "").strip().lower()
        if requested_type == "series":
            requested_type = "tv"

        tmdb_match = _TMDB_RE.fullmatch(value)
        if tmdb_match:
            if requested_type in {"movie", "tv"}:
                return requested_type, int(tmdb_match.group(1))
            return None

        if not _IMDB_RE.fullmatch(value):
            return None

        data = await self._get(
            f"/find/{value}",
            external_source="imdb_id",
            language=normalize_language(language),
        )
        if not data:
            return None

        if requested_type in {"movie", "tv"}:
            key = "movie_results" if requested_type == "movie" else "tv_results"
            results = data.get(key) or []
            if results and isinstance(results[0], dict):
                return requested_type, int(results[0]["id"])
            return None

        for key, kind in (("movie_results", "movie"), ("tv_results", "tv")):
            results = data.get(key) or []
            if results and isinstance(results[0], dict):
                return kind, int(results[0]["id"])
        return None

    async def search_id(
        self,
        title: str,
        media_type: str,
        year: int | None = None,
        language: str = "en-US",
        original_title: str | None = None,
    ) -> tuple[str, int] | None:
        """Resolve addon/private IDs conservatively using title + year."""
        kind = "tv" if str(media_type).lower() in {"tv", "series"} else "movie"
        queries: list[str] = []
        for value in (title, original_title):
            value = str(value or "").strip()
            if value and value not in queries:
                queries.append(value)
        if not queries:
            return None

        def norm(value: Any) -> str:
            return " ".join(re.findall(r"\w+", str(value or "").casefold(), flags=re.UNICODE))

        best: tuple[float, int] | None = None
        for query in queries:
            params: dict[str, Any] = {
                "query": query,
                "language": normalize_language(language),
                "include_adult": "false",
            }
            if year:
                params["year" if kind == "movie" else "first_air_date_year"] = int(year)
            data = await self._get(f"/search/{kind}", **params)
            for item in ((data or {}).get("results") or [])[:8]:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                title_key = "title" if kind == "movie" else "name"
                original_key = "original_title" if kind == "movie" else "original_name"
                wanted = norm(query)
                title_score = max(
                    SequenceMatcher(None, wanted, norm(item.get(title_key))).ratio(),
                    SequenceMatcher(None, wanted, norm(item.get(original_key))).ratio(),
                )
                date_key = "release_date" if kind == "movie" else "first_air_date"
                date = str(item.get(date_key) or "")
                result_year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
                delta = abs(result_year - int(year)) if year and result_year else None
                if title_score < 0.82 or (year and delta is not None and delta > 1):
                    continue
                score = title_score + (0.18 if delta == 0 else 0.08 if delta == 1 else 0.0)
                if best is None or score > best[0]:
                    best = (score, int(item["id"]))
        return (kind, best[1]) if best else None

    async def details_from_search(
        self,
        title: str,
        media_type: str,
        year: int | None = None,
        language: str = "en-US",
        original_title: str | None = None,
    ) -> dict[str, Any] | None:
        resolved = await self.search_id(title, media_type, year, language, original_title)
        if resolved is None:
            return None
        kind, tmdb_id = resolved
        return await self.details(tmdb_id, kind, language=language)

    async def details(
        self,
        tmdb_id: int,
        media_type: str,
        language: str = "en-US",
    ) -> dict[str, Any] | None:
        kind = "tv" if str(media_type).lower() in {"tv", "series"} else "movie"
        lang = normalize_language(language)

        localized_task = self._get(
            f"/{kind}/{int(tmdb_id)}",
            language=lang,
            append_to_response="external_ids",
        )
        fallback_task = None
        if lang.lower() != "en-us":
            fallback_task = self._get(
                f"/{kind}/{int(tmdb_id)}",
                language="en-US",
                append_to_response="external_ids",
            )

        if fallback_task is None:
            localized = await localized_task
            fallback = None
        else:
            localized, fallback = await asyncio.gather(
                localized_task,
                fallback_task,
            )

        if not localized:
            localized = fallback
        if not localized:
            return None

        if fallback:
            localized = _merge_localized(
                localized,
                fallback,
                (
                    "title",
                    "name",
                    "overview",
                    "tagline",
                ),
            )

        return self._canonical_details(localized, kind, lang)

    async def details_from_external_id(
        self,
        external_id: str,
        media_type: str | None = None,
        language: str = "en-US",
    ) -> dict[str, Any] | None:
        resolved = await self.resolve_id(
            external_id,
            media_type=media_type,
            language=language,
        )
        if resolved is None:
            return None
        kind, tmdb_id = resolved
        return await self.details(
            tmdb_id,
            kind,
            language=language,
        )

    def _canonical_details(
        self,
        data: dict[str, Any],
        kind: str,
        language: str,
    ) -> dict[str, Any]:
        external = data.get("external_ids") or {}
        release_date = (
            data.get("release_date")
            if kind == "movie"
            else data.get("first_air_date")
        )
        title = (
            data.get("title")
            if kind == "movie"
            else data.get("name")
        )
        original_title = (
            data.get("original_title")
            if kind == "movie"
            else data.get("original_name")
        )

        year: int | None = None
        if isinstance(release_date, str) and len(release_date) >= 4:
            try:
                year = int(release_date[:4])
            except ValueError:
                pass

        runtime_minutes: int | None = None
        if kind == "movie":
            try:
                runtime_minutes = int(data.get("runtime") or 0) or None
            except (TypeError, ValueError):
                pass
        else:
            runtimes = data.get("episode_run_time") or []
            if runtimes:
                try:
                    runtime_minutes = int(runtimes[0]) or None
                except (TypeError, ValueError):
                    pass

        provider_ids = {
            "Tmdb": str(data["id"]),
        }
        imdb_id = external.get("imdb_id") or data.get("imdb_id")
        if imdb_id:
            provider_ids["Imdb"] = str(imdb_id)

        return {
            "provider": "tmdb",
            "language": language,
            "type": "series" if kind == "tv" else "movie",
            "tmdb_id": int(data["id"]),
            "id": str(imdb_id or f"tmdb:{data['id']}"),
            "name": str(title or original_title or "").strip(),
            "original_name": str(original_title or "").strip() or None,
            "overview": str(data.get("overview") or "").strip(),
            "tagline": str(data.get("tagline") or "").strip(),
            "year": year,
            "release_date": release_date or None,
            "poster": image_url(data.get("poster_path")),
            "background": image_url(data.get("backdrop_path")),
            "genres": [
                str(item.get("name"))
                for item in (data.get("genres") or [])
                if isinstance(item, dict) and item.get("name")
            ],
            "runtime_minutes": runtime_minutes,
            "rating": data.get("vote_average"),
            "provider_ids": provider_ids,
            "number_of_seasons": data.get("number_of_seasons"),
            "number_of_episodes": data.get("number_of_episodes"),
            "status": data.get("status"),
        }
