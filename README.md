# Stremfin

Stremfin is a lightweight, open-source Python bridge that emulates the subset of the Jellyfin Server API needed by clients such as Infuse and VidHub. It resolves playable media URLs from configured Stremio addons and optionally passes links through Real-Debrid or TorBox.

## Features

- FastAPI and async `httpx` implementation.
- Jellyfin-compatible server discovery, authentication, users, views, items, and stream redirect endpoints.
- Configurable Stremio addon resolver with support for multiple addon base URLs.
- Provider service boundary for Real-Debrid and TorBox integrations.
- Docker and Docker Compose packaging.
- Responsive configuration dashboard at `/` with SQLite persistence.
- Dashboard authentication with configurable `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, and signed admin cookies.
- Separate dynamic stream-addon and subtitle-addon managers.
- Live catalog discovery from configured Stremio manifests, selected catalog persistence, artwork, provider IDs, and series episode DTOs.
- Subtitle addon resolution with Arabic/English normalization and Jellyfin external subtitle proxy routes.
- Jellyfin primary/backdrop image proxy endpoints for Infuse and VidHub.
- Episode-aware stream resolution using `series-id:s1e1` item IDs.
- No mock catalog, media URL, or hardcoded content is used; configured live addons are required.

## Quick start with Docker

```bash
cp .env.example .env
# Edit .env and configure STREMIO_ADDON_URL(S) and DEBRID_PROVIDER as needed
docker compose up --build
```

The service listens on `http://localhost:3001` from the host and on port `3000` inside the container. Open the same URL in a browser to configure live Stremio addons, debrid credentials, and playback preferences. The dashboard defaults to `admin` / `admin`; change these values before exposing the service. Settings persist in the `stremfin-data` Docker volume. Jellyfin client authentication remains the Phase 1 compatibility flow and accepts any username/password.

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

The dashboard fetches every configured addon manifest, exposes its real catalogs as checkboxes, and persists the selected catalog descriptors. `GET /api/catalog/movie` and `/api/catalog/series` query only those selected live catalogs. Jellyfin `/Items` routes normalize those results into client-friendly DTOs with posters, backdrops, overviews, years, and provider IDs. Series seasons and episodes are taken from the addon's live `/meta/{type}/{id}.json` response.

The dashboard stores separate stream and subtitle addon lists. Subtitle tracks are exposed as Jellyfin `MediaStreams` with `DeliveryMethod: External`, normalized `eng`/`ara` language codes, and proxy URLs such as `/Subtitles/{itemId}/{index}/Stream.srt`. Artwork is available through `/Items/{itemId}/Images/Primary` and `/Items/{itemId}/Images/Backdrop`.

`GET /Videos/{itemId}/stream` parses movie IDs or episode IDs such as `tt1234567:s1e2`, queries configured addons at `/stream/movie/{id}.json` or `/stream/series/{id}/1:2.json`, selects a stream, and passes it through the configured debrid resolver before returning a Jellyfin-style HTTP 302 redirect. If no live addon returns a stream, the endpoint returns 404.

## License

MIT. See [LICENSE](LICENSE).
