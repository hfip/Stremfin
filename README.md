# Stremfin

Stremfin is a lightweight, open-source Python bridge that emulates the subset of the Jellyfin Server API needed by clients such as Infuse and VidHub. It resolves playable media URLs from configured Stremio addons and optionally passes links through Real-Debrid or TorBox.

## Features

- FastAPI and async `httpx` implementation.
- Jellyfin-compatible server discovery, authentication, users, views, items, and stream redirect endpoints.
- Configurable Stremio addon resolver with support for multiple addon base URLs.
- Provider service boundary for Real-Debrid and TorBox integrations.
- Docker and Docker Compose packaging.
- Safe demo fallback stream for testing before an addon is configured.

## Quick start with Docker

```bash
cp .env.example .env
# Edit .env and configure STREMIO_ADDON_URL(S) and DEBRID_PROVIDER as needed
docker compose up --build
```

The service listens on `http://localhost:3001` from the host and on port `3000` inside the container. Point a Jellyfin-compatible client at that base URL. Authentication accepts any username and password in this Phase 1 bridge.

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

`GET /Videos/{itemId}/stream` first queries configured addons at `/stream/movie/{itemId}.json` (or the series variant), selects the first stream URL, and returns a Jellyfin-style HTTP 302 redirect. If no addon responds, it redirects to `FALLBACK_STREAM_URL`. The current implementation treats an `http://` or `https://` candidate as already playable; provider-specific debrid exchange methods are isolated in `app/services/debrid.py` for further production hardening.

## License

MIT. See [LICENSE](LICENSE).
