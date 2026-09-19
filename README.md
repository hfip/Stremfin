::: {align="center"}
# Stremfin

### Jellyfin / Emby API Bridge for Stremio Addons

**A lightweight protocol bridge that brings Stremio addon catalogs,
metadata, streams, artwork, and subtitles to Jellyfin/Emby-compatible
media clients.**

[![Version](https://img.shields.io/badge/version-v0.3.0-5865F2?style=for-the-badge)](https://github.com/hfip/Stremfin)
[![License](https://img.shields.io/badge/license-MIT-22C55E?style=for-the-badge)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](Dockerfile)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](requirements.txt)
[![FastAPI](https://img.shields.io/badge/FastAPI-Async-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Jellyfin](https://img.shields.io/badge/Jellyfin-Compatible-00A4DC?style=for-the-badge)](https://jellyfin.org/)
[![Emby](https://img.shields.io/badge/Emby-Compatible-52B54B?style=for-the-badge)](https://emby.media/)
[![Status](https://img.shields.io/badge/status-Active%20%7C%20Fast-8B5CF6?style=for-the-badge)](https://github.com/hfip/Stremfin)

**Python · FastAPI · SQLite · Docker · Async HTTP**
:::

------------------------------------------------------------------------

## Table of Contents

-   [About](#about)
-   [How It Works](#how-it-works)
-   [Features](#features)
-   [Supported Clients](#supported-clients)
-   [Quick Start with Docker](#quick-start-with-docker)
-   [Docker Compose](#docker-compose)
-   [Configuration](#configuration)
-   [Dashboard & Addons](#dashboard--addons)
-   [Local Development](#local-development)
-   [Health & API Checks](#health--api-checks)
-   [Data & Persistence](#data--persistence)
-   [Version History](#version-history)
-   [Security Notes](#security-notes)
-   [Disclaimer](#disclaimer)
-   [License](#license)

------------------------------------------------------------------------

## About

**Stremfin** is an open-source media protocol bridge written in Python
with FastAPI.

It presents a Jellyfin/Emby-compatible API to supported media clients
while resolving live catalogs, metadata, artwork, subtitles, and
playable sources from configured **Stremio addons**.

This makes it possible to use the Stremio addon ecosystem through
clients that understand Jellyfin/Emby APIs, including **Infuse,
SenPlayer, Rex, and VidHub**.

Stremfin is a bridge --- not a media server and not a content provider.
It does not ship with a mock catalog or hardcoded media library. Data
exposed to clients comes from the Stremio addons configured by the user.

------------------------------------------------------------------------

## How It Works

``` text
Infuse / SenPlayer / Rex / VidHub
              │
      Jellyfin / Emby API
              │
              ▼
           Stremfin
 FastAPI · SQLite · Cache
 Metadata · Streams · Subtitles
              │
       Stremio Addon API
              │
              ▼
   Configured Stremio Addons
 Catalog · Meta · Stream · Subs
```

Stremfin normalizes Stremio metadata into client-friendly Jellyfin/Emby
DTOs and keeps protocol-specific behavior inside the bridge.

------------------------------------------------------------------------

## Features

### Media & Metadata

-   Jellyfin/Emby-compatible discovery, authentication, views, items,
    seasons, episodes, and playback endpoints.
-   Live Stremio catalog and metadata resolution.
-   Movie, series, season, and episode navigation.
-   Posters, backdrops, provider IDs, metadata, and ClearLogo support
    where available.
-   Deterministic media/item identifiers for stable client navigation.
-   No mock catalog or hardcoded media content.

### Multi-Source Playback

-   Multiple Stremio stream addons at the same time.
-   User-controlled source priority.
-   Quality-aware ordering including **4K → 1440p → 1080p → 720p** where
    detected.
-   Episode-aware stream resolution.
-   Optional **Real-Debrid** and **TorBox** integration.
-   Jellyfin-style HTTP redirect playback flow.

### External Subtitles

-   Multiple subtitle addons.
-   Arabic and English language normalization.
-   Multiple subtitle versions retained.
-   Jellyfin external subtitle `MediaStreams`.
-   Video-scoped subtitle compatibility routes.
-   **HTTP 302 Redirect** delivery for external subtitle files.

### Modern Web Dashboard

-   Responsive Web UI.
-   Arabic and English.
-   Full **RTL / LTR** support.
-   Dark and Light modes.
-   Mobile-friendly glass interface.
-   Add, remove, and reorder Stream and Subtitle addons.
-   Live Stremio Manifest validation.
-   Real addon name + domain display.
-   Online / Offline state and response latency.
-   Live catalog discovery and selection.
-   Debrid and playback preferences.
-   SQLite persistence.

### Performance

-   Async FastAPI/httpx architecture.
-   Concurrent addon resolution.
-   LRU/TTL caching for metadata and catalog operations.
-   Lightweight Python 3.11 slim Docker image.
-   Manifest status caching.

------------------------------------------------------------------------

## Supported Clients

  ------------------------------------------------------------------------------
  Client          Platform       Catalogs /        Playback         External
                                  Metadata                         Subtitles
  --------------- ----------- ---------------- ---------------- ----------------
  **Infuse**      Apple TV /         ✅               ✅               ✅
                  iOS / macOS                                   

  **SenPlayer**   Apple              ✅               ✅               ✅
                  platforms                                     

  **Rex**         Apple              ✅               ✅               ✅
                  platforms                                     

  **VidHub**      Apple              ✅               ✅               ✅
                  platforms                                     
  ------------------------------------------------------------------------------

> Client behavior can vary between app versions. Stremfin focuses on the
> Jellyfin/Emby API subset required by these clients.

------------------------------------------------------------------------

## Quick Start with Docker

### Requirements

-   Docker
-   A Stremio addon Manifest URL
-   Host port **3001**

### 1. Clone

``` bash
git clone https://github.com/hfip/Stremfin.git
cd Stremfin
```

### 2. Configure

``` bash
cp .env.example .env
```

Change the dashboard credentials and session secret before exposing
Stremfin outside a trusted local network.

### 3. Build & Run

``` bash
docker build -t stremfin .
docker run -d   --name stremfin   --restart unless-stopped   -p 3001:3000   --env-file .env   -v stremfin-data:/app/data   stremfin
```

### 4. Open

``` text
http://YOUR-SERVER-IP:3001
```

The container listens on **3000** internally and maps to **3001** on the
host.

------------------------------------------------------------------------

## Docker Compose

``` bash
cp .env.example .env
docker compose up -d --build
```

Logs:

``` bash
docker compose logs -f stremfin
```

Stop:

``` bash
docker compose down
```

The `stremfin-data` volume preserves the SQLite database across
container recreation.

------------------------------------------------------------------------

## Configuration

  Variable                     Default / Example            Description
  ---------------------------- ---------------------------- -------------------------------------
  `APP_NAME`                   `Stremfin`                   Application name
  `APP_VERSION`                `0.3.0`                      Application version
  `SERVER_NAME`                `Stremfin Jellyfin Bridge`   Bridge/server name
  `SERVER_ID`                  `stremfin-local`             Stable server identifier
  `PUBLIC_BASE_URL`            `http://localhost:3000`      Base server URL
  `STREMIO_ADDON_URL`          empty                        Optional single addon URL
  `STREMIO_ADDON_URLS`         empty                        Comma-separated stream addon URLs
  `SUBTITLE_ADDON_URLS`        empty                        Comma-separated subtitle addon URLs
  `DEBRID_PROVIDER`            `none`                       `none`, `real-debrid`, or `torbox`
  `REAL_DEBRID_API_KEY`        empty                        Real-Debrid API key
  `TORBOX_API_KEY`             empty                        TorBox API key
  `DATABASE_PATH`              `./data/stremfin.db`         SQLite database
  `DASHBOARD_USERNAME`         `admin`                      Dashboard username
  `DASHBOARD_PASSWORD`         `admin`                      Dashboard password
  `DASHBOARD_SESSION_SECRET`   change it                    Session signing secret
  `REQUEST_TIMEOUT_SECONDS`    `20`                         Outbound request timeout

> `.env` is for server-level settings. Dashboard-managed settings are
> persisted in SQLite.

------------------------------------------------------------------------

## Dashboard & Addons

Open `http://YOUR-SERVER-IP:3001`.

The dashboard manages Stream Addons, Subtitle Addons, live catalog
discovery, Debrid configuration, and playback preferences.

For configured addons it can show the **real addon name**, **domain**,
**Online / Offline state**, **response latency**, and Manifest
capabilities. Addon order controls source priority while Stremfin
preserves usable multi-source results.

------------------------------------------------------------------------

## Local Development

Requires **Python 3.11+**.

``` bash
git clone https://github.com/hfip/Stremfin.git
cd Stremfin
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 3000
```

Windows PowerShell:

``` powershell
.venv\Scripts\Activate.ps1
```

------------------------------------------------------------------------

## Health & API Checks

``` bash
curl http://localhost:3001/health
curl http://localhost:3001/System/Info/Public
```

The dashboard also uses authenticated management APIs for settings,
addon inspection, addon status, and catalog discovery.

------------------------------------------------------------------------

## Data & Persistence

Dashboard configuration is stored in:

``` text
./data/stremfin.db
```

With Docker Compose, `/app/data` is backed by the persistent
`stremfin-data` volume.

------------------------------------------------------------------------

## Version History

### v0.3.0 --- Current

Current highlights: - Jellyfin/Emby compatibility routes for supported
clients. - Multi-addon stream resolution and quality-aware ordering. -
External subtitles with HTTP 302 delivery. - Live catalogs, metadata,
artwork, seasons, and episodes. - Persistent SQLite dashboard
settings. - Arabic/English responsive Web UI with RTL. - Manifest
validation and addon status inspection. - Docker and Docker Compose
deployment.

> Stremfin remains under active development and client compatibility may
> continue to evolve.

------------------------------------------------------------------------

## Security Notes

-   Change the default dashboard username and password.
-   Set a long random `DASHBOARD_SESSION_SECRET`.
-   Never commit `.env` files or API keys.
-   Use an HTTPS reverse proxy if exposing Stremfin to the internet.
-   Back up the persistent data volume when needed.

------------------------------------------------------------------------

## Disclaimer

Stremfin is a **protocol bridge**. It does **not** host, upload,
distribute, bundle, or provide media files and does not include a
built-in media catalog.

Users are responsible for the addons, servers, services, URLs, and media
sources they configure and for complying with applicable laws and
service terms.

Stremfin is an independent open-source project and is not affiliated
with or endorsed by Stremio, Jellyfin, Emby, Infuse, SenPlayer, Rex,
VidHub, Real-Debrid, or TorBox.

------------------------------------------------------------------------

## License

Stremfin is released under the **MIT License**. See
[`LICENSE`](LICENSE).

------------------------------------------------------------------------

::: {align="center"}
**Stremfin**

Jellyfin / Emby API Bridge for Stremio Addons

Made with Python + FastAPI.

© 2026 Stremfin Contributors
:::
