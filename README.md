> ⚖️ **إخلاء مسؤولية** — هذا المشروع لا يستضيف أي محتوى ولا يوفّر روابط بث أو مفاتيح. يعمل فقط على مصادر يضيفها المستخدم بنفسه، واستخدامه مسؤولية المستخدم ووفق قوانين بلده.
>
> **Disclaimer** — This project hosts no content and provides no streams or keys. It works only with sources the user supplies, and use is the user's responsibility under their local law.

---

# Stremfin

**A lightweight FastAPI bridge that turns Stremio addons into a Jellyfin / Emby compatible library.**

![License](https://img.shields.io/badge/license-MIT-green) ![Python](https://img.shields.io/badge/python-3.11%2B-blue) ![Docker](https://img.shields.io/badge/docker-ready-2496ED)

Jellyfin clients like **Infuse, VidHub, SenPlayer and Rex** are excellent players — but they can't speak to Stremio addons.
Stremfin sits in the middle: it presents the catalogs and streams of the addons you choose as a normal Jellyfin library, so your
favorite player can browse and play them with no extra app.

---

## Features

- **Multiple addons** — one URL, or a comma-separated list (`STREMIO_ADDON_URLS`).
- **Debrid support** — `none`, `real-debrid`, or `torbox`.
- **Subtitle addons** — separate `SUBTITLE_ADDON_URLS` list.
- **Metadata enrichment** — optional TMDB key or access token, with a fallback language (Arabic works).
- **SQLite cache** — catalog and metadata responses are cached locally to keep the UI fast and avoid rate limits.
- **Protected dashboard** — `/dashboard` behind a login, with settings backup and restore.
- **Optional client auth** — require a username/password for Jellyfin/Emby clients.
- **Deploy anywhere** — Docker, Docker Compose or a systemd unit; configurable request timeouts.
- **Bilingual-friendly** — client-requested language takes priority over the fallback locale.

---

## Quick start (Docker)

```bash
git clone https://github.com/hfip/Stremfin.git
cd Stremfin
cp .env.example .env      # then edit it
docker compose up -d
```

Open **http://localhost:3000/dashboard** and paste your addon URLs.

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 3000
```

---

## Configuration

| Variable | Purpose |
|---|---|
| `STREMIO_ADDON_URL` / `STREMIO_ADDON_URLS` | Addon manifest URL(s), comma-separated |
| `SUBTITLE_ADDON_URLS` | Optional subtitle addons |
| `DEBRID_PROVIDER` | `none` · `real-debrid` · `torbox` |
| `REAL_DEBRID_API_KEY` / `TORBOX_API_KEY` | Debrid credentials |
| `TMDB_API_KEY` / `TMDB_ACCESS_TOKEN` | Optional metadata enrichment |
| `TMDB_DEFAULT_LANGUAGE` | Fallback locale, e.g. `ar-SA` |
| `DATABASE_PATH` | SQLite file (default `./data/stremfin.db`) |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | Dashboard login |
| `DASHBOARD_SESSION_SECRET` | Cookie signing secret |
| `CLIENT_AUTH_ENABLED` / `CLIENT_USERNAME` / `CLIENT_PASSWORD` | Jellyfin client login |
| `REQUEST_TIMEOUT_SECONDS` | Upstream timeout (default 20) |

---

## Endpoints

| Path | What it is |
|---|---|
| `/dashboard` | Admin UI (settings, addon inspection, backup/restore) |
| `/System/Info/Public` · `/System/Info` | Jellyfin server info |
| `/Users/AuthenticateByName` | Jellyfin client login |
| `/Users/{id}/Views` · `/UserViews` | Library views |
| `/Library/VirtualFolders` | Virtual library listing |

Both `/emby/...` and plain paths are served, so Emby clients work too.

---

## Security notes

- Change `DASHBOARD_PASSWORD` and `DASHBOARD_SESSION_SECRET` before exposing the service.
- Never commit `.env` — it holds API keys and passwords.
- Put Stremfin behind a reverse proxy with TLS and authentication if it is reachable from the internet.

---

## Disclaimer

Use Stremfin only with content you own or are authorized to access. Debrid and torrent addons may be restricted by law or by
your provider's terms in some countries. This project ships no content and hosts nothing.

## License

MIT
