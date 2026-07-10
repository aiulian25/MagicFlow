"""MagicFlow backend.

Unified conversion model — every job is one "direction":

    Direction      Input                Output                 Endpoint
    ---------      -----                ------                 --------
    Combine        many stills          1 animation/doc        POST /convert
    Extract        1 animation/doc      many stills (ZIP)      POST /explode
    Convert still  1 still              1 still                POST /convert-image
    Re-convert     1 stored output      1 new output           POST /reconvert/{name}

All directions share the same building blocks: per-job UUID work dirs under
/data, list-arg subprocess calls (never shell=True), validation + size/frame
caps, ImageMagick for transforms, ffmpeg for APNG (en/decode), and
try/finally temp cleanup. "Convert still" is kept separate from "Combine"
(rather than /convert with one frame) because still outputs — JPEG/AVIF/HEIC/
BMP/TGA — are a different format set than the animated/multi-page ones.
"""

import io
import json
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from utils import (
    build_command,
    build_assembly_command,
    build_explode_command,
    build_apng_encode_command,
    build_apng_decode_command,
    build_video_encode_command,
    sanitize_filename,
    validate_file,
    validate_explode_input,
    ALLOWED_EXTENSIONS,
    ALLOWED_OUTPUT_FORMATS,
    EXPLODE_INPUT_EXTENSIONS,
    RAW_EXTENSIONS,
    EXPLODE_OUTPUT_FORMATS,
    EXPLODE_OUTPUT_EXT,
    STILL_OUTPUT_FORMATS,
    STILL_OUTPUT_EXT,
    OUTPUT_EXTENSIONS,
    OUTPUT_MEDIA_TYPES,
)

INPUT_DIR = Path("/data/input")
OUTPUT_DIR = Path("/data/output")
TMP_DIR = Path("/data/tmp")
CONFIG_DIR = Path("/data/config")
SETTINGS_FILE = CONFIG_DIR / "settings.json"
# Cache for server-rendered card thumbnails (keyed by filename + mtime)
THUMB_DIR = TMP_DIR / "thumbs"
# Sidecar "recipes": the settings that produced each kept output, stored beside
# it (dotfile dir, so /files skips it) — powers the card meta line + Re-convert
# prefill. Lives in the mounted output volume, so it survives rebuilds.
RECIPE_DIR = OUTPUT_DIR / ".recipes"

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB per file
MAX_FILES = 60                     # hard backend cap
MAX_EXPLODE_FRAMES = 2000          # decompression-bomb guard for /explode

# Inputs that can never expand to multiple frames — skipped by the /convert
# expanded-frame guard so large jobs don't pay one identify call per still.
SINGLE_FRAME_EXTENSIONS = frozenset({".jpg", ".jpeg", ".bmp", ".tga"}) | RAW_EXTENSIONS

# Outputs made with "Keep on server" off get this prefix: they are hidden from
# /files listings, deleted on download (?delete=true), and swept after an hour
# if the user never downloads them.
TMP_OUTPUT_PREFIX = "magicflow_tmp_"

# Live conversion progress, keyed by job_id: {"phase", "done", "total"}.
# In-memory is safe because the app runs a SINGLE uvicorn worker
# (Dockerfile: --workers 1). A multi-worker deployment would need a shared
# store (Redis / a file per job) since a poll could hit a different worker.
JOBS: dict[str, dict] = {}

# User-preference persistence limits
SETTINGS_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_SETTINGS_KEYS = 100
# 8 KB per value: most settings are short scalars, but `user_presets` (F20) packs
# a JSON-encoded map of saved Combine snapshots into one string value, which a
# 256-char cap can't hold. MAX_SETTINGS_BYTES (below) remains the real ceiling on
# the whole payload, so this only widens the per-value allowance.
MAX_SETTINGS_VALUE_LEN = 8192
MAX_SETTINGS_BYTES = 64 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    for directory in (INPUT_DIR, OUTPUT_DIR, TMP_DIR, CONFIG_DIR, THUMB_DIR, RECIPE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    _sweep_tmp_outputs()
    yield


app = FastAPI(title="MagicFlow", docs_url=None, redoc_url=None, lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ─────────────────────────────────────────────
# Static entry point
# ─────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ─────────────────────────────────────────────
# Conversion progress (polled by the UI during /convert). Read-only lookup
# of the in-memory JOBS map; unknown/finished jobs report "unknown".
# ─────────────────────────────────────────────

@app.get("/progress/{job_id}")
async def progress(job_id: str):
    return JOBS.get(job_id) or {"phase": "unknown"}


# ─────────────────────────────────────────────
# Server configuration (read-only; the frontend derives its
# allow-lists, caps and labels from this so formats/limits
# live in exactly one place: utils.py + the constants above)
# ─────────────────────────────────────────────

@app.get("/config")
async def get_config():
    return {
        "allowed_input_exts": sorted(ALLOWED_EXTENSIONS),
        "explode_input_exts": sorted(EXPLODE_INPUT_EXTENSIONS),
        "still_output_formats": sorted(STILL_OUTPUT_FORMATS),
        "explode_output_formats": sorted(EXPLODE_OUTPUT_FORMATS),
        "animation_output_formats": sorted(ALLOWED_OUTPUT_FORMATS),
        "max_file_size": MAX_FILE_SIZE,
        "max_files": MAX_FILES,
        "max_explode_frames": MAX_EXPLODE_FRAMES,
        "output_dir": str(OUTPUT_DIR),
    }


# ─────────────────────────────────────────────
# User preferences (persisted to mounted config volume,
# survives container restart / rebuild / page refresh)
# ─────────────────────────────────────────────

@app.get("/settings")
async def get_settings():
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text())
            if isinstance(data, dict):
                return {"settings": data}
    except Exception:
        pass  # corrupt/unreadable file → fall back to defaults
    return {"settings": {}}


@app.put("/settings")
async def put_settings(settings: dict = Body(...)):
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="Settings must be a JSON object.")
    if len(settings) > MAX_SETTINGS_KEYS:
        raise HTTPException(status_code=400, detail="Too many settings keys.")

    clean: dict = {}
    for key, value in settings.items():
        if not isinstance(key, str) or not SETTINGS_KEY_RE.match(key):
            raise HTTPException(status_code=400, detail=f"Invalid settings key: {key!r}.")
        # bool must be checked before int (bool is a subclass of int)
        if isinstance(value, bool) or isinstance(value, (int, float)):
            clean[key] = value
        elif isinstance(value, str):
            if len(value) > MAX_SETTINGS_VALUE_LEN:
                raise HTTPException(status_code=400, detail=f"Value for {key!r} is too long.")
            clean[key] = value
        else:
            raise HTTPException(status_code=400, detail=f"Invalid value type for {key!r}.")

    payload = json.dumps(clean)
    if len(payload.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise HTTPException(status_code=400, detail="Settings payload too large.")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file then replace, so a crash never leaves a partial file
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload)
    tmp.replace(SETTINGS_FILE)
    return {"saved": True}


