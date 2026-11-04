<div align="center">

<img src="docs/magicflow-logo.jpg" alt="MagicFlow" width="160" />

# MagicFlow

**A local, self-hosted, Dockerized converter for image sequences, animations, and video.**

Combine stills into animations, pull frames back out, batch-convert formats, and re-encode —
through your browser on your chosen MagicFlow server. Files are uploaded to that server;
no cloud account or telemetry service is required.

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
| **Convert** | A single still (batch "Convert All" supported) | JPG · PNG · WebP · AVIF · HEIC · TIFF · BMP · TGA · ICO |

Accepted still inputs include JPG, PNG, APNG, WebP, GIF, BMP, TGA, TIFF, PSD, HEIC/HEIF and AVIF still images.

AVIF input uses the packaged, offline `avifdec` decoder to preserve alpha and rotation.
Sequences, collections, declared HDR, unsupported NCLX color spaces and mirror/crop variants are rejected with an explanation.
Convert offers **ICO** under **More image formats**: one 8-bit RGBA icon at
16, 24, 32, 48, 64, 128 or 256 pixels square. The image fits inside the selected size
(with enlargement if needed), with transparent padding. ICO has no quality setting.
Each input/output is offered only after startup probes pass; unavailable saved format
choices stay selected with their reason, so restoring a capability restores the choice.
This expansion adds image formats only; it adds no video formats or codecs.

