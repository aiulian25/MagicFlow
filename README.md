<div align="center">

<img src="docs/magicflow-logo.jpg" alt="MagicFlow" width="160" />

# MagicFlow

**A local, self-hosted, Dockerized converter for image sequences, animations, and video.**

Combine stills into animations, pull frames back out, batch-convert formats, and re-encode —
all in your browser, all on your own machine. No cloud, no uploads, no telemetry.

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-aiulian25%2Fmagicflow-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/aiulian25/magicflow)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

## What it does

MagicFlow is a small FastAPI app that wraps **ImageMagick** and **ffmpeg** behind a clean,
fully offline web UI. It runs as a single container and keeps everything local.

Three workflows:

| Mode | Input | Output |
|------|-------|--------|
| **Combine** | A sequence of stills (or animated sources) | GIF · WebP · APNG · **MP4** · **WebM** · TIFF · **PDF** |
| **Extract** | GIF · WebP · APNG · PNG · TIFF · **PDF** · **MP4** · **WebM** | ZIP of PNG / JPEG / WebP frames |
| **Convert** | A single still (batch "Convert All" supported) | JPG · PNG · WebP · AVIF · HEIC · TIFF · BMP · TGA |

Accepted still inputs include JPG, PNG, APNG, WebP, GIF, BMP, TGA, TIFF, PSD, HEIC/HEIF,
and camera RAW (CR2/CR3/NEF/ARW/DNG/RAF/ORF/RW2/PEF/SRW).

## Features

- 🎞️ **Animation ⇄ frames, both ways** — build an animation from frames, or explode any
  animation/video/PDF back into individual stills (with range and "every Nth frame" selection).
- 🎬 **Video output & input** — encode to H.264 MP4 / VP9 WebM, and extract frames from video.
- 📄 **PDF pages** — split a PDF into frames or assemble stills into a slideshow PDF.
- ⏱️ **Timing-aware re-convert** — re-encode a saved output while preserving each frame's
  original delay, or override it uniformly.
- 📊 **Live progress** — real "frame N of M" feedback during encode.
- 🖼️ **Server-rendered thumbnails** so the grid never downloads multi-MB animations to paint a card.
- 🧾 **Conversion recipes** — every output remembers the exact settings that made it, shown on
  the card and used to pre-fill Re-convert.
- ⭐ **Presets** — four built-ins plus your own savable presets, persisted across restarts.
- 🗂️ **Bulk file ops** — multi-select to download a ZIP of outputs or delete in one go.
- 🔎 **Pre-flight inspector**, **estimated output size**, **boomerang/ping-pong**, geometry &
  color adjustments, and **"Edit in Combine"** to round-trip an output back into the editor.
- 🔌 **Fully offline UI** — Tailwind, fonts, and icons are all vendored; no CDN, no external calls.

## Quick start

You only need the [`docker-compose.yml`](docker-compose.yml) file. From a folder containing it:

```bash
docker compose up -d
```

This pulls `aiulian25/magicflow:latest` from Docker Hub and starts the app. Open:

**http://localhost:3876**

That's it — no build, no clone required. Data persists in four named Docker volumes
(`magicflow-input`, `magicflow-output`, `magicflow-tmp`, `magicflow-config`).

To update to the latest image and stop:

```bash
docker compose pull && docker compose up -d   # update
docker compose down                           # stop
```

### One-liner (without cloning)

```bash
curl -O https://raw.githubusercontent.com/aiulian25/MagicFlow/main/docker-compose.yml
docker compose up -d
```

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| Port | `3876` | Change the left side of the `ports` mapping in `docker-compose.yml`. |
| Max file size | 50 MB | Per uploaded file. |
| Max files per job | 60 | |
| Max extracted frames | 2000 | Decompression-bomb guard for Extract. |

Runtime data lives under `/data/{input,output,tmp,config}` inside the container. The default
compose uses named volumes; to map host folders instead, replace the volume entries with bind
mounts (see `docker-compose.dev.yml` in local development for an example pattern).

## Security & privacy

- **Everything is local.** Files never leave your machine; there is no telemetry or outbound traffic.
- Runs as a **non-root** user inside the container, with `no-new-privileges`.
- All conversions use **argument-list subprocess calls** (never a shell), per-job UUID working
  directories with guaranteed cleanup, filename sanitization, format allow-lists, and a hardened
  ImageMagick policy (PDF granted read-only, needed for page extraction).
- Intended for **local / trusted-network use**. If you expose it beyond localhost, put it behind
  your own authenticating reverse proxy.

## Development

Requires Docker. The repo builds the image from source:

```bash
# build + run with live ./app reload (edit files, refresh the browser)
docker compose -f docker-compose.dev.yml up -d --build
```

> `docker-compose.dev.yml` is git-ignored — copy the committed `docker-compose.yml` and add
> `build: .`, a `./app:/app` mount, and your own host data paths to create your local dev file.

Tech stack: **FastAPI + Uvicorn** · vanilla JS single-page UI · **ImageMagick-6** · **ffmpeg**
(+ ghostscript for PDF) · **Tailwind CSS** compiled at build time.

## Licenses & attribution

MagicFlow is released under the [MIT License](LICENSE).

Bundled web fonts are vendored under their own licenses:

- **Geist** and **JetBrains Mono** — SIL Open Font License 1.1
- **Material Symbols** — Apache License 2.0