# ─────────────────────────────────────────────
# Saved files listing and deletion
# ─────────────────────────────────────────────

def _sweep_tmp_outputs(max_age_s: int = 3600) -> None:
    """Delete not-kept outputs (TMP_OUTPUT_PREFIX) that were never downloaded.
    They normally vanish on download (?delete=true); this reaps the ones whose
    tab closed first. Runs at startup and before each /files listing."""
    try:
        candidates = list(OUTPUT_DIR.glob(f"{TMP_OUTPUT_PREFIX}*"))
    except OSError:
        return
    now = time.time()
    for f in candidates:
        try:
            if f.is_file() and now - f.stat().st_mtime > max_age_s:
                f.unlink()
        except OSError:
            pass  # already gone or being downloaded — next sweep will retry


def _read_output_dims(path: Path) -> tuple[Optional[int], Optional[int]]:
    """First-frame pixel dimensions of a produced output, or (None, None) when
    they can't be read (e.g. some video containers). Best-effort — recipes are a
    convenience, never a hard dependency."""
    try:
        r = subprocess.run(
            ["identify", "-format", "%w %h", f"{path}[0]"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None, None


def _write_recipe(output_name: str, direction: str, params: dict,
                  sources: list[str], frames: int,
                  width: Optional[int], height: Optional[int]) -> None:
    """Persist the settings that produced `output_name` to a sidecar
    RECIPE_DIR/<output_name>.json (atomic temp-then-replace, mirroring
    put_settings). Records the parameter set, source filenames, frame count and
    the measured output dimensions so the UI can show "what made this" and
    Re-convert can open where the job left off. Best-effort: any failure here is
    swallowed so a completed conversion is never lost to a bad sidecar write.
    `output_name` is server-generated and already safe; sources are sanitized."""
    try:
        RECIPE_DIR.mkdir(parents=True, exist_ok=True)
        recipe = {
            "direction": direction,
            "sources": [sanitize_filename(s) for s in sources][:MAX_FILES],
            "frames": frames,
            **params,
            # Measured output dims win over any requested resize in params.
            "width": width,
            "height": height,
        }
        payload = json.dumps(recipe)
        target = RECIPE_DIR / (output_name + ".json")
        tmp = RECIPE_DIR / (output_name + ".json.tmp")
        tmp.write_text(payload)
        tmp.replace(target)
    except Exception:
        pass


@app.get("/files")
async def list_files():
    if not OUTPUT_DIR.exists():
        return {"files": []}
    _sweep_tmp_outputs()
    files = []
    for f in OUTPUT_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in OUTPUT_EXTENSIONS and not f.name.startswith(TMP_OUTPUT_PREFIX):
            stat = f.stat()
            # Attach the sidecar recipe when present; a missing/corrupt one is a
            # non-event (older files predate recipes) → recipe: null, defaults hold.
            recipe = None
            recipe_path = RECIPE_DIR / (f.name + ".json")
            if recipe_path.is_file():
                try:
                    recipe = json.loads(recipe_path.read_text())
                except Exception:
                    recipe = None
            files.append(
                {
                    "filename": f.name,
                    "size": stat.st_size,
                    "download_url": f"/download/{f.name}",
                    "modified": stat.st_mtime,
                    "recipe": recipe,
                }
            )
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"files": files}


@app.delete("/files/{filename}")
async def delete_file(filename: str):
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = OUTPUT_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    path.unlink()
    # Remove the sidecar recipe alongside its file (no-op if it never had one).
    (RECIPE_DIR / (safe + ".json")).unlink(missing_ok=True)
    return {"deleted": safe}


# ── Bulk file operations ──────────────────────────────────────────────────────
# Cap on a single bulk-archive download so a huge selection can't exhaust memory
# (the ZIP is built in-memory, like /explode's).
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_BULK_FILES = 500


def _validate_filename_batch(payload: dict) -> list[str]:
    """Extract and validate a batch of output filenames from a JSON body. Each
    name must survive the SAME sanitize-equality check as delete_file (rejecting
    traversal, separators, leading dots) so a crafted name like '../settings.json'
    can never escape OUTPUT_DIR. Returns the validated list."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    names = payload.get("filenames")
    if not isinstance(names, list) or not names:
        raise HTTPException(status_code=400, detail="'filenames' must be a non-empty list.")
    if len(names) > MAX_BULK_FILES:
        raise HTTPException(status_code=400, detail=f"Too many files (max {MAX_BULK_FILES}).")
    for n in names:
        if not isinstance(n, str) or sanitize_filename(n) != n:
            raise HTTPException(status_code=400, detail=f"Invalid filename: {n!r}.")
    return names


@app.post("/files/delete")
async def delete_files(payload: dict = Body(...)):
    """Delete a batch of outputs. Missing names are reported, never fatal, so one
    stale entry doesn't abort the whole batch. Recipe sidecars go with their file."""
    names = _validate_filename_batch(payload)
    deleted: list[str] = []
    missing: list[str] = []
    for name in names:
        path = OUTPUT_DIR / name
        if path.is_file():
            try:
                path.unlink()
                (RECIPE_DIR / (name + ".json")).unlink(missing_ok=True)
                deleted.append(name)
            except OSError:
                missing.append(name)  # vanished mid-batch — treat as missing
        else:
            missing.append(name)
    return {"deleted": deleted, "missing": missing}


@app.post("/files/archive")
async def archive_files(payload: dict = Body(...)):
    """Stream a ZIP of the selected outputs. Uses ZIP_STORED — the files are
    already-compressed media, so re-deflating only burns CPU for no size win.
    Same in-memory BytesIO pattern as /explode."""
    names = _validate_filename_batch(payload)
    paths: list[Path] = []
    total = 0
    for name in names:
        path = OUTPUT_DIR / name
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            paths.append(path)
    if not paths:
        raise HTTPException(status_code=404, detail="No matching files to archive.")
    if total > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="Selected files exceed the 2 GiB archive limit.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for p in paths:
            zf.write(p, arcname=p.name)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="magicflow_files.zip"'},
    )


# ─────────────────────────────────────────────
# Conversion
# ─────────────────────────────────────────────