> **Camera RAW needs a working decoder delegate.** ImageMagick lists the RAW formats as
> readable, but decoding them needs a delegate program (`ufraw-batch`) that the image does not
> install in the source build below, so RAW is *not* offered there. MagicFlow checks this at startup rather than claiming it: see
> [Verified capabilities](#verified-capabilities).

### PDF page layout

Combine and Re-convert show PDF-only page controls: **Original image size**
(the compatible default), **A4**, **Letter**, or **Custom** sides of 50–600 mm.
Portrait/landscape arranges those sides; one image becomes one page in frame order.
Choose a uniform 0–100 mm margin that leaves at least 1 mm inside. **Fit whole image**
is the default; **Fill and crop edges** deliberately trims the image to fill the content area.
The first-image preview shows page geometry and placement; other pixel edits are applied on export.

Fixed pages use white paper, flatten transparency onto white, and render at 144 dpi
with lossless ZIP compression. Their physical dimensions and margins round by less
than 0.1 mm. The general quality slider does not affect this lossless PDF path.
Existing pixel transforms run before page placement. Original-size PDF and TIFF
pages retain their existing behavior. Layout settings are saved with Combine presets
and recorded in recipes. Re-convert starts with original-size pages, because an
existing PDF already contains its previous page layout; choose a new layout explicitly.
This exports raster pages, without searchable text or editable vectors.

## Features

- 🎞️ **Animation ⇄ frames, both ways** — build an animation from frames, or explode any
  animation/video/PDF back into individual stills (with range and "every Nth frame" selection).
  Extracted frames are named `frame_00000.png` upward — **zero-based and five digits**, the same
  numbering whichever decoder read the source, so a name always matches the index you selected.
- 🎬 **Video output & input** — encode to H.264 MP4 / VP9 WebM, and extract frames from video.
- 🖼️ **Consistent animation canvas** — after rotation and resizing, GIF/WebP/APNG/video frames
  are centered on a canvas spanning the widest and tallest frame. Fit stays shrink-only;
  smaller images get padding without enlargement. Choose Exact pad/crop/stretch for a fixed
  size. Padding is transparent unless a background is selected; MP4 defaults to opaque black
  and pads odd dimensions on the right/bottom. TIFF/PDF pages keep their individual sizes.
- 📄 **PDF pages** — split a PDF into frames or assemble stills into a slideshow PDF.
- ⏱️ **Timing-aware re-convert** — re-encode a saved output while preserving each frame's
  original delay, or override it uniformly.
- 📊 **Live progress** — real "frame N of M" feedback during encode.
- 🖼️ **Server-rendered thumbnails** so the grid never downloads multi-MB animations to paint a card.
- 🧾 **Conversion recipes** — kept outputs record requested settings separately from measured
  output details, with encoder and timing notes. Re-convert shows this history read-only and
  starts with neutral effects, keeping the saved output's size unless you request a resize.
  Original uploads are not retained; format/quality changes do not replay earlier effects.
- ⭐ **Presets** — four built-ins plus your own savable presets, persisted across restarts.
  Settings saves detect changes made in another tab and offer a reload instead of overwriting
  newer presets. Local edits remain unsaved if a save fails. Settings use a versioned JSON
  document with a previous valid copy at `/data/config/settings.backup.json`; corrupt files
  are preserved and block saves until restored. API clients must GET `/settings` and send
  the returned `revision` in the `If-Match` header when PUTting settings.
- 🔍 **Compare previews** — compare a completed result with its available source, or select
  exactly two saved outputs. Shared pan/zoom and checkerboard/white/black backgrounds help
  inspect geometry and alpha. Previews are first-frame/page, 8-bit sRGB and at most 640×640;
  they are not pixel-exact and animation/video timing is not synchronized.
- 🗂️ **Bulk file ops** — multi-select to download a ZIP of outputs or delete in one go.
- 🔎 **Pre-flight inspector**, **estimated output size**, **boomerang/ping-pong**, geometry &
  color adjustments, and **"Edit in Combine"** to round-trip an output back into the editor.
- 🔌 **Fully offline UI** — Tailwind, fonts, and icons are all vendored; no CDN, no external calls.

### Verified capabilities

MagicFlow does not advertise a format because it is in a list. At startup it writes and reads
back a few pixels in every format it offers, checks the ImageMagick policy and the delegate
programs, and keeps only what worked. `GET /config` reports the verified formats plus a reason
for each one withheld, and the UI disables those options with that reason on hover — so a
missing codec, a tightened policy or an absent delegate shows up before you pick a file, not
after a conversion fails. The startup log names anything withheld.

## Quick start

You only need the [`docker-compose.yml`](docker-compose.yml) file. From a folder containing it:

```bash
docker compose up -d
```

This pulls `aiulian25/magicflow:latest` from Docker Hub and starts the app. Open:

**http://localhost:3876**

That's it — no build, no clone required. Data persists in four named Docker volumes
(`magicflow-input`, `magicflow-output`, `magicflow-tmp`, `magicflow-config`).

> **Linux `amd64` only.** The published image is built and verified for `amd64`; there is no
> `arm64` image, so Apple Silicon and Raspberry Pi are not supported targets.

The default port mapping is `127.0.0.1:3876:3876`, so MagicFlow listens **only on this
machine**. See [Hosting on a LAN](#hosting-on-a-lan) to reach it from other devices.

For a repeatable installation, set `MAGICFLOW_IMAGE` in `.env` to a verified release
reference, for example `aiulian25/magicflow@sha256:<verified-release-digest>` (replace the
placeholder). The default `latest` is a mutable convenience tag; it does not identify this
checkout or prove that its fixes have been published. Inspect a candidate before use:

```bash
docker buildx imagetools inspect aiulian25/magicflow:latest
docker compose config
```

Only Linux **amd64** is built, locked and verified — the published image is a single-arch
`amd64` manifest by design. Do not infer ARM support from the base-image manifest: the base
is multi-arch, the release is not.

### Backup, upgrade and rollback

Keep the previous image locally and back up **all four data volumes** before upgrading.
Stop the service for a consistent snapshot (especially settings and recipe sidecars):

```bash
mkdir -p backups
container=$(docker compose ps -aq magicflow)
docker inspect --format '{{.Image}}' "$container" > backups/previous-image.txt
docker compose stop
docker run --rm --network none --volumes-from "$container":ro \
  --mount type=bind,src="$PWD/backups",dst=/backup \
  --entrypoint tar "$(cat backups/previous-image.txt)" \
  -czf /backup/magicflow-data.tgz -C /data input output tmp config
```

Choose and record the new immutable `MAGICFLOW_IMAGE` in `.env`, then run
`docker compose pull && docker compose up -d`. Check `/health`, `/config` and Settings' build
identity, inspect the startup log and try a small conversion before accepting the upgrade.
A backup is only useful after a restore test into disposable replacement volumes.

To roll back, stop the candidate and recreate with the retained image:

```bash
MAGICFLOW_IMAGE="$(cat backups/previous-image.txt)" docker compose up -d --pull never
```

If the candidate changed the data schema, restore the matching backup **into empty replacement
volumes while stopped**, then start the old image. Use the same volume mounts with
`--entrypoint tar ... -xzf /backup/magicflow-data.tgz -C /data` (backup mount read-only).
Do not unpack over a live library. Confirm outputs, `.recipes`, settings and presets after
restore. Cross-version schema downgrade compatibility must be tested per release; retaining
an image alone does not undo data changes. `docker compose down` stops/removes containers
without deleting named volumes. **Never use `docker compose down -v` for an upgrade.**
Do not prune the retained image until the upgrade and backup are accepted.

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| Port / binding | `127.0.0.1:3876` | Loopback only. Change the `ports` mapping in `docker-compose.yml`; see [Hosting on a LAN](#hosting-on-a-lan). |
| `MAGICFLOW_ALLOWED_ORIGINS` | unset | Only needed when a reverse proxy rewrites `Host`. Comma-separated browser-visible origins, e.g. `https://magicflow.example.com`. |
| Max file size | 50 MB | Per uploaded file. |
| Max files per job | 60 | |
| Max upload per job | 1 GiB | All files of one job together (`MAGICFLOW_MAX_TOTAL_UPLOAD_BYTES`). A larger body is refused before it is read. |
| Max extracted frames | 2000 | Decompression-bomb guard for Extract. |
| Max image dimension | 16000 px | Per side (`MAGICFLOW_MAX_IMAGE_DIMENSION`), matching the ImageMagick policy. |
| Max decoded pixels | 2000 MP | Per job (`MAGICFLOW_MAX_DECODED_PIXELS`). Input bytes don't bound decoded size — a 1 MB PNG can be 300 MP. |
| Free disk reserve | 1 GiB | A job is refused with `507` below this (`MAGICFLOW_MIN_FREE_DISK_BYTES`) rather than failing part-way. |
| Concurrent conversions | 1 | Media work is CPU-bound, so jobs run one at a time; up to 4 more wait (the UI shows "Waiting for the converter") and further requests get `503` rather than piling up. |
| Job time budget | 10 min | Total per conversion, across all its steps. A job that overruns is stopped — together with any encoder it started — and reports `504`. |

Runtime data lives under `/data/{input,output,tmp,config}` inside the container. The default
compose uses named volumes; to map host folders instead, replace the volume entries with bind
mounts (see the committed `docker-compose.dev.example.yml` for a portable example).
On a clean start, root initializes only the four mount roots, then execs the service as
uid 1000 (`appuser`). Existing files are not recursively re-owned. For a one-time migration
of old nested ownership, set `MAGICFLOW_FIX_OWNERSHIP=1`, start once, then remove it. This
repair visits only those trees, does not follow symlinks, and changes only mismatched owners.
It does not repair restrictive modes: grant uid 1000 read/write/search access as appropriate.
An unwritable mount fails startup clearly. An already-correct nonroot deployment can use
`--user 1000`; it must supply writable data paths itself.

## Security & privacy

- **Uploads go to your server.** On a LAN, media leaves the browser computer for the machine
  running MagicFlow. The normal app has no telemetry service or required external API. UI
  assets are self-hosted; native decoder restrictions are not a container-wide egress guarantee.
- Runs as a **non-root** user inside the container, with `no-new-privileges`.
- All conversions use **argument-list subprocess calls** (never a shell), per-job UUID working
  directories with normal-exit/error cleanup and abandoned-job reclamation on restart, filename
  sanitization, format allow-lists, and a hardened
  ImageMagick policy: PDF is granted **read and write** (both are needed for the PDF modes above),
  while the PostScript coders that hand data to Ghostscript — PS/PS2/PS3/EPS/XPS — and the remote
  coders URL/HTTP/HTTPS stay denied. Ghostscript itself is only ever invoked to rasterize pages
  when *reading* a PDF; writing one uses ImageMagick's own encoder. The policy is asserted at
  image build time, so a rebuild cannot silently lose it.
- **Downloads never delete.** `GET /download/...` is read-only, so an interrupted transfer can
  simply be retried and no link can destroy a file. Results created with *Keep on server* off
  are discarded by an explicit follow-up request once the download succeeds, and any that are
  missed become eligible for expiry after an hour, swept on startup or library refresh.
- **Writes must come from MagicFlow's own page.** Requests that change state (convert, delete,
  settings) are rejected when a browser reports a different origin, so another site open in
  your browser cannot drive the app. Non-browser clients such as `curl` are unaffected.
- **Budgets are checked before the work, not after.** Oversize bodies are refused before they
  are read, a job's decoded pixel count is probed before full frame processing, a
  nearly-full disk is reported up front, and archives are built on disk rather than in memory.
  The limits above are reported by `GET /config`, and the compose file caps the container's
  memory and process count as a backstop for ffmpeg and ghostscript, which have no policy of
  their own.
- Intended for **local / trusted-network use**. The default binding is loopback-only.

### Hosting on a LAN

To reach MagicFlow from other devices, edit the `ports` mapping in `docker-compose.yml`:

```yaml
    ports:
      - "3876:3876"        # all interfaces instead of 127.0.0.1 only
```

The app has **no accounts or passwords**: anyone who can reach the port can read, convert and
delete everything in your library. Put an authenticating reverse proxy in front of it, and note
that your media is uploaded to whichever machine runs the container — on a LAN that is another
computer, not the browser's.

## Development

Requires Docker and a checkout. The committed example uses project-local disposable data
under `.magicflow-dev/` and a read-only source bind mount, without host-specific paths:

```bash
docker compose -f docker-compose.dev.example.yml up -d --build
```

Uvicorn reload watches Python changes; refresh the browser for HTML/JS. Tailwind changes need
a rebuild (the example's source mount serves committed CSS). Source overrides are identified
as development/working-tree by `/config`; they are not the image's immutable release artifact.
The old ignored `docker-compose.dev.yml` can remain a personal configuration.

### Release build and provenance

Builds require network access for pinned base layers, the dated Debian snapshot, npm and
PyPI. Runtime UI/conversion work does not require that build network. The lock set targets
CPython 3.12/Linux amd64: base image digests and the Debian snapshot are recorded in
`build/inputs.json` and the Dockerfile. Debian packages are checked against SHA-256 values
from the signed snapshot index, with the official Debian pool and snapshot as byte-identical
artifact sources. npm uses `build/package-lock.json` with `npm ci`, and
all Python runtime dependencies use exact wheel hashes in `requirements.txt`.

```bash
MAGICFLOW_VERSION=local-test MAGICFLOW_BUILD_IMAGE=magicflow:local scripts/build-release.sh
python3 scripts/verify-deployment.py magicflow:local /tmp/magicflow-release-check
```

If the builder bridge has broken DNS, append `--network host` to the build command
where Docker supports it; this affects build-time networking only.

The build script records the Git revision with `-dirty` when the checkout contains changes.
Each image also records an aggregate SHA-256 of the installed source/assets/locks, so an
uncommitted checkout is distinguishable from its base commit. OCI labels carry version and
revision; `/config` and Settings expose the manifest identity. The image includes
`/app/release-manifest.json` (file hashes, complete dpkg inventory, build inputs and format
capabilities) and `/app/python-sbom.json` (`pip inspect` dependency inventory). Export them
alongside the immutable image ID/digest and test logs for each release. These inventories
are not a signed attestation, and reproducible inputs do not promise byte-identical OCI
metadata or squash native codec differences across architectures.

The verifier uses only final-image code, fresh data, no exposed port and `--network none`;
all containers use `--rm`. It checks nonroot startup, correct ownership preservation,
explicit ownership repair, permission failure, format/PDF smoke, installed manifest hashes,
shutdown/restart and a stopped backup/restore with output recipes and presets. Restoring the
same immutable artifact verifies recovery mechanics; it does not certify a different
version's schema compatibility. Pass `--rollback-image RETAINED_IMAGE` to additionally test
the actual previous candidate against the restored fixtures. Logs and the isolated backup remain outside containers.

Update locks deliberately when reviewing fixes, rather than leaving them frozen indefinitely:
edit direct versions in `build/requirements.in` and `build/package.json`; download wheels in
a fresh directory using the pinned amd64 Python image; run
`python3 scripts/lock-python.py WHEEL_DIRECTORY > requirements.txt`. Regenerate the npm lock
using the pinned Node image and `npm install --package-lock-only --ignore-scripts` in `build/`.
Update both Dockerfile digests/snapshot and `build/inputs.json` together. Then build afresh,
run the verifier and client checks (`node --test tests/*.test.mjs`), review the inventories,
and record the candidate digest. Test an actual restored backup and previous-version rollback
before publishing a release that changes persistence. No image is published by these scripts.

Tech stack: **FastAPI + Uvicorn** · vanilla JS · **ImageMagick-6** · **ffmpeg/Ghostscript** ·
**Tailwind CSS** compiled at build time. Font versions, fingerprints and full license notices
are in [`app/static/vendor/LICENSES.txt`](app/static/vendor/LICENSES.txt).

## Regression checks

The suite uses Python's standard `unittest`, Node's built-in test runner, generated tiny
media, and headless Chrome. No pytest, front-end framework or external integrations are
required. Use Python 3.12+ and Node 22; browser checks expect `google-chrome` (or set
`CHROME_BIN`). Docker is required only for application/media/recovery checks.

```bash
python3 scripts/test.py syntax       # parse Python/JS/shell and check the runner itself
python3 scripts/test.py ui           # UI/state/settings plus browser behavior

# Build with the locked FIX-025 inputs, or select an already-built local final image.
MAGICFLOW_VERSION=test MAGICFLOW_BUILD_IMAGE=magicflow:test scripts/build-release.sh
python3 scripts/test.py api --image magicflow:test        # small API/lifecycle contracts
python3 scripts/test.py media --image magicflow:test      # regular full media/API coverage
python3 scripts/test.py fidelity --image magicflow:test   # explicit color/precision corpus
python3 scripts/test.py isolation --image magicflow:test  # intentional failure; runner must pass cleanup proof
python3 scripts/verify-deployment.py --lifecycle-only magicflow:test test-results/recovery
```

`tests/suites.json` is the authoritative grouping. The media group covers every application
regression module except color fidelity, which has its own explicit group. `api` is a fast
subset, not a substitute for media checks. The host runner's contract test detects new Python
test files omitted from the grouping. Existing pixel/frame/timeline, publication/retention,
recipe, job lifecycle, filename and UI-state regressions remain the primary behavioral tests;
command-shape assertions are limited to the test runner's isolation boundary. Historical
before/after evidence remains with each corresponding audit fix; the isolation probe's
intentional failure is a test of the harness, not an application regression.

Application tests contain `/data` paths and must **never** be discovered directly on a host
or against a live server. The runner resolves the chosen image to an immutable local ID,
mounts only the tests read-only, creates `/data` and `/tmp` as disposable tmpfs, exposes no
port and disables runtime networking. The image's code, tools, policy and compiled assets
stay together. API requests use loopback inside that container; no production volume or
source overlay is accepted. Syntax checks parse Python without importing application tests.
The release verifier separately creates only labelled temporary named volumes/bind roots.

Logs, command plans, image IDs and per-suite results default to ignored `test-results/`;
choose another destination with `--logs DIRECTORY`. Failures return nonzero and retain their
logs. Every container has `--rm`, bounded resources and an invocation label. Normal exit,
failure, timeout and SIGTERM run scoped cleanup. The `isolation` command deliberately prints
an assertion failure, then succeeds only when its expected exit/log and removal are verified.
For recovery after an interrupted CI process, use the recorded run ID:

```bash
python3 scripts/test.py cleanup --run-id RECORDED_RUN_ID
```

Cleanup checks ownership labels and never prunes Docker globally. Do not share a run ID
between concurrent independent jobs; `MF_TEST_RUN_ID` supplies the CI job's stable scope to
both runners. A hard-killed host cannot execute its own cleanup; CI's always-run cleanup is
the fallback while the runner remains available. Logs should be retained before manual cleanup.

[CI](.github/workflows/ci.yml) runs syntax/runner/UI checks and a locked final-image build,
media regressions, the failing-job isolation proof, browser checks using exported built
HTML/CSS, and offline startup/shutdown/restore checks on Linux amd64. Read-only permissions,
immutable action revisions and always-uploaded logs are configured; no image is published.
The generated color/precision corpus is an explicit `workflow_dispatch` fidelity job. Real
camera RAW, GPU and additional architecture qualification require their own licensed fixtures
and appropriate hardware; their availability is not implied by this suite. No such job is
silently skipped and counted as support. Chrome/host tool versions are logged; the hosted
runner/browser environment is not a bit-identical image guarantee.

## Comparing results

Use **Compare first input** after Combine (only the first input/frame is compared), **Compare**
on a completed Convert card, or **Compare source and output** on a temporary Re-convert result.
Kept Re-convert pairs can be reopened through the output card's Compare action during this
page session. Comparing a temporary result does not download, release, or discard it.

Original files are used only while the browser still holds them; comparison uploads the
selected original again on demand and the server removes its private copy after rendering.
No new original-source retention is added. If the original was cleared, deleted, or belongs
to an earlier session, close Compare and select two outputs in **My Files → Compare selected**.
Missing/corrupt/unsupported sources leave an explanation rather than an empty image.

Drag either pane or focus it and use arrow keys to pan both; use Shared zoom or Reset view.
Changing Viewing background never changes file pixels. The dialog shows source/frame dimensions,
file bytes and actual preview dimensions; PDF pixels are measured at 72 dpi. Orientation is
applied for display, image alpha is preserved, and previews follow the existing SDR/sRGB policy;
video previews use the existing opaque video-decoding path.
HDR remains unsupported. Equal geometry shares the same fit/pan/zoom. Different dimensions
are disclosed and cannot be treated as pixel-aligned. No synchronized playback or wipe is
included in this first-frame comparison.

The preview routes use the normal job slot, disk/pixel/dimension budgets, decoder policy
and timeouts. Original uploads retain the per-file limit; saved files over the per-job byte
limit are refused for comparison. Bounded PNG responses are not cached, and comparison scratch
is cleaned before responding. The original and saved outputs remain unchanged.

## Color and precision policy

New exports record processing policy `sdr-srgb-v1` in their recipes. Photo orientation
is baked before user rotation/resize, and embedded ICC profiles are converted to sRGB
before optional metadata stripping (including the animated-WebP workaround). Untagged
RGB is treated as sRGB; untagged video is assumed BT.709; untagged CMYK uses ImageMagick's default conversion and needs a
source ICC profile for calibrated color. The runtime uses Ghostscript's sRGB profile.
PNG/TIFF image exports can retain 16-bit samples (TIFF uses lossless LZW); APNG/video
preparation is 8-bit, and GIF/WebP exports are 8-bit.
HDR tone mapping is unsupported: PQ/HLG video and HDR cICP PNG are rejected. ICC-tagged APNG frames are normalized after FFmpeg decoding, including direct PNG
extraction. Other untagged sources are assumed SDR; no camera-RAW/HDR fidelity
guarantee is implied. Existing exports remain unchanged.

PNG/APNG retain alpha, GIF reduces it to on/off, and WebP supports alpha. WebM can encode alpha for compatible players, but the app currently decodes video
into opaque frames; use APNG when transparency must survive an editing round trip. JPEG/BMP
flatten onto white; MP4 uses black or the selected background. Video encodes use limited
range BT.709 YUV coefficients/primaries with sRGB transfer metadata, not HDR.
SDR video decoding converts the declared transfer/primaries into the sRGB working
space; temporary filter padding is cropped away to retain odd dimensions. Colors
can change through lossy compression and chroma subsampling. The completion checkerboard
indicates transparency; it is not baked into the exported image.

## Library and decoder boundaries

The output library accepts regular files only. Downloads and archives hold an open
file descriptor, and re-conversion and uncached thumbnails copy the selected file
into private job storage before decoding. HTTP ranges remain supported. Source
snapshots need additional temporary disk space; a short read fails the job rather
than switching to a replacement file. This implementation requires Linux/POSIX
no-follow directory opens and `/proc` (as provided by the container).

Symlinked media is omitted from the library and rejected by file operations; links
are preserved. To migrate an existing symlink-based library, stop MagicFlow, copy
the intended media into real regular files inside the output directory, and replace
linked directories with real directories. The output root, `.staging`, `.recipes`
and temporary-output registry must also be real service-owned objects. Jobs refuse
an invalid internal layout, and startup refuses it before cleanup. Keep the app's
input and temporary directories private to the service.

Rebuild the image to install `app/imagemagick-policy.xml`. Its coder allow-list
covers supported media and the pixel/profile primitives needed for processing;
URL, SVG, MVG, MSL and indirect `@file` inputs are denied. Delegates are limited to
Ghostscript's PDF paths and the RAW decoder; PDF reading/writing remains supported.
FFmpeg/FFprobe allow only local file/pipe protocols and supported media demuxers;
concat is enabled only for explicit application-built timelines. File extensions
still serve as input validation, while detected content is limited by these decoder
policies. A supported image renamed to another supported extension can still decode.

These boundaries reduce parser exposure; they are not an operating-system sandbox
for native codecs or protection against a privileged host writer, hard links, or
in-place changes to an already open inode. No container-wide network isolation is
added to normal deployment. Controlled local/network reference fixtures are tested
without using production data or secrets.

## Licenses & attribution

MagicFlow is released under the [MIT License](LICENSE).

Bundled web fonts are vendored under their own licenses:

- **Geist** and **JetBrains Mono** — SIL Open Font License 1.1
- **Material Symbols** — Apache License 2.0
