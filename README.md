# Stremfin

Stremfin is a lightweight, open-source Python bridge that emulates the subset of the Jellyfin Server API needed by clients such as Infuse and VidHub. It resolves playable media URLs from configured Stremio addons and optionally passes links through Real-Debrid or TorBox.

## Features

- FastAPI and async `httpx` implementation.
- Jellyfin-compatible server discovery, authentication, users, views, items, and stream redirect endpoints.
- Configurable Stremio addon resolver with support for multiple addon base URLs.
- Provider service boundary for Real-Debrid and TorBox integrations.
- Docker and Docker Compose packaging.
- Responsive configuration dashboard at `/` with SQLite persistence.
- TMDB trending catalogs with Cinemeta fallback, artwork, provider IDs, and series episode DTOs.
- Episode-aware stream resolution using `series-id:s1e1` item IDs.
- Safe demo fallback stream for testing before an addon is configured.

## Quick start with Docker

```bash
cp .env.example .env
# Edit .env and configure STREMIO_ADDON_URL(S) and DEBRID_PROVIDER as needed
docker compose up --build
```

The service listens on `http://localhost:3001` from the host and on port `3000` inside the container. Open the same URL in a browser to configure addons, TMDB, debrid credentials, and playback preferences. Settings persist in the `stremfin-data` Docker volume. Point a Jellyfin-compatible client at that base URL; authentication accepts any username and password.

## Local development

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 3000
```

Useful checks:

```bash
curl http://localhost:3000/health
curl http://localhost:3000/System/Info/Public
curl -X POST http://localhost:3000/Users/AuthenticateByName \
  -H 'Content-Type: application/json' -d '{"Username":"demo","Pw":"demo"}'
```

## API notes

`GET /api/catalog/movie` and `/api/catalog/series` use TMDB trending data when `TMDB_API_KEY` is configured and otherwise query Cinemeta. Jellyfin `/Items` routes normalize those results into client-friendly DTOs with posters, backdrops, overviews, years, and IMDb IDs. Series children are exposed as season-one episode DTOs under a series `ParentId`.

`GET /Videos/{itemId}/stream` parses movie IDs or episode IDs such as `tt1234567:s1e2`, queries configured addons at `/stream/movie/{id}.json` or `/stream/series/{id}/1:2.json`, selects a stream, and passes it through the configured debrid resolver before returning a Jellyfin-style HTTP 302 redirect. If no addon responds, it redirects to `FALLBACK_STREAM_URL`.

## License

MIT. See [LICENSE](LICENSE).