def _validate_transform_params(
    delay: int, loop: int, width: Optional[int], height: Optional[int],
    resize_mode: str, quality: int, rotate: int,
    brightness: int, saturation: int, hue: int,
    sharpen: float, blur: float, colors: int, dither: str, background: str,
) -> None:
    """Shared validation for the transform parameters of /convert and
    /reconvert — one place, so the two endpoints can never drift apart."""
    if not (1 <= delay <= 10000):
        raise HTTPException(status_code=400, detail="Delay must be 1–10000 ms.")
    if not (0 <= loop <= 1000):
        raise HTTPException(status_code=400, detail="Loop must be 0–1000.")
    if width is not None and not (1 <= width <= 8000):
        raise HTTPException(status_code=400, detail="Width must be 1–8000 px.")
    if height is not None and not (1 <= height <= 8000):
        raise HTTPException(status_code=400, detail="Height must be 1–8000 px.")
    if resize_mode not in ("fit", "pad", "crop", "stretch"):
        raise HTTPException(status_code=400, detail="Resize mode must be fit, pad, crop, or stretch.")
    if not (1 <= quality <= 100):
        raise HTTPException(status_code=400, detail="Quality must be 1–100.")
    if rotate not in (0, 90, 180, 270):
        raise HTTPException(status_code=400, detail="Rotate must be 0, 90, 180, or 270.")
    if not (50 <= brightness <= 150):
        raise HTTPException(status_code=400, detail="Brightness must be 50–150.")
    if not (0 <= saturation <= 200):
        raise HTTPException(status_code=400, detail="Saturation must be 0–200.")
    if not (0 <= hue <= 200):
        raise HTTPException(status_code=400, detail="Hue must be 0–200.")
    if not (0.0 <= sharpen <= 5.0):
        raise HTTPException(status_code=400, detail="Sharpen must be 0–5.")
    if not (0.0 <= blur <= 5.0):
        raise HTTPException(status_code=400, detail="Blur must be 0–5.")
    if not (2 <= colors <= 256):
        raise HTTPException(status_code=400, detail="Colors must be 2–256.")
    if dither not in ("Riemersma", "Floyd-Steinberg", "None"):
        raise HTTPException(status_code=400, detail="Dither must be Riemersma, Floyd-Steinberg, or None.")
    # Sanitize background color — only allow CSS color names and hex values
    if background and not re.fullmatch(r"[a-zA-Z0-9#]{1,30}", background):
        raise HTTPException(status_code=400, detail="Invalid background color value.")


@app.post("/convert")
async def convert(
    files: List[UploadFile] = File(...),
    format: str = Form(...),
    delay: int = Form(100),
    loop: int = Form(0),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    resize_mode: str = Form("fit"),
    quality: int = Form(85),
    keep_output: bool = Form(True),
    custom_name: Optional[str] = Form(None),
    # Timing / sequence
    boomerang: bool = Form(False),
    delays: Optional[str] = Form(None),  # comma-separated per-frame ms (0/blank = use global)
    # Geometry
    rotate: int = Form(0),
    flip: bool = Form(False),
    flop: bool = Form(False),
    # Color adjustments
    brightness: int = Form(100),
    saturation: int = Form(100),
    hue: int = Form(100),
    # Color effects
    grayscale: bool = Form(False),
    negate: bool = Form(False),
    sepia: bool = Form(False),
    auto_level: bool = Form(False),
    normalize: bool = Form(False),
    # Sharpness / blur
    sharpen: float = Form(0.0),
    blur: float = Form(0.0),
    # GIF-specific
    colors: int = Form(256),
    dither: str = Form("Riemersma"),
    optimize_layers: bool = Form(True),
    # Output extras
    strip: bool = Form(False),
    background: str = Form(""),
    # Optional client-supplied job id so the UI can poll GET /progress/{job_id}
    # during the job. Must be a UUID4 (used as a work-dir name and JOBS key).
    job_id: Optional[str] = Form(None),
):
    # ── Input validation ──────────────────────
    fmt = format.lower().strip()
    if fmt not in ALLOWED_OUTPUT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format must be one of: {', '.join(sorted(ALLOWED_OUTPUT_FORMATS))}.")

    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Too many files. Maximum allowed is {MAX_FILES}.")
    _validate_transform_params(
        delay=delay, loop=loop, width=width, height=height,
        resize_mode=resize_mode, quality=quality, rotate=rotate,
        brightness=brightness, saturation=saturation, hue=hue,
        sharpen=sharpen, blur=blur, colors=colors, dither=dither, background=background,
    )

    # ── Job directories ───────────────────────
    # A client may supply job_id (to poll /progress); accept only a valid UUID
    # so it is safe as a directory name and dict key. Otherwise generate one.
    if job_id is not None:
        try:
            job_id = str(uuid.UUID(job_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid job_id.")
    else:
        job_id = str(uuid.uuid4())
    job_input = INPUT_DIR / job_id
    job_tmp = TMP_DIR / job_id

    # Map format to file extension
    ext_map = {"gif": ".gif", "webp": ".webp", "apng": ".apng", "tiff": ".tiff",
               "pdf": ".pdf", "mp4": ".mp4", "webm": ".webm"}
    out_ext = ext_map[fmt]

    try:
        job_input.mkdir(parents=True, exist_ok=True)
        job_tmp.mkdir(parents=True, exist_ok=True)

        frame_paths: list[str] = []

        for i, upload in enumerate(files):
            content = await upload.read()

            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"Frame {i + 1} ('{upload.filename}') exceeds the 50 MB per-file limit.",
                )

            ok, msg = validate_file(upload.filename or "", upload.content_type or "")
            if not ok:
                raise HTTPException(status_code=400, detail=f"Frame {i + 1}: {msg}")

            safe_name = sanitize_filename(upload.filename or f"frame_{i:03d}")
            dest = job_input / f"{i:03d}_{safe_name}"
            dest.write_bytes(content)
            frame_paths.append(str(dest))

        # ── Decompression-bomb guard (mirror of /explode's, app-wide invariant):
        # multi-frame inputs (GIF/WebP/TIFF/PSD…) expand into all their sub-frames
        # inside the convert command, so cap the TOTAL expanded frame count.
        expanded_total = 0
        multi_frame_inputs = 0
        for p in frame_paths:
            if Path(p).suffix.lower() in SINGLE_FRAME_EXTENSIONS:
                expanded_total += 1
                continue
            n = _count_frames(p) or 1  # unknown → 1; ImageMagick limits still apply
            expanded_total += n
            if n > 1:
                multi_frame_inputs += 1
        effective_total = expanded_total + (max(0, expanded_total - 2) if boomerang else 0)
        if effective_total > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Combined inputs expand to {effective_total} frames; the limit is {MAX_EXPLODE_FRAMES}.",
            )
        # Per-frame delays address input FILES, not expanded sub-frames — with an
        # animated input the mapping would be silently wrong, so reject explicitly.
        if delays and multi_frame_inputs:
            raise HTTPException(
                status_code=400,
                detail="Per-frame delays cannot be combined with animated inputs.",
            )

        if keep_output:
            output_name = f"magicflow_{job_id}{out_ext}"
            if custom_name:
                safe_custom = sanitize_filename(custom_name)
                safe_stem = safe_custom.rsplit(".", 1)[0] if "." in safe_custom else safe_custom
                if safe_stem:
                    output_name = f"{safe_stem}{out_ext}"
                if (OUTPUT_DIR / output_name).exists():
                    output_name = f"{safe_stem}_{job_id[:8]}{out_ext}"
        else:
            # Not kept: prefixed name — hidden from /files, deleted on download,
            # swept by _sweep_tmp_outputs if never downloaded. Custom naming is
            # meaningless for a file that must vanish.
            output_name = f"{TMP_OUTPUT_PREFIX}{job_id}{out_ext}"
        output_path = OUTPUT_DIR / output_name

        # ImageMagick delay unit is centiseconds (1/100 s)
        delay_cs = max(1, round(delay / 10))

        # Per-frame delays (centiseconds). Parse the comma-separated ms list; a
        # blank/0 entry falls back to the global delay for that frame.
        # NOTE: entries map 1:1 to input FILES (jobs mixing delays with animated
        # inputs were rejected above, so file == frame holds here).
        frame_delays = None
        if delays:
            parts = [p.strip() for p in delays.split(",")]
            if len(parts) == len(frame_paths):
                try:
                    frame_delays = [
                        (max(1, round(int(p) / 10)) if p and int(p) > 0 else delay_cs)
                        for p in parts
                    ]
                except ValueError:
                    frame_delays = None  # malformed → fall back to global delay

        # Boomerang / ping-pong: append the middle frames in reverse so the loop
        # plays forward then backward (1,2,3 → 1,2,3,2). Delays mirror the frames.
        if boomerang and len(frame_paths) > 2:
            mirror = list(range(len(frame_paths) - 2, 0, -1))
            frame_paths = frame_paths + [frame_paths[i] for i in mirror]
            if frame_delays is not None:
                frame_delays = frame_delays + [frame_delays[i] for i in mirror]

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}

        common = dict(
            fmt=fmt, delay_cs=delay_cs, loop=loop, width=width, height=height,
            resize_mode=resize_mode, quality=quality, rotate=rotate, flip=flip, flop=flop,
            brightness=brightness, saturation=saturation, hue=hue,
            grayscale=grayscale, negate=negate, sepia=sepia,
            auto_level=auto_level, normalize=normalize, sharpen=sharpen,
            blur=blur, colors=colors, dither=dither,
            optimize_layers=optimize_layers, strip=strip, background=background,
            frame_delays=frame_delays,
        )

        # Two-phase pipeline for every format so progress can be reported:
        #   phase 1 (transforming) — per-input transform → numbered PNG frames
        #   phase 2 (encoding)     — assemble frames (ffmpeg for APNG/video,
        #                            ImageMagick for GIF/WebP/TIFF/PDF)
        JOBS[job_id] = {"phase": "transforming", "done": 0, "total": effective_total}
        if fmt == "apng":
            _encode_apng(frame_paths, str(output_path), delay_cs, loop, common, job_tmp, run_env,
                         job_id, effective_total)
        elif fmt in ("mp4", "webm"):
            _encode_video(frame_paths, str(output_path), delay_cs, fmt, common, job_tmp, run_env,
                          job_id, effective_total)
        else:
            prep_pattern = _prep_frames(frame_paths, common, job_tmp, run_env, "Conversion",
                                        job_id, effective_total)
            JOBS[job_id] = {"phase": "encoding", "done": effective_total, "total": effective_total}
            prep_files = sorted(str(p) for p in job_tmp.glob("prep_*.png"))
            cmd = build_assembly_command(
                prep_files, str(output_path), fmt, delay_cs, loop, quality,
                colors=colors, dither=dither, optimize_layers=optimize_layers,
                frame_delays=frame_delays,
            )
            _run_or_500(cmd, job_tmp, run_env, "Conversion failed")
            if not output_path.exists():
                raise HTTPException(status_code=500, detail="Conversion produced no output file.")

        # Sidecar recipe for kept outputs (F14). frames = expanded output frame
        # count (effective_total), not the input-file count.
        if keep_output:
            out_w, out_h = _read_output_dims(output_path)
            _write_recipe(
                output_name, "convert",
                params={
                    "format": fmt, "delay": delay, "loop": loop,
                    "resize_mode": resize_mode, "quality": quality,
                    "boomerang": boomerang, "rotate": rotate, "flip": flip, "flop": flop,
                    "brightness": brightness, "saturation": saturation, "hue": hue,
                    "grayscale": grayscale, "negate": negate, "sepia": sepia,
                    "auto_level": auto_level, "normalize": normalize,
                    "sharpen": sharpen, "blur": blur,
                    "colors": colors, "dither": dither, "optimize_layers": optimize_layers,
                    "strip": strip, "background": background,
                },
                sources=[u.filename or "" for u in files],
                frames=effective_total, width=out_w, height=out_h,
            )

        return JSONResponse(
            {
                "output_filename": output_name,
                "download_url": f"/download/{output_name}",
                "delete_after_download": not keep_output,
                "file_size": output_path.stat().st_size,
            }
        )

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Conversion timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        JOBS.pop(job_id, None)  # progress entry never outlives the job
        shutil.rmtree(job_input, ignore_errors=True)
        shutil.rmtree(job_tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Explode (animation / multi-frame image → ZIP of still frames)
# ─────────────────────────────────────────────

def _run_or_500(cmd: list[str], job_tmp: Path, run_env: dict, label: str):
    """Run a subprocess, raising HTTP 500 with stderr detail on failure."""
    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=str(job_tmp), env=run_env,
    )
    if r.returncode != 0:
        detail = r.stderr.strip() or r.stdout.strip() or "Unknown error."
        raise HTTPException(status_code=500, detail=f"{label}: {detail}")
    return r


def _prep_frames(frame_paths, common_kwargs, job_tmp, run_env, label,
                 job_id=None, progress_total=0):
    """Phase 1 of the two-phase pipeline: transform each INPUT into PNG frames,
    one ImageMagick call per input, then renumber everything into one contiguous
    sequence prep_00000.png, prep_00001.png…. Per-input processing is what makes
    live progress possible (a counter ticks per input); the produced prep frames
    are byte-identical to the former single-call prep because every transform is
    a per-frame op. Returns the numbered-frame input pattern (prep_%05d.png).

    Each input writes to its own tmpNNNNN_%05d.png so a multi-frame input (a GIF
    dropped into Combine) can expand without ever colliding with another input;
    the final rename gives ffmpeg / the assembler a clean 0-based sequence.

    Updates JOBS[job_id] with {"phase":"transforming","done","total"} per input."""
    n_inputs = len(frame_paths)
    for i, fp in enumerate(frame_paths):
        per_input_pattern = str(job_tmp / f"tmp{i:05d}_%05d.png")
        cmd = build_command(
            frames=[fp], output="", frames_pattern=per_input_pattern, **common_kwargs,
        )
        _run_or_500(cmd, job_tmp, run_env, f"{label} frame preparation failed")
        produced = len(list(job_tmp.glob("tmp*_*.png")))
        if produced > MAX_EXPLODE_FRAMES:
            raise HTTPException(status_code=400, detail=f"Too many frames ({produced}).")
        if job_id:
            JOBS[job_id] = {
                "phase": "transforming",
                "done": produced,  # actual frames written so far (tracks expansion)
                "total": max(progress_total, produced, n_inputs),
            }

    # Renumber the per-input frames into one contiguous 0-based sequence. Sorting
    # by name keeps global order: the zero-padded outer index dominates, the
    # inner frame index orders an expanded input's own frames.
    tmp_frames = sorted(job_tmp.glob("tmp*_*.png"))
    if not tmp_frames:
        raise HTTPException(status_code=500, detail=f"{label} frame preparation produced no frames.")
    for seq, src in enumerate(tmp_frames):
        src.rename(job_tmp / f"prep_{seq:05d}.png")
    return str(job_tmp / "prep_%05d.png")


def _encode_apng(frame_paths, output_path, delay_cs, loop, common_kwargs, job_tmp, run_env,
                 job_id=None, progress_total=0):
    """Produce a clean APNG: transform frames to numbered PNGs with ImageMagick,
    then assemble them with ffmpeg (one APNG frame per PNG)."""
    prep_pattern = _prep_frames(frame_paths, common_kwargs, job_tmp, run_env, "APNG",
                                job_id, progress_total)
    if job_id:
        JOBS[job_id] = {"phase": "encoding", "done": progress_total, "total": progress_total}
    cmd_enc = build_apng_encode_command(prep_pattern, str(output_path), delay_cs, loop)
    _run_or_500(cmd_enc, job_tmp, run_env, "APNG encoding failed")
    if not Path(output_path).exists():
        raise HTTPException(status_code=500, detail="APNG encoding produced no output file.")


def _encode_video(frame_paths, output_path, delay_cs, fmt, common_kwargs, job_tmp, run_env,
                  job_id=None, progress_total=0):
    """Produce an MP4/WebM: same two-step model as APNG — ImageMagick prepares
    the transformed frames, ffmpeg encodes them (H.264 / VP9). `loop` is ignored
    (no video equivalent)."""
    prep_pattern = _prep_frames(frame_paths, common_kwargs, job_tmp, run_env, fmt.upper(),
                                job_id, progress_total)
    if job_id:
        JOBS[job_id] = {"phase": "encoding", "done": progress_total, "total": progress_total}
    cmd_enc = build_video_encode_command(prep_pattern, str(output_path), delay_cs, fmt)
    _run_or_500(cmd_enc, job_tmp, run_env, f"{fmt.upper()} encoding failed")
    if not Path(output_path).exists():
        raise HTTPException(status_code=500, detail=f"{fmt.upper()} encoding produced no output file.")


def _extract_frames(source: Path, job_tmp: Path, run_env: dict, prefix: str = "frame") -> list[str]:
    """Coalesce any multi-frame image into numbered PNG frames and return their
    sorted paths. APNG is decoded with ffmpeg (exact frames); everything else
    uses ImageMagick -coalesce. Enforces the frame-count cap."""
    pattern = str(job_tmp / f"{prefix}_%05d.png")
    if source.suffix.lower() in (".apng", ".mp4", ".webm"):
        # build_apng_decode_command is a generic ffmpeg frame dumper
        # (-vsync 0 passthrough): it decodes APNG and MP4/WebM identically.
        cmd = build_apng_decode_command(str(source), pattern)
    elif source.suffix.lower() == ".pdf":
        # -density is read-time and must precede the input so PDF pages rasterize
        # at a readable resolution instead of the 72-dpi default.
        cmd = ["convert", "-density", "150", str(source), "-coalesce", pattern]
    else:
        cmd = ["convert", str(source), "-coalesce", pattern]
    _run_or_500(cmd, job_tmp, run_env, "Could not read source")

    frames = sorted(str(p) for p in job_tmp.glob(f"{prefix}_*.png"))
    if not frames:
        raise HTTPException(status_code=500, detail="Source produced no frames.")
    if len(frames) > MAX_EXPLODE_FRAMES:
        raise HTTPException(status_code=400, detail=f"Too many frames ({len(frames)}).")
    return frames


def _count_frames(path: str) -> int:
    """Return the number of frames/pages in an image, or 0 if it can't be
    determined. Used as an upfront decompression-bomb guard."""
    try:
        r = subprocess.run(
            ["identify", "-format", "%n\n", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            if lines:
                return int(lines[0].strip())
    except Exception:
        pass
    return 0  # unknown → rely on post-explode count + ImageMagick resource limits


def _count_video_frames(path: str) -> int:
    """Return the number of frames in a video, or 0 if it can't be determined.
    `identify` can't count video frames reliably, so MP4/WebM use ffprobe's
    packet count as the upfront decompression-bomb guard (same role as
    _count_frames for images)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-count_packets",
             "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_packets",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            line = r.stdout.strip().splitlines()
            if line and line[0].strip().isdigit():
                return int(line[0].strip())
    except Exception:
        pass
    return 0  # unknown → rely on post-decode count + resource limits


def _read_frame_delays(path: str) -> Optional[list[int]]:
    """Return each frame's own delay in centiseconds (identify %T), so /reconvert
    can preserve the source animation's original rhythm. Returns None — meaning
    "fall back to a uniform delay" — when the delays can't be read or every value
    is 0 (stills and multi-page TIFF/PDF report 0, i.e. no animation timing).
    Values are clamped to a 1 cs floor (a 0-delay frame among non-zero ones is
    degenerate; 1 cs keeps it visible). The caller reconciles the length against
    the decoded frame count and drops these on any mismatch."""
    try:
        r = subprocess.run(
            ["identify", "-format", "%T\n", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        raw: list[int] = []
        for line in r.stdout.strip().splitlines():
            token = line.strip()
            if not token.lstrip("-").isdigit():
                return None  # unparseable → uniform fallback
            raw.append(int(token))
        if not raw or all(d == 0 for d in raw):
            return None  # no per-frame timing to preserve
        return [max(1, d) for d in raw]
    except Exception:
        return None


# ─────────────────────────────────────────────
# Pre-flight inspection (read-only probe: frame count, pixel size,
# format and per-frame delays — shown on the cards before a job runs.
# Must never block a workflow: identify failures return nulls, not 500s;
# the real gatekeeping stays in each conversion endpoint's validation.)
# ─────────────────────────────────────────────

@app.post("/inspect")
async def inspect(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")

    # Accept anything at least one direction can consume (inspection is read-only)
    ok_convert, _ = validate_file(file.filename or "", file.content_type or "")
    ok_explode, _ = validate_explode_input(file.filename or "", file.content_type or "")
    if not (ok_convert or ok_explode):
        raise HTTPException(status_code=400, detail="Unsupported file type.")

    nulls = {"frames": None, "width": None, "height": None, "format": None, "delays_cs": None}

    job_id = str(uuid.uuid4())
    job_tmp = TMP_DIR / job_id

    try:
        job_tmp.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(file.filename or "input")
        src = job_tmp / safe_name
        src.write_bytes(content)

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}
        r = subprocess.run(
            ["identify", "-format", "%n %w %h %m %T\n", str(src)],
            capture_output=True, text=True, timeout=30, cwd=str(job_tmp), env=run_env,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return nulls

        # One line per frame: "<total> <width> <height> <format> <delay_cs>"
        lines = r.stdout.strip().splitlines()
        first = lines[0].split()
        if len(first) < 5:
            return nulls
        frames = int(first[0])
        delays_cs = []
        for line in lines[:MAX_EXPLODE_FRAMES]:
            parts = line.split()
            delays_cs.append(int(parts[4]) if len(parts) >= 5 and parts[4].lstrip("-").isdigit() else 0)
        return {
            "frames": frames,
            "width": int(first[1]),
            "height": int(first[2]),
            "format": first[3],
            "delays_cs": delays_cs,
        }
    except HTTPException:
        raise
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        return nulls
    except Exception:
        return nulls
    finally:
        shutil.rmtree(job_tmp, ignore_errors=True)


@app.post("/explode")
async def explode(
    file: UploadFile = File(...),
    out_format: str = Form("png"),
    quality: int = Form(92),
    # Frame selection (0-indexed, inclusive range). Defaults = every frame.
    start: int = Form(0),
    end: Optional[int] = Form(None),
    step: int = Form(1),
):
    fmt = out_format.lower().strip()
    if fmt not in EXPLODE_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Output format must be one of: {', '.join(sorted(EXPLODE_OUTPUT_FORMATS))}.",
        )
    if not (1 <= quality <= 100):
        raise HTTPException(status_code=400, detail="Quality must be 1–100.")
    if start < 0:
        raise HTTPException(status_code=400, detail="Start frame must be 0 or greater.")
    if end is not None and end < start:
        raise HTTPException(status_code=400, detail="End frame must be greater than or equal to the start frame.")
    if not (1 <= step <= 100):
        raise HTTPException(status_code=400, detail="Every Nth (step) must be 1–100.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")

    ok, msg = validate_explode_input(file.filename or "", file.content_type or "")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    job_id = str(uuid.uuid4())
    job_input = INPUT_DIR / job_id
    job_tmp = TMP_DIR / job_id

    out_ext = EXPLODE_OUTPUT_EXT[fmt]

    try:
        job_input.mkdir(parents=True, exist_ok=True)
        job_tmp.mkdir(parents=True, exist_ok=True)

        safe_name = sanitize_filename(file.filename or "input")
        src = job_input / safe_name
        src.write_bytes(content)

        # ── Decompression-bomb guard: check frame count before exploding ──
        # Video is counted with ffprobe (identify can't count video frames);
        # both paths reject over the cap before any frame is decoded.
        if src.suffix.lower() in (".mp4", ".webm"):
            frame_count = _count_video_frames(str(src))
        else:
            frame_count = _count_frames(str(src))
        if frame_count > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"File has {frame_count} frames; the limit is {MAX_EXPLODE_FRAMES}.",
            )

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}

        if src.suffix.lower() in (".apng", ".mp4", ".webm"):
            # Decode exact frames with ffmpeg, then convert to the chosen format
            png_frames = _extract_frames(src, job_tmp, run_env, prefix="frame")
            if out_ext == "png":
                frames = sorted(Path(p) for p in png_frames)
            else:
                for i, p in enumerate(png_frames):
                    outp = job_tmp / f"frame_{i:05d}.{out_ext}"
                    _run_or_500(
                        ["convert", p, "-quality", str(quality), str(outp)],
                        job_tmp, run_env, "Extraction failed",
                    )
                frames = sorted(job_tmp.glob(f"frame_*.{out_ext}"))
        else:
            out_pattern = str(job_tmp / f"frame_%03d.{out_ext}")
            cmd = build_explode_command(str(src), out_pattern, fmt, quality)
            _run_or_500(cmd, job_tmp, run_env, "Extraction failed")
            frames = sorted(
                p for p in job_tmp.iterdir()
                if p.is_file() and p.suffix.lower() == f".{out_ext}"
            )

        if not frames:
            raise HTTPException(status_code=500, detail="No frames were extracted.")

        # ── Frame selection (start:end:step over the decoded frames) ──
        # arcnames keep their original 0-based index (patterns number from 0),
        # so a selected ZIP still reads frame_00010.png … not a re-count.
        if start >= len(frames):
            raise HTTPException(
                status_code=400,
                detail=f"Start frame {start} is beyond the last frame ({len(frames) - 1}).",
            )
        frames = frames[start:(end + 1 if end is not None else None):step]

        if len(frames) > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Too many frames extracted ({len(frames)}); the limit is {MAX_EXPLODE_FRAMES}.",
            )

        # ── Build ZIP in memory ──
        stem = sanitize_filename((file.filename or "frames").rsplit(".", 1)[0]) or "frames"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in frames:
                zf.write(p, arcname=p.name)
        zip_bytes = buf.getvalue()
        zip_name = f"{stem}_frames.zip"

        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_name}"',
                "X-Frame-Count": str(len(frames)),
            },
        )

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Extraction timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        shutil.rmtree(job_input, ignore_errors=True)
        shutil.rmtree(job_tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Convert single still → still (format converter)
# ─────────────────────────────────────────────

@app.post("/convert-image")
async def convert_image(
    file: UploadFile = File(...),
    out_format: str = Form(...),
    quality: int = Form(90),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    keep_output: bool = Form(True),
    custom_name: Optional[str] = Form(None),
):
    fmt = out_format.lower().strip()
    if fmt not in STILL_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Output format must be one of: {', '.join(sorted(STILL_OUTPUT_FORMATS))}.",
        )
    if not (1 <= quality <= 100):
        raise HTTPException(status_code=400, detail="Quality must be 1–100.")
    if width is not None and not (1 <= width <= 8000):
        raise HTTPException(status_code=400, detail="Width must be 1–8000 px.")
    if height is not None and not (1 <= height <= 8000):
        raise HTTPException(status_code=400, detail="Height must be 1–8000 px.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")

    ok, msg = validate_file(file.filename or "", file.content_type or "")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    job_id = str(uuid.uuid4())
    job_input = INPUT_DIR / job_id
    job_tmp = TMP_DIR / job_id
    out_ext = STILL_OUTPUT_EXT[fmt]

    try:
        job_input.mkdir(parents=True, exist_ok=True)
        job_tmp.mkdir(parents=True, exist_ok=True)

        safe_name = sanitize_filename(file.filename or "image")
        src = job_input / safe_name
        src.write_bytes(content)

        if keep_output:
            output_name = f"magicflow_{job_id}{out_ext}"
            if custom_name:
                safe_custom = sanitize_filename(custom_name)
                safe_stem = safe_custom.rsplit(".", 1)[0] if "." in safe_custom else safe_custom
                if safe_stem:
                    output_name = f"{safe_stem}{out_ext}"
                if (OUTPUT_DIR / output_name).exists():
                    output_name = f"{safe_stem}_{job_id[:8]}{out_ext}"
        else:
            # Not kept: prefixed name — hidden from /files, deleted on download,
            # swept by _sweep_tmp_outputs if never downloaded. Custom naming is
            # meaningless for a file that must vanish.
            output_name = f"{TMP_OUTPUT_PREFIX}{job_id}{out_ext}"
        output_path = OUTPUT_DIR / output_name

        # Read only the first frame/page so multi-frame inputs collapse to one still
        cmd = build_command(
            frames=[f"{src}[0]"],
            output=str(output_path),
            fmt=fmt,
            delay_cs=0,
            loop=0,
            width=width,
            height=height,
            quality=quality,
            still=True,
        )

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            cwd=str(job_tmp), env=run_env,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown ImageMagick error."
            raise HTTPException(status_code=500, detail=f"Conversion failed: {detail}")
        if not output_path.exists():
            raise HTTPException(status_code=500, detail="Conversion produced no output file.")

        # Sidecar recipe for kept stills (F14).
        if keep_output:
            out_w, out_h = _read_output_dims(output_path)
            _write_recipe(
                output_name, "convert-image",
                params={"format": fmt, "quality": quality},
                sources=[file.filename or ""],
                frames=1, width=out_w, height=out_h,
            )

        return JSONResponse(
            {
                "output_filename": output_name,
                "download_url": f"/download/{output_name}",
                "delete_after_download": not keep_output,
                "file_size": output_path.stat().st_size,
            }
        )

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Conversion timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        shutil.rmtree(job_input, ignore_errors=True)
        shutil.rmtree(job_tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Re-convert a stored output with new settings
# (coalesce back to frames, then re-run build_command)
# ─────────────────────────────────────────────

@app.post("/reconvert/{filename}")
async def reconvert(
    filename: str,
    format: str = Form(...),
    delay: int = Form(100),
    # Preserve each source frame's own delay by default; when False, re-time every
    # frame uniformly to `delay` (the historical behaviour).
    keep_timing: bool = Form(True),
    loop: int = Form(0),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    resize_mode: str = Form("fit"),
    quality: int = Form(85),
    keep_output: bool = Form(True),
    custom_name: Optional[str] = Form(None),
    # Timing / sequence
    boomerang: bool = Form(False),
    # Geometry
    rotate: int = Form(0),
    flip: bool = Form(False),
    flop: bool = Form(False),
    # Color adjustments
    brightness: int = Form(100),
    saturation: int = Form(100),
    hue: int = Form(100),
    # Color effects
    grayscale: bool = Form(False),
    negate: bool = Form(False),
    sepia: bool = Form(False),
    auto_level: bool = Form(False),
    normalize: bool = Form(False),
    # Sharpness / blur
    sharpen: float = Form(0.0),
    blur: float = Form(0.0),
    # GIF-specific
    colors: int = Form(256),
    dither: str = Form("Riemersma"),
    optimize_layers: bool = Form(True),
    # Output extras
    strip: bool = Form(False),
    background: str = Form(""),
):
    # ── Locate the source file in OUTPUT_DIR ──
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    source = OUTPUT_DIR / safe
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source file not found.")
    # PDF needs Ghostscript to be READ back into frames; keyed on the constant
    # so enabling PDF extraction automatically unlocks re-conversion too.
    if source.suffix.lower() == ".pdf" and ".pdf" not in EXPLODE_INPUT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="PDF sources cannot be re-converted (PDF reading is disabled in this build).",
        )
    # Video sources need ffmpeg decoding to be READ back into frames (ImageMagick
    # -coalesce can't). Keyed on the same constant so enabling video extraction
    # (F3) automatically unlocks re-converting a video too.
    if source.suffix.lower() in (".mp4", ".webm") and source.suffix.lower() not in EXPLODE_INPUT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Video sources cannot be re-converted (video reading is disabled in this build).",
        )

    # ── Validate target settings (same rules and messages as /convert) ──
    fmt = format.lower().strip()
    if fmt not in ALLOWED_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Format must be one of: {', '.join(sorted(ALLOWED_OUTPUT_FORMATS))}.",
        )
    _validate_transform_params(
        delay=delay, loop=loop, width=width, height=height,
        resize_mode=resize_mode, quality=quality, rotate=rotate,
        brightness=brightness, saturation=saturation, hue=hue,
        sharpen=sharpen, blur=blur, colors=colors, dither=dither, background=background,
    )

    job_id = str(uuid.uuid4())
    job_tmp = TMP_DIR / job_id

    ext_map = {"gif": ".gif", "webp": ".webp", "apng": ".apng", "tiff": ".tiff",
               "pdf": ".pdf", "mp4": ".mp4", "webm": ".webm"}
    out_ext = ext_map[fmt]

    try:
        job_tmp.mkdir(parents=True, exist_ok=True)

        # ── Coalesce the stored file back into full frames ──
        if source.suffix.lower() in (".mp4", ".webm"):
            frame_count = _count_video_frames(str(source))
        else:
            frame_count = _count_frames(str(source))
        if frame_count > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Source has {frame_count} frames; the limit is {MAX_EXPLODE_FRAMES}.",
            )

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}

        # ── Read the stored file back into full frames (ffmpeg for APNG) ──
        frame_paths = _extract_frames(source, job_tmp, run_env, prefix="frame")

        # ── Preserve the source's own per-frame timing (F6) ──
        # Read %T from the ORIGINAL file (identify reads GIF/WebP/APNG delays even
        # though APNG frames are decoded by ffmpeg). Drop on any length mismatch so
        # per-frame delays always line up 1:1 with the decoded frames.
        frame_delays = _read_frame_delays(str(source)) if keep_timing else None
        if frame_delays is not None and len(frame_delays) != len(frame_paths):
            frame_delays = None

        # ── Boomerang / ping-pong (same semantics as /convert) ──
        if boomerang and len(frame_paths) > 2:
            mirror = list(range(len(frame_paths) - 2, 0, -1))
            frame_paths = frame_paths + [frame_paths[i] for i in mirror]
            if frame_delays is not None:
                frame_delays = frame_delays + [frame_delays[i] for i in mirror]
        if len(frame_paths) > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Combined inputs expand to {len(frame_paths)} frames; the limit is {MAX_EXPLODE_FRAMES}.",
            )

        # ── Build a fresh output name (never overwrite the source) ──
        if keep_output:
            source_stem = source.stem
            output_name = f"magicflow_{job_id}{out_ext}"
            if custom_name:
                safe_custom = sanitize_filename(custom_name)
                safe_stem = safe_custom.rsplit(".", 1)[0] if "." in safe_custom else safe_custom
                if safe_stem:
                    output_name = f"{safe_stem}{out_ext}"
            else:
                output_name = f"{source_stem}_re{out_ext}"
            if (OUTPUT_DIR / output_name).exists():
                output_name = f"{output_name[:-len(out_ext)]}_{job_id[:8]}{out_ext}"
        else:
            # Not kept: prefixed name — hidden from /files, deleted on download,
            # swept by _sweep_tmp_outputs if never downloaded.
            output_name = f"{TMP_OUTPUT_PREFIX}{job_id}{out_ext}"
        output_path = OUTPUT_DIR / output_name

        delay_cs = max(1, round(delay / 10))

        # Uniform-framerate encoders (APNG/MP4/WebM) can't express per-frame delays.
        # When preserving non-uniform source timing for those, collapse to the mean
        # so the overall duration is honoured, and tell the UI it was averaged.
        timing_averaged = False
        encode_delay_cs = delay_cs
        if frame_delays is not None and fmt in ("apng", "mp4", "webm"):
            if len(set(frame_delays)) > 1:
                encode_delay_cs = max(1, round(sum(frame_delays) / len(frame_delays)))
                timing_averaged = True
            else:
                encode_delay_cs = frame_delays[0]  # already clamped ≥1

        common = dict(
            fmt=fmt, delay_cs=delay_cs, loop=loop, width=width, height=height,
            resize_mode=resize_mode, quality=quality, rotate=rotate, flip=flip, flop=flop,
            brightness=brightness, saturation=saturation, hue=hue,
            grayscale=grayscale, negate=negate, sepia=sepia,
            auto_level=auto_level, normalize=normalize, sharpen=sharpen,
            blur=blur, colors=colors, dither=dither,
            optimize_layers=optimize_layers, strip=strip, background=background,
            frame_delays=frame_delays,
        )

        # Cleanup covers ALL branches: a failed encode never leaves a partial file
        try:
            if fmt == "apng":
                _encode_apng(frame_paths, str(output_path), encode_delay_cs, loop, common, job_tmp, run_env)
            elif fmt in ("mp4", "webm"):
                _encode_video(frame_paths, str(output_path), encode_delay_cs, fmt, common, job_tmp, run_env)
            else:
                cmd = build_command(frames=frame_paths, output=str(output_path), **common)
                _run_or_500(cmd, job_tmp, run_env, "Re-conversion failed")
                if not output_path.exists():
                    raise HTTPException(status_code=500, detail="Re-conversion produced no output file.")
        except HTTPException:
            output_path.unlink(missing_ok=True)  # remove any partial output
            raise

        # Sidecar recipe for kept re-conversions (F14); records the source it
        # was derived from so the chain stays traceable.
        if keep_output:
            out_w, out_h = _read_output_dims(output_path)
            _write_recipe(
                output_name, "reconvert",
                params={
                    "format": fmt, "delay": delay, "keep_timing": keep_timing, "loop": loop,
                    "resize_mode": resize_mode, "quality": quality,
                    "boomerang": boomerang, "rotate": rotate, "flip": flip, "flop": flop,
                    "brightness": brightness, "saturation": saturation, "hue": hue,
                    "grayscale": grayscale, "negate": negate, "sepia": sepia,
                    "auto_level": auto_level, "normalize": normalize,
                    "sharpen": sharpen, "blur": blur,
                    "colors": colors, "dither": dither, "optimize_layers": optimize_layers,
                    "strip": strip, "background": background, "source": safe,
                },
                sources=[safe],
                frames=len(frame_paths), width=out_w, height=out_h,
            )

        return JSONResponse(
            {
                "output_filename": output_name,
                "download_url": f"/download/{output_name}",
                "delete_after_download": not keep_output,
                "file_size": output_path.stat().st_size,
                "frame_count": len(frame_paths),
                # "averaged": source had per-frame timing collapsed to a uniform
                # rate for a video/APNG target; "preserved": kept as-is; absent
                # when the user opted out or the source had no per-frame timing.
                "timing": "averaged" if timing_averaged
                          else ("preserved" if frame_delays is not None else None),
            }
        )

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Re-conversion timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        shutil.rmtree(job_tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# Thumbnail (first frame/page → 480px JPEG, cached by mtime)
# Cards use this so TIFF/PDF/RAW/HEIC/video get real previews and the
# grid stops downloading multi-megabyte animations to paint a small card.
# ─────────────────────────────────────────────

VIDEO_THUMB_EXTENSIONS = frozenset({".mp4", ".webm"})


@app.get("/thumb/{filename}")
async def thumb(filename: str):
    # Same path-safety contract as /download: reject anything sanitization
    # would alter (traversal, separators) before touching the filesystem.
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    path = OUTPUT_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    # Cache key ties the thumbnail to the source's mtime, so replacing the
    # output (same name, new content) invalidates the old thumbnail.
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        raise HTTPException(status_code=404, detail="File not found.")
    cache_path = THUMB_DIR / f"{safe}.{mtime}.jpg"

    if not cache_path.exists():
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        # Drop stale thumbnails for this file (older mtimes). sanitize_filename
        # restricts names to [a-zA-Z0-9._-], so no glob metacharacters survive.
        for stale in THUMB_DIR.glob(f"{safe}.*"):
            if stale != cache_path:
                try:
                    stale.unlink()
                except OSError:
                    pass

        ext = path.suffix.lower()
        if ext in VIDEO_THUMB_EXTENSIONS:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(path),
                   "-frames:v", "1", "-vf", "scale=480:-1", str(cache_path)]
        else:
            # [0] = first frame/page; -density helps PDF rasterize legibly and
            # is harmless for raster inputs. List args only — never shell=True.
            cmd = ["convert", f"{path}[0]", "-density", "150",
                   "-thumbnail", "480x", "-quality", "80", str(cache_path)]

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(THUMB_DIR)}
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=run_env)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=404, detail="Thumbnail render timed out.")
        # Degrade to the card's icon fallback (404) rather than a 500 on any
        # unrenderable / corrupt source.
        if r.returncode != 0 or not cache_path.exists():
            cache_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="Thumbnail unavailable.")

    return FileResponse(str(cache_path), media_type="image/jpeg")


@app.get("/download/{filename}")
async def download(filename: str, delete: bool = False):
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    path = OUTPUT_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    ext = path.suffix.lower()
    media_type = OUTPUT_MEDIA_TYPES.get(ext, "application/octet-stream")

    if delete:
        content = path.read_bytes()
        path.unlink(missing_ok=True)
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )

    return FileResponse(str(path), media_type=media_type, filename=safe)
