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

import contextvars
import errno
import stat as stat_module
import hashlib
import json
import math
import os
import re
from pdf_layout import resolve_layout, build_page_command, build_pdf_command
import shutil
import signal
import subprocess
import threading
import tempfile
import time
import uuid
import zipfile
import zlib
from contextlib import ExitStack, asynccontextmanager, contextmanager
from functools import wraps
from pathlib import Path
from typing import List, Optional

import capabilities
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles

from utils import (
    ALLOWED_MIME_TYPES,
    ICO_SIZES,
    validate_avif_still,
    PROCESSING_POLICY,
    color_normalization_args,
    ANIMATION_CANVAS_FORMATS,
    FFMPEG_FRAME_CODERS,
    PNG_SIGNATURE,
    build_canvas_command,
    CONCAT_LIST_NAME,
    build_concat_list,
    is_apng,
    DEFAULT_QUALITY,
    video_crf_for_quality,
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
# Encoders write here, never straight into the library: a job's output becomes
# visible only when the finished file is linked into OUTPUT_DIR under a reserved
# name (see _publish_output). Deliberately a subdirectory of OUTPUT_DIR — /data/tmp
# may be a different volume, where the publishing rename would not be atomic —
# and a directory, so the /files listing (files only) skips it.
STAGING_DIR = OUTPUT_DIR / ".staging"

# Outputs ImageMagick cannot inspect: `identify` delegates video decoding to
# ffmpeg, which the hardened policy does not grant.
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm"})
# The same two as output *format* names, for the encoder settings a recipe
# records.
VIDEO_OUTPUT_FORMATS = frozenset({"mp4", "webm"})

# Formats whose extra "frames" are layers rather than moments in time. Read as
# the flattened composite, which is what the file looks like when opened.
LAYERED_EXTENSIONS = frozenset({".psd"})

def _env_int(name: str, default: int) -> int:
    """An operator override for a resource budget. A missing, malformed or
    non-positive value falls back to the default: a budget must never be
    removed by a typo in the environment."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB per file
MAX_FILES = 60                     # hard backend cap
MAX_EXPLODE_FRAMES = 2000          # decompression-bomb guard for /explode

# ── Resource budgets ──────────────────────────────────────────────────────────
# Input bytes bound neither the decoded pixels nor the peak memory a job needs:
# a 1 MB PNG can be 300 megapixels once decoded, and sixty files inside the
# per-file limit are still 3 GB of upload. These budgets are therefore checked
# BEFORE the work is paid for — before the body is read, before a decoder runs,
# before an archive is built — and each can be raised or lowered by an operator.

# Everything one job may upload, across all its files.
MAX_TOTAL_UPLOAD_BYTES = _env_int("MAGICFLOW_MAX_TOTAL_UPLOAD_BYTES", 1024 * 1024 * 1024)
# Room for the multipart framing and form fields around those bytes, so the
# early Content-Length check never rejects a request the quota would allow.
REQUEST_OVERHEAD_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_TOTAL_UPLOAD_BYTES + REQUEST_OVERHEAD_BYTES

# Per-side and whole-job ceilings on what a job may decode. The dimension cap
# mirrors the ImageMagick policy's 16KP so the app can say what happened
# instead of surfacing a coder error.
MAX_IMAGE_DIMENSION = _env_int("MAGICFLOW_MAX_IMAGE_DIMENSION", 16000)
MAX_DECODED_PIXELS = _env_int("MAGICFLOW_MAX_DECODED_PIXELS", 2_000_000_000)

# Free space that must remain after a job is admitted. Scratch frames are far
# larger than their compressed sources, so filling the volume is the normal way
# a big job fails; refusing early gives the user something they can act on.
MIN_FREE_DISK_BYTES = _env_int("MAGICFLOW_MIN_FREE_DISK_BYTES", 1024 * 1024 * 1024)

# Inputs that can never expand to multiple frames — skipped by the /convert
# expanded-frame guard so large jobs don't pay one identify call per still.
SINGLE_FRAME_EXTENSIONS = frozenset({".jpg", ".jpeg", ".bmp", ".tga"}) | RAW_EXTENSIONS

# Outputs made with "Keep on server" off get this prefix. It is a readability
# hint only: what actually makes an output temporary is an entry in the registry
# below. A name alone never authorizes deletion, so a kept file that happens to
# carry the prefix is safe (and new kept names may not use it — see
# _reject_reserved_output_name).
TMP_OUTPUT_PREFIX = "magicflow_tmp_"

# Explicit ownership record for not-kept outputs: {filename: {"expires": epoch}}.
# Lives beside the files it describes so it shares their volume, and is skipped
# by /files (its suffix is not an output extension) and unreachable through the
# API (sanitize_filename rejects leading dots).
TEMP_REGISTRY_FILE = OUTPUT_DIR / ".temp-outputs.json"
TEMP_OUTPUT_TTL_S = 3600  # how long an undownloaded temporary output survives

# ── Job runtime ───────────────────────────────────────────────────────────────
# Media work is CPU- and memory-bound, so letting several conversions run at
# once on a small self-hosted box makes all of them slower and can exhaust it.
# One job runs at a time, a few may wait, and anything past that is refused
# rather than queued without bound.
MAX_CONCURRENT_JOBS = 1
MAX_WAITING_JOBS = 4

# A job's total wall-clock budget. Every step also has its own ceiling, but a
# job with many inputs runs many steps, so without a job-wide budget one stuck
# conversion could hold the only slot for hours.
JOB_BUDGET_S = 600
SUBPROCESS_TIMEOUT_S = 120   # ceiling for any single step
PROBE_TIMEOUT_S = 30         # ceiling for a metadata/thumbnail probe

# How long a finished job's outcome stays pollable. Deleting the record as the
# response is written loses the outcome to a poll that is already in flight.
JOB_RECORD_TTL_S = 300

# The phase vocabulary /progress reports; the UI renders these by name.
JOB_PHASE_QUEUED = "queued"
JOB_PHASE_TRANSFORMING = "transforming"
JOB_PHASE_ENCODING = "encoding"
JOB_PHASE_DONE = "done"
JOB_PHASE_FAILED = "failed"
JOB_PHASE_UNKNOWN = "unknown"

# Live conversion progress, keyed by job_id: {"phase", "done", "total"}.
# In-memory is safe because the app runs a SINGLE uvicorn worker
# (Dockerfile: --workers 1). A multi-worker deployment would need a shared
# store (Redis / a file per job) since a poll could hit a different worker.
# Jobs now run in worker threads while /progress is answered on the event loop,
# so every access takes the lock.
JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# One job at a time, plus a bounded line of waiters.
_job_slot = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
_queue_lock = threading.Lock()
_waiting_jobs = 0

# Children this process has started, so a timeout or a shutdown can stop them.
_live_processes: set[subprocess.Popen] = set()
_processes_lock = threading.Lock()

# When the current job must be finished by. Set per request, so each job's steps
# share one budget; Starlette copies the context into its worker thread, which
# keeps this per-request even though threads are reused.
_job_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "magicflow_job_deadline", default=None)

# User-preference persistence limits
SETTINGS_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_SETTINGS_KEYS = 100
# 8 KB per value: most settings are short scalars, but `user_presets` (F20) packs
# a JSON-encoded map of saved Combine snapshots into one string value, which a
# 256-char cap can't hold. MAX_SETTINGS_BYTES (below) remains the real ceiling on
# the whole payload, so this only widens the per-value allowance.
MAX_SETTINGS_VALUE_LEN = 8192
MAX_SETTINGS_BYTES = 64 * 1024


# What this deployment can actually do, discovered at startup (see
# capabilities.py). Empty until the lifespan fills it, so every reader goes
# through the helpers below rather than touching it directly.
CAPABILITIES: dict = {}


def _supported(kind: str, offered) -> list:
    """The offered formats this build proved it can handle. Before discovery has
    run — only possible in a test that imports the module without the lifespan —
    the static offer stands, which is the previous behaviour."""
    discovered = CAPABILITIES.get(kind)
    if discovered is None:
        return sorted(offered)
    return sorted(set(offered) & set(discovered))


def _reject_unavailable_format(kind: str, fmt: str, offered) -> None:
    """Refuse a format this build cannot produce, saying why, instead of letting
    the encoder fail later with a coder error."""
    if fmt in _supported(kind, offered):
        return
    reason = CAPABILITIES.get("unavailable", {}).get(
        f"{'still' if kind == 'still_outputs' else 'animation'}:{fmt}")
    detail = f"{fmt.upper()} is not available in this build."
    raise HTTPException(status_code=400, detail=f"{detail} {reason}" if reason else detail)


def _set_job(job_id: Optional[str], phase: str, done: int = 0, total: int = 0) -> None:
    """Record what a job is doing now. Routes that take no job id pass None."""
    if not job_id:
        return
    with _jobs_lock:
        JOBS[job_id] = {"phase": phase, "done": done, "total": total}


def _finish_job(job_id: Optional[str], phase: str) -> None:
    """Replace a job's live record with a terminal one instead of deleting it,
    so a poll that lands just after the response still learns how the job ended.
    Terminal records are evicted here once they are older than their retention,
    which bounds the map without a timer."""
    if not job_id:
        return
    now = time.monotonic()
    with _jobs_lock:
        JOBS[job_id] = {"phase": phase, "finished": now}
        for stale in [key for key, record in JOBS.items()
                      if now - record.get("finished", now) > JOB_RECORD_TTL_S]:
            del JOBS[stale]


def _read_job(job_id: str) -> dict:
    with _jobs_lock:
        record = dict(JOBS.get(job_id) or {"phase": JOB_PHASE_UNKNOWN})
    record.pop("finished", None)  # internal retention clock, not the client's
    return record


@contextmanager
def job_slot(job_id: Optional[str] = None):
    """Admit one job at a time, with a bounded line of waiters.

    A waiting job reports the `queued` phase, so the UI can say so rather than
    appearing stuck, and holding the slot also starts the job's total budget —
    the two belong together, because the budget is what guarantees a waiter
    eventually gets its turn."""
    global _waiting_jobs
    with _queue_lock:
        if _waiting_jobs >= MAX_WAITING_JOBS:
            raise HTTPException(
                status_code=503,
                detail="The server is busy with other conversions. Try again shortly.")
        _waiting_jobs += 1
    _set_job(job_id, JOB_PHASE_QUEUED)
    try:
        admitted = _job_slot.acquire(timeout=JOB_BUDGET_S)
    finally:
        with _queue_lock:
            _waiting_jobs -= 1
    if not admitted:
        raise HTTPException(
            status_code=503,
            detail="Timed out waiting for the converter. Try again shortly.")

    token = _job_deadline.set(time.monotonic() + JOB_BUDGET_S)
    try:
        # Checked here rather than per route: admission is the one place every
        # job passes through, and free space is only meaningful once this job
        # is the one about to write.
        _require_free_space()
        yield
    finally:
        _job_deadline.reset(token)
        _job_slot.release()


def _progress_id(raw: Optional[str]) -> Optional[str]:
    """The canonical form of a client-supplied job id, or None when it is absent
    or malformed. Normalizing in one place keeps the queued, live and terminal
    records of a job under the same key."""
    try:
        return str(uuid.UUID(raw)) if raw else None
    except (ValueError, AttributeError, TypeError):
        return None


def _require_library_layout() -> None:
    for directory in (OUTPUT_DIR, STAGING_DIR, RECIPE_DIR):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise HTTPException(status_code=503, detail="Output library directories must be real directories, not links.")
    if TEMP_REGISTRY_FILE.is_symlink() or (TEMP_REGISTRY_FILE.exists() and not TEMP_REGISTRY_FILE.is_file()):
        raise HTTPException(status_code=503, detail="Temporary-output metadata must be a regular file.")


def with_job_slot(handler):
    """Run a media route under the single job slot and record how it ended.

    A decorator rather than a block inside each route: the four producers
    already own long bodies with their own try/finally cleanup, and wrapping
    them here keeps admission, the budget and the terminal record in one place
    instead of four. functools.wraps keeps the signature FastAPI reads for
    dependency injection."""
    @wraps(handler)
    def wrapper(*args, **kwargs):
        job_id = _progress_id(kwargs.get("job_id"))
        with job_slot(job_id):
            try:
                _require_library_layout()
                result = handler(*args, **kwargs)
            except BaseException:
                _finish_job(job_id, JOB_PHASE_FAILED)
                raise
        _finish_job(job_id, JOB_PHASE_DONE)
        return result
    return wrapper


def _megabytes(value: int) -> int:
    return value // (1024 * 1024)


def _read_upload(upload: UploadFile, label: str) -> bytes:
    """Read one uploaded file, having first refused it on size.

    `UploadFile.size` is what the multipart parser actually wrote, not a number
    the client declared, so checking it before `.read()` keeps an oversize file
    from becoming a Python allocation on top of the spooled copy."""
    size = upload.size
    if size is not None and size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"{label} exceeds the {_megabytes(MAX_FILE_SIZE)} MB per-file limit.")
    content = upload.file.read()
    if size is None and len(content) > MAX_FILE_SIZE:
        # Only reachable if the parser reported no size; the allocation has
        # already happened by then, so this is the backstop, not the guard.
        raise HTTPException(
            status_code=413,
            detail=f"{label} exceeds the {_megabytes(MAX_FILE_SIZE)} MB per-file limit.")
    return content


def _require_upload_quota(uploads: list[UploadFile]) -> None:
    """Refuse a job whose files together exceed the per-job quota, before any of
    them is read. Per-file limits alone let sixty admissible files add up to
    something no small server can hold."""
    total = sum(upload.size or 0 for upload in uploads)
    if total > MAX_TOTAL_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"These files total {_megabytes(total)} MB; one job may upload "
                   f"{_megabytes(MAX_TOTAL_UPLOAD_BYTES)} MB.")


def _require_free_space() -> None:
    """Refuse a job when the scratch volume is nearly full. Decoded frames are
    much larger than the sources they came from, so a job admitted onto a full
    volume fails deep inside an encoder with an unhelpful error."""
    try:
        free = shutil.disk_usage(TMP_DIR).free
    except OSError:
        return  # a broken probe must not block work
    if free < MIN_FREE_DISK_BYTES:
        raise HTTPException(
            status_code=507,
            detail=f"Not enough free disk space: {_megabytes(free)} MB available, "
                   f"{_megabytes(MIN_FREE_DISK_BYTES)} MB required. Delete some saved "
                   f"files or free space on the volume and try again.")


def _require_within_dimension_budget(width: int, height: int, label: str) -> None:
    if max(width, height) > MAX_IMAGE_DIMENSION:
        raise HTTPException(
            status_code=400,
            detail=f"{label} is {width}×{height}px; the limit is "
                   f"{MAX_IMAGE_DIMENSION}px on a side.")


def _require_measurable(width: int, height: int, label: str) -> None:
    """Refuse a source whose size cannot be read.

    This is not pedantry: `identify` refuses an image past the policy's
    dimension or area limits *silently* — exit 1, no stdout, no stderr — so an
    unmeasurable source would skip every budget below and fail later inside the
    encoder with "no images defined". The probe and the converter are the same
    binary with the same policy and delegates, so anything the probe cannot read
    the conversion could not have produced either."""
    if width and height:
        return
    raise HTTPException(
        status_code=400,
        detail=f"{label} could not be read. It may exceed this build's limits "
               f"({MAX_IMAGE_DIMENSION}px per side), or its format may not be "
               f"supported here.")


def _require_within_pixel_budget(pixels: int) -> None:
    """Total decoded pixels a job may produce. Checked from metadata before any
    decoding, so a small file that expands enormously is refused rather than
    discovered. Inputs whose size cannot be probed contribute nothing here and
    are left to the frame cap and ImageMagick's own policy."""
    if pixels > MAX_DECODED_PIXELS:
        raise HTTPException(
            status_code=400,
            detail=f"This job would decode about {pixels // 1_000_000} megapixels; "
                   f"the limit is {MAX_DECODED_PIXELS // 1_000_000}. Use fewer or "
                   f"smaller frames.")


# Every decoder in the app writes frames numbered from zero with this width, so
# one sequence means the same thing whoever produced it. Five digits covers the
# frame cap with room to spare.
FRAME_NUMBER_PATTERN = "%05d"
_FRAME_NUMBERS = re.compile(r"\d+")


def _frame_order_key(path) -> tuple[int, ...]:
    """Order frame files by the numbers in their names rather than as text.

    Sorting as text is only the frame order while every name is the same width:
    at a thousand frames `frame_1000` sorts between `frame_100` and `frame_101`,
    and a requested page range quietly returns different pages. A tuple of every
    number in the name also orders the two-field scratch names (input index,
    then frame index) correctly."""
    return tuple(int(number) for number in _FRAME_NUMBERS.findall(Path(path).name))


def _select_frames(frames: list[Path], start: int, end: Optional[int],
                   step: int) -> list[Path]:
    """The start:end:step slice of a decoded sequence, end inclusive."""
    return frames[start:(end + 1 if end is not None else None):step]


# Already-compressed media gains nothing from DEFLATE, so the archive is stored:
# it costs no CPU and keeps the writer streaming straight to disk.
ARCHIVE_COMPRESSION = zipfile.ZIP_STORED


def _open_output(name: str, subdir: Optional[str] = None, *, directory: Optional[Path] = None):
    """Pin one regular library inode, refusing links at the root/child boundary."""
    if (directory is None and subdir is None and sanitize_filename(name) != name) or Path(name).name != name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    root = directory or OUTPUT_DIR
    directory = None
    descriptor = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if subdir is not None:
            child = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
            raise HTTPException(status_code=404, detail="Not a regular media file.")
        handle = os.fdopen(descriptor, "rb")
        descriptor = None
        return handle
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EACCES):
            raise HTTPException(status_code=404, detail="Regular media file not found.")
        raise HTTPException(status_code=503, detail="Could not open the media file.")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _atomic_output_text(name: str, payload: str, subdir: Optional[str] = None) -> None:
    """Write owned metadata via an anchored directory and exclusive temporary file."""
    if Path(name).name != name or name in (".", ".."):
        raise OSError(errno.EINVAL, "Invalid metadata filename")
    root = os.open(OUTPUT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".metadata-" + uuid.uuid4().hex + ".tmp"
    try:
        if subdir is not None:
            try:
                os.mkdir(subdir, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            child = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            os.close(root)
            root = child
        try:
            if not stat_module.S_ISREG(os.stat(name, dir_fd=root, follow_symlinks=False).st_mode):
                raise OSError(errno.ELOOP, "Metadata target is not a regular file")
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(temporary, name, src_dir_fd=root, dst_dir_fd=root)
    finally:
        try:
            os.unlink(temporary, dir_fd=root)
        except FileNotFoundError:
            pass
        os.close(root)


def _unlink_output(name: str, subdir: Optional[str] = None) -> bool:
    if Path(name).name != name or name in (".", ".."):
        return False
    root = os.open(OUTPUT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if subdir is not None:
            child = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            os.close(root)
            root = child
        info = os.stat(name, dir_fd=root, follow_symlinks=False)
        if not stat_module.S_ISREG(info.st_mode):
            return False
        os.unlink(name, dir_fd=root)  # unlink never follows a last-moment replacement link
        return True
    except FileNotFoundError:
        return True  # an already missing entry is safely gone
    finally:
        os.close(root)


def _regular_output(name: str) -> bool:
    try:
        with _open_output(name):
            return True
    except HTTPException as error:
        if error.status_code == 404:
            return False
        raise


class _PinnedFileResponse(FileResponse):
    """Preserve range/inline semantics without reopening the mutable library name."""
    def __init__(self, handle, **kwargs):
        self._handle = handle
        super().__init__(f"/proc/self/fd/{handle.fileno()}", stat_result=os.fstat(handle.fileno()), **kwargs)

    async def __call__(self, scope, receive, send):
        # Force reads inside this process while its descriptor is owned here.
        scope = {**scope, "extensions": {key: value for key, value in scope.get("extensions", {}).items()
                                        if key != "http.response.pathsend"}}
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._handle.close()


def _pinned_staging_path(name: str) -> tuple[Path, int]:
    """Keep encoders inside the original private staging directory across renames."""
    root = os.open(OUTPUT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    child = None
    try:
        try:
            os.mkdir(".staging", mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        child = os.open(".staging", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        os.fchmod(child, 0o700)
        path = Path(f"/proc/{os.getpid()}/fd/{child}") / name
        return path, child
    except BaseException:
        if child is not None:
            os.close(child)
        raise
    finally:
        os.close(root)


def _snapshot_output(name: str, job_tmp: Path, source_handle=None) -> Path:
    if sanitize_filename(name) != name:
        raise HTTPException(status_code=400, detail="Invalid source filename.")
    destination = job_tmp / "source" / name
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ExitStack() as stack:
        source = source_handle if source_handle is not None else stack.enter_context(_open_output(name))
        size = os.fstat(source.fileno()).st_size
        if shutil.disk_usage(job_tmp).free < size + MIN_FREE_DISK_BYTES:
            raise HTTPException(status_code=507, detail="Not enough space for a safe source snapshot.")
        with destination.open("xb") as target:
            remaining = size
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise HTTPException(status_code=409, detail="Source changed while being copied; retry.")
                target.write(chunk)
                remaining -= len(chunk)
    return destination


def _zip_response(paths: list[Path], zip_name: str,
                  headers: Optional[dict] = None) -> FileResponse:
    """Write a ZIP to the scratch volume and stream it back.

    Building the archive in memory cost roughly twice its size in RSS — once for
    the buffer and once for the copy `getvalue()` makes — which a 2 GiB cap
    turns into 4 GiB of peak memory on a box that may not have it. Writing it to
    a job-shaped directory means an abandoned one is reclaimed at startup like
    any other scratch, and the response deletes it once the bytes are sent."""
    archive_dir = TMP_DIR / str(uuid.uuid4())
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / "archive.zip"
    try:
        with ExitStack() as stack:
            opened = []
            for path in paths:
                if path.parent == OUTPUT_DIR:
                    handle = stack.enter_context(_open_output(path.name))
                else:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                        os.close(fd)
                        raise HTTPException(status_code=404, detail="Not a regular archive input.")
                    handle = stack.enter_context(os.fdopen(fd, "rb"))
                opened.append((path.name, handle, os.fstat(handle.fileno()).st_size))
            total = sum(size for _, _, size in opened)
            if total > MAX_ARCHIVE_BYTES:
                raise HTTPException(status_code=413, detail="Archive selection exceeds the byte limit.")
            _require_free_space()
            with zipfile.ZipFile(archive_path, "w", ARCHIVE_COMPRESSION) as archive:
                for name, handle, size in opened:
                    # Copy only the pinned size, even if another process appends.
                    remaining = size
                    with archive.open(name, "w") as target:
                        while remaining:
                            chunk = handle.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                raise HTTPException(status_code=409, detail="Archive input changed; retry.")
                            target.write(chunk)
                            remaining -= len(chunk)
    except OSError as error:
        shutil.rmtree(archive_dir, ignore_errors=True)
        raise HTTPException(status_code=507, detail=f"Could not build the archive: {error}")
    except BaseException:
        shutil.rmtree(archive_dir, ignore_errors=True)
        raise

    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=zip_name,
        headers=headers,
        background=BackgroundTask(shutil.rmtree, archive_dir, ignore_errors=True),
    )


def _step_timeout() -> float:
    """How long the next step may run: its own ceiling, or whatever is left of
    the job's budget, whichever is smaller. Raises once the budget is spent, so
    a job with many steps cannot multiply the per-step ceiling indefinitely."""
    deadline = _job_deadline.get()
    if deadline is None:
        return SUBPROCESS_TIMEOUT_S  # outside a job (probe, thumbnail)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HTTPException(status_code=504, detail="Job exceeded its time budget.")
    return min(SUBPROCESS_TIMEOUT_S, remaining)


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Stop a child and everything it started. Python's own timeout kills only
    the direct child, which leaves a delegate — ghostscript under a PDF read,
    ffmpeg under a video encode — running on a box that has given up on it."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def _run_subprocess(cmd: list[str], timeout: float, cwd=None, env=None):
    """Every child process this app starts goes through here: in its own process
    group, registered while it runs, and killed as a group if it overruns."""
    if Path(cmd[0]).name in ("ffmpeg", "ffprobe"):
        demuxers = "mov,matroska,webm,apng,png_pipe,image2"
        if any(cmd[index:index + 2] == ["-f", "concat"] for index in range(len(cmd) - 1)):
            demuxers += ",concat"
        cmd = [cmd[0], "-protocol_whitelist", "file,pipe", "-format_whitelist", demuxers, *cmd[1:]]
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=cwd, env=env, start_new_session=True,
    )
    with _processes_lock:
        _live_processes.add(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        process.communicate()  # reap, now that the group is gone
        raise
    finally:
        with _processes_lock:
            _live_processes.discard(process)
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _stop_running_jobs() -> None:
    """Shutdown: stop every child still running, as a group. Without this a
    SIGTERM leaves encoders burning CPU on a container that is going away."""
    with _processes_lock:
        running = list(_live_processes)
    for process in running:
        _terminate_process_group(process)


def _is_job_directory(name: str) -> bool:
    """A job's scratch directory is named after its storage id and nothing else
    is, which is what makes reclaiming one safe."""
    try:
        uuid.UUID(name)
    except ValueError:
        return False
    return True


def _reclaim_abandoned_jobs() -> None:
    """Delete scratch left by jobs an earlier run never finished. Only
    UUID-named directories directly under the input and tmp roots are touched,
    so the thumbnail cache (TMP_DIR/thumbs) and anything a user put there by
    hand are left alone, and outputs are never considered at all. Safe at
    startup: one worker, nothing running yet."""
    reclaimed = 0
    for root in (INPUT_DIR, TMP_DIR):
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and _is_job_directory(entry.name):
                shutil.rmtree(entry, ignore_errors=True)
                reclaimed += 1
    if reclaimed:
        print(f"[magicflow] reclaimed {reclaimed} abandoned job scratch "
              f"director{'y' if reclaimed == 1 else 'ies'} from a previous run.",
              flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _require_library_layout()
    for directory in (INPUT_DIR, OUTPUT_DIR, TMP_DIR, CONFIG_DIR, THUMB_DIR,
                      RECIPE_DIR, STAGING_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    CAPABILITIES.update(capabilities.detect())
    withheld = CAPABILITIES.get("unavailable", {})
    if withheld:
        print(f"[magicflow] {len(withheld)} advertised capability/capabilities are "
              f"not available in this build and will not be offered: "
              f"{', '.join(sorted(withheld))}", flush=True)
    _clear_stale_staging()
    _reclaim_abandoned_jobs()
    _sweep_tmp_outputs()
    _log_unowned_legacy_outputs()
    try:
        yield
    finally:
        _stop_running_jobs()


app = FastAPI(title="MagicFlow", docs_url=None, redoc_url=None, lifespan=lifespan)

# ── Cross-origin write protection ─────────────────────────────────────────────
# The app has no authentication by design (local use), so a browser on any other
# site could otherwise drive state-changing requests with the user's access.
# Methods that never change state stay unrestricted so downloads and previews
# keep working from anywhere they already did.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Deployments whose reverse proxy rewrites Host (so it no longer matches the
# browser's Origin) list the browser-visible origins here, comma-separated,
# e.g. MAGICFLOW_ALLOWED_ORIGINS=https://magicflow.example.com
ALLOWED_ORIGINS = frozenset(
    origin.strip().rstrip("/").lower()
    for origin in os.environ.get("MAGICFLOW_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)


def _origin_is_trusted(origin: str, host: Optional[str]) -> bool:
    """True when a browser-supplied Origin belongs to this same deployment.

    Browsers attach Origin to every cross-origin request and to every non-GET
    request, so comparing it to the Host the request arrived on identifies a
    same-site write without needing cookies, tokens or an allow-list. An
    opaque origin ("null", from a sandboxed frame) has no netloc and is
    therefore never trusted."""
    if origin.strip().rstrip("/").lower() in ALLOWED_ORIGINS:
        return True
    origin_netloc = urlsplit(origin).netloc.lower()
    return bool(origin_netloc) and origin_netloc == (host or "").strip().lower()


@app.middleware("http")
async def reject_oversize_requests(request, call_next):
    """Refuse a body that cannot possibly be within budget before any of it is
    read. Without this the multipart parser has already received and spooled the
    whole request — and the handler has already allocated it as a Python
    bytes object — by the time a size check runs. A chunked upload declares no
    length and is not caught here; the per-file and per-job quotas still apply
    once the parser has it."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"Request body exceeds the "
                               f"{MAX_TOTAL_UPLOAD_BYTES // (1024 * 1024)} MB per-job limit."},
        )
    return await call_next(request)


@app.middleware("http")
async def block_cross_origin_writes(request, call_next):
    """Reject state-changing requests that a foreign site initiated. Requests
    with no Origin header (curl, scripts, server-to-server) are left alone:
    they are not browser-driven, so the user's session cannot be abused."""
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and not _origin_is_trusted(origin, request.headers.get("host")):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin request rejected."},
            )
    return await call_next(request)


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
    return _read_job(job_id)


# ─────────────────────────────────────────────
# Server configuration (read-only; the frontend derives its
# allow-lists, caps and labels from this so formats/limits
# live in exactly one place: utils.py + the constants above)
# ─────────────────────────────────────────────

def _build_identity() -> dict:
    manifest = Path(__file__).with_name("release-manifest.json")
    if not manifest.exists():
        return {"version": "development", "revision": "working-tree", "source_sha256": None}
    data = json.loads(manifest.read_text())
    return {key: data[key] for key in ("version", "revision", "source_sha256")}


@app.get("/config")
async def get_config():
    return {
        "build": _build_identity(),
        # Verified against the running tools, not merely offered: the browser
        # must not present a format this build cannot produce.
        "allowed_input_exts": _supported("input_extensions", ALLOWED_EXTENSIONS),
        "explode_input_exts": _supported("explode_inputs", EXPLODE_INPUT_EXTENSIONS),
        "still_output_formats": _supported("still_outputs", STILL_OUTPUT_FORMATS),
        "ico_sizes": list(ICO_SIZES),
        "explode_output_formats": _supported("still_outputs", EXPLODE_OUTPUT_FORMATS),
        "animation_output_formats": _supported("animation_outputs", ALLOWED_OUTPUT_FORMATS),
        "raw_input_exts": sorted(RAW_EXTENSIONS),
        # The browser used to keep its own copy of this list and drifted from
        # it: a PSD was allow-listed by extension here and refused by MIME there.
        "allowed_mime_types": sorted(ALLOWED_MIME_TYPES),
        "unavailable": CAPABILITIES.get("unavailable", {}),
        "max_file_size": MAX_FILE_SIZE,
        "max_files": MAX_FILES,
        "max_explode_frames": MAX_EXPLODE_FRAMES,
        "max_total_upload_bytes": MAX_TOTAL_UPLOAD_BYTES,
        "max_image_dimension": MAX_IMAGE_DIMENSION,
        "max_decoded_pixels": MAX_DECODED_PIXELS,
        "output_dir": str(OUTPUT_DIR),
    }


# ─────────────────────────────────────────────
# User preferences (persisted to mounted config volume,
# survives container restart / rebuild / page refresh)
# ─────────────────────────────────────────────

SETTINGS_PRESET_KEYS = frozenset({
    'setting-auto-level',
    'setting-background',
    'setting-background-picker',
    'setting-blur',
    'setting-boomerang',
    'setting-brightness',
    'setting-colors',
    'setting-delay',
    'setting-dither',
    'setting-flip',
    'setting-flop',
    'setting-format',
    'setting-grayscale',
    'setting-height',
    'setting-hue',
    'setting-loop',
    'setting-max-files',
    'setting-negate',
    'setting-normalize',
    'setting-optimize-layers',
    'setting-pdf-page-size',
    'setting-pdf-orientation',
    'setting-pdf-width-mm',
    'setting-pdf-height-mm',
    'setting-pdf-margin-mm',
    'setting-pdf-fit',
    'setting-quality',
    'setting-resize-mode',
    'setting-rotate',
    'setting-saturation',
    'setting-sepia',
    'setting-sharpen',
    'setting-strip',
    'setting-width',
})
SETTINGS_CHECKBOX_KEYS = frozenset({
    'setting-auto-level',
    'setting-boomerang',
    'setting-flip',
    'setting-flop',
    'setting-grayscale',
    'setting-negate',
    'setting-normalize',
    'setting-optimize-layers',
    'setting-sepia',
    'setting-strip',
})

def _read_settings_document():
    raw = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else b""
    revision = hashlib.sha256(raw).hexdigest()
    if not SETTINGS_FILE.exists():
        return {}, revision, raw
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "schema_version" in data:
            if data["schema_version"] != 1:
                raise ValueError("Unsupported settings schema")
            data = data["settings"]
        if not isinstance(data, dict):
            raise ValueError("Settings are not an object")
        return data, revision, raw
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=503, detail="Saved settings are corrupt; the original file has not been changed. Restore a valid settings.json (settings.backup.json if available) before saving.")


@app.get("/settings")
async def get_settings():
    settings, revision, _ = _read_settings_document()
    return {"settings": settings, "revision": revision, "schema_version": 1}


@app.put("/settings")
async def put_settings(request: Request, settings: dict = Body(...)):
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
            if isinstance(value, float) and not math.isfinite(value):
                raise HTTPException(status_code=400, detail="Settings numbers must be finite.")
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

    if "user_presets" in clean:
        try:
            presets = json.loads(clean["user_presets"])
            if not isinstance(presets, dict) or len(presets) > 10:
                raise ValueError()
            for name, values in presets.items():
                if not isinstance(name, str) or not name.strip() or len(name) > 40 or not isinstance(values, dict):
                    raise ValueError()
                for key, value in values.items():
                    valid_type = type(value) is bool if key in SETTINGS_CHECKBOX_KEYS else (isinstance(value, (str, int, float)) and not isinstance(value, bool))
                    if key not in SETTINGS_PRESET_KEYS or not valid_type or (isinstance(value, float) and not math.isfinite(value)):
                        raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid user presets; use known Combine controls and valid scalar values.")
    _, revision, previous = _read_settings_document()
    expected = request.headers.get("if-match")
    if expected is None:
        raise HTTPException(status_code=428, detail="Read settings first and send their revision in If-Match.")
    if expected != revision:
        raise HTTPException(status_code=409, detail="Settings changed in another tab. Reload the newer settings before saving.")
    payload = json.dumps({"schema_version": 1, "settings": clean}, allow_nan=False)
    if len(payload.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise HTTPException(status_code=400, detail="Settings payload too large.")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if previous:
        backup = SETTINGS_FILE.with_name("settings.backup.json")
        backup_tmp = backup.with_suffix(".tmp")
        backup_tmp.write_bytes(previous)
        backup_tmp.replace(backup)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload)
    tmp.replace(SETTINGS_FILE)
    return {"saved": True, "revision": hashlib.sha256(payload.encode()).hexdigest()}


# ─────────────────────────────────────────────
# Saved files listing and deletion
# ─────────────────────────────────────────────

def _reject_reserved_output_name(stem: str) -> None:
    """Keep the temporary prefix out of user-chosen kept names. The registry
    already makes such a file safe from sweeping, but allowing the name would
    leave an output that reads as disposable and could collide with a real
    temporary; all three producers call this on the same boundary."""
    if stem.startswith(TMP_OUTPUT_PREFIX):
        raise HTTPException(
            status_code=400,
            detail=f"Output name cannot start with '{TMP_OUTPUT_PREFIX}' (reserved).",
        )


def _read_temp_registry() -> dict:
    """Load the not-kept output registry. A missing or unreadable file means
    nothing is owned, so nothing can be swept — the safe direction."""
    try:
        with _open_output(TEMP_REGISTRY_FILE.name, directory=OUTPUT_DIR) as handle:
            data = json.load(handle)
    except (HTTPException, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: meta for name, meta in data.items()
            if sanitize_filename(name) == name and isinstance(meta, dict)
            and isinstance(meta.get("expires", 0), (int, float))}


def _write_temp_registry(entries: dict) -> None:
    """Atomic temp-then-replace, mirroring _write_recipe. Best-effort: a failed
    write leaves the previous registry, which at worst delays an expiry."""
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_output_text(TEMP_REGISTRY_FILE.name, json.dumps(entries))
    except OSError:
        pass


def _register_temp_output(output_name: str, ttl_s: int = TEMP_OUTPUT_TTL_S) -> None:
    """Claim ownership of a not-kept output. Only a registered file is ever
    swept or accepted by the release endpoint, so this is the single point that
    decides an output is disposable."""
    entries = _read_temp_registry()
    entries[output_name] = {"expires": time.time() + ttl_s}
    _write_temp_registry(entries)


def _remove_output_artifacts(output_name: str) -> None:
    """Drop the sidecar recipe and any cached thumbnails belonging to an output,
    so deleting it does not leave entries behind. output_name is already
    sanitized, so it carries no glob metacharacters."""
    try:
        _unlink_output(output_name + ".json", ".recipes")
        for thumb in THUMB_DIR.glob(f"{output_name}.*"):
            thumb.unlink(missing_ok=True)
    except OSError:
        pass


def _discard_temp_output(output_name: str) -> bool:
    """Delete one registered temporary output and its artifacts. Returns False
    when the file resists deletion, so the caller keeps its registry entry and
    retries on the next sweep."""
    try:
        if not _unlink_output(output_name):
            return False
    except OSError:
        return False  # still being written or read — next sweep tries again
    _remove_output_artifacts(output_name)
    return True


def _sweep_tmp_outputs() -> None:
    """Expire not-kept outputs the client never released. Only registered files
    are considered, and only against their own recorded expiry — a file's name
    and mtime grant no permission to delete it, so a kept output can never be
    swept. Runs at startup and before each /files listing."""
    entries = _read_temp_registry()
    if not entries:
        return
    now = time.time()
    surviving = {}
    for name, meta in entries.items():
        expires = meta.get("expires", 0) if isinstance(meta, dict) else 0
        if not _regular_output(name):
            if not _discard_temp_output(name):
                surviving[name] = meta  # non-regular entries are never removed
            continue
        if now < expires:
            surviving[name] = meta
        elif not _discard_temp_output(name):
            surviving[name] = meta
    if len(surviving) != len(entries):
        _write_temp_registry(surviving)


def _log_unowned_legacy_outputs() -> None:
    """Report, once per start, how many prefix-named outputs predate the
    registry. They are deliberately never swept — an old name is not evidence
    that a file is disposable — so they now simply appear in My Files for the
    user to keep or delete. Counts only; no filenames or media contents."""
    try:
        owned = set(_read_temp_registry())
        legacy = sum(1 for f in OUTPUT_DIR.glob(f"{TMP_OUTPUT_PREFIX}*")
                     if sanitize_filename(f.name) == f.name and _regular_output(f.name) and f.name not in owned)
    except OSError:
        return
    if legacy:
        print(f"[magicflow] {legacy} output(s) carry the reserved "
              f"'{TMP_OUTPUT_PREFIX}' prefix but are not registered as temporary; "
              f"leaving them in place and listing them in My Files.", flush=True)


def _clear_stale_staging() -> None:
    """Drop staging files an interrupted job left behind. Safe to run at startup:
    the app serves a single worker (Dockerfile: --workers 1), so no encode can be
    in flight while it boots. An unpublished staging file has no identity in the
    library, so nothing the user can see is affected."""
    root = child = None
    try:
        root = os.open(OUTPUT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        child = os.open(".staging", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        for name in os.listdir(child):
            if stat_module.S_ISREG(os.stat(name, dir_fd=child, follow_symlinks=False).st_mode):
                os.unlink(name, dir_fd=child)
    except OSError:
        pass
    finally:
        if child is not None:
            os.close(child)
        if root is not None:
            os.close(root)


def _read_output_dims(path: Path) -> tuple[Optional[int], Optional[int]]:
    """First-frame pixel dimensions of a produced output, or (None, None) when
    they can't be read (e.g. some video containers). Best-effort — recipes are a
    convenience, never a hard dependency."""
    try:
        if path.suffix.lower() in VIDEO_EXTENSIONS or is_apng(path):
            _, width, height = _probe_video(str(path))
            return width or None, height or None
        r = _run_subprocess(["identify", "-format", "%w %h", f"{path}[0]"],
                            PROBE_TIMEOUT_S)
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None, None


def _recipe_timing(path: Path, fmt: str, frames: int) -> tuple[str, list[float]]:
    """Measure output timing for provenance, independently of encoder inputs.

    Video records only the measurable intervals between frames, never an
    invented final hold. A failed probe is explicitly unknown.
    """
    if fmt not in {"gif", "webp", "apng", "mp4", "webm"}:
        return "not_applicable", []
    try:
        if fmt in {"gif", "webp"}:
            result = _run_subprocess(["identify", "-format", "%T\n", str(path)], PROBE_TIMEOUT_S)
            delays = [int(value) * 10 for value in result.stdout.split()]
            if result.returncode == 0 and len(delays) == frames:
                return "frame_durations", delays
        else:
            result = _run_subprocess([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time",
                "-of", "json", str(path)], PROBE_TIMEOUT_S)
            rows = json.loads(result.stdout).get("frames", [])
            if result.returncode == 0 and len(rows) == frames:
                if fmt == "apng":
                    return "frame_durations", [round(float(row["pkt_duration_time"]) * 1000, 3)
                                               for row in rows]
                stamps = [float(row["best_effort_timestamp_time"]) for row in rows]
                return "frame_intervals", [round((b - a) * 1000, 3)
                                           for a, b in zip(stamps, stamps[1:])]
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        pass
    return "unavailable", []


def _output_color_metadata(path: Path) -> dict:
    """Measured first-frame/sample encoding facts, never inferred source fidelity."""
    try:
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            result = _run_subprocess(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt,color_space,color_transfer,color_primaries,color_range", "-of", "json", str(path)], PROBE_TIMEOUT_S)
            streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
            return streams[0] if streams else {}
        result = _run_subprocess(["identify", "-format", "%[colorspace]|%z|%[channels]", f"{path}[0]"], PROBE_TIMEOUT_S)
        fields = result.stdout.strip().split("|")
        if result.returncode == 0 and len(fields) == 3:
            return {"colorspace": fields[0], "depth": int(fields[1]), "channels": fields[2]}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass
    return {}


def _write_recipe(output_name: str, direction: str, params: dict,
                  sources: list[str], frames: int,
                  width: Optional[int], height: Optional[int], *,
                  encoder_delays_ms: Optional[list[int]] = None,
                  legacy_delays_ms: Optional[list[int]] = None,
                  timing_mode: str = "not_applicable") -> None:
    """Versioned, read-only history of work already baked into this output.

    Requested geometry must never be replaced by measurements. Flat aliases
    remain for existing readers; v2 consumers use requested/effective instead.
    Source names are provenance, not retained uploads or replay instructions.
    Writes remain best-effort so a sidecar failure cannot lose a finished file.
    """
    try:
        RECIPE_DIR.mkdir(parents=True, exist_ok=True)
        fmt = params["format"]
        basis, measured = _recipe_timing(OUTPUT_DIR / output_name, fmt, frames)
        encoder = {"mp4": "libx264", "webm": "libvpx-vp9", "apng": "apng"}.get(fmt, "ImageMagick")
        effective = {
            "format": fmt, "width": width, "height": height, "frames": frames,
            "processing_policy": PROCESSING_POLICY,
            "color": _output_color_metadata(OUTPUT_DIR / output_name),
            "encoder": encoder,
            # APNG doesn't use quality; video maps it to CRF, not -quality.
            "quality": params["quality"] if encoder == "ImageMagick" else None,
            "timing": {"mode": timing_mode if basis != "not_applicable" else "not_applicable",
                       "basis": basis,
                       "encoder_delays_ms": (encoder_delays_ms or []) if basis != "not_applicable" else [],
                       "measured_delays_ms": measured},
        }
        if fmt in VIDEO_OUTPUT_FORMATS:
            effective["video_crf"] = video_crf_for_quality(fmt, params["quality"])
        warnings = []
        if fmt == "apng":
            warnings.append("APNG does not use the requested quality setting.")
        if fmt in VIDEO_OUTPUT_FORMATS:
            warnings.append("Video timing lists frame intervals; the final frame hold is not verified.")
        if timing_mode == "fallback_uniform":
            warnings.append("Source timing could not be read; the requested uniform delay was used.")
        if params.get("delays") and timing_mode == "uniform":
            warnings.append("The per-input delay list was unusable; the uniform delay was used.")
        if basis == "unavailable":
            warnings.append("Output timing could not be measured; encoder timing is not a measurement.")
        if encoder_delays_ms and basis == "frame_durations" and measured != encoder_delays_ms:
            warnings.append("The output timing differs from the encoder timeline (quantization or frame merging).")
        if timing_mode != "source" and basis != "not_applicable" and "delay" in params and (params["delay"] % 10 or any(
                value.strip().isdigit() and int(value.strip()) % 10
                for value in (params.get("delays") or "").split(","))):
            warnings.append("Requested delays were rounded to centiseconds before encoding.")
        if "pdf_layout" in params:
            effective["pdf_layout"] = params["pdf_layout"]
            effective["quality"] = None  # fixed pages use lossless ZIP compression
        recipe = {
            **params,
            "schema_version": 2,
            "direction": direction,
            "sources": [sanitize_filename(s) for s in sources][:MAX_FILES],
            "provenance": {"source_kind": "saved_output" if direction == "reconvert" else "uploads",
                           "original_uploads_retained": False},
            "requested": dict(params),
            "effective": effective,
            "warnings": warnings,
            "frames": frames,
            # Legacy aliases retain their old meaning; no reader should use
            # these baked transforms as the next job's defaults.
            "width": width,
            "height": height,
            **({"video_crf": effective["video_crf"]} if "video_crf" in effective else {}),
            **({"effective_delays_ms": legacy_delays_ms} if legacy_delays_ms is not None else {}),
        }
        payload = json.dumps(recipe)
        _atomic_output_text(output_name + ".json", payload, ".recipes")
    except Exception:
        pass


@app.get("/files")
async def list_files():
    if not OUTPUT_DIR.exists():
        return {"files": []}
    _sweep_tmp_outputs()
    # Hide only outputs this server owns as temporary. A file merely *named*
    # like one — a legacy temporary, or a kept file from before the naming
    # guard — stays listed so the user can see and manage it.
    temp_names = set(_read_temp_registry())
    files = []
    for f in OUTPUT_DIR.iterdir():
        if f.suffix.lower() in OUTPUT_EXTENSIONS and sanitize_filename(f.name) == f.name and f.name not in temp_names and _regular_output(f.name):
            with _open_output(f.name) as handle:
                stat = os.fstat(handle.fileno())
            # Attach the sidecar recipe when present; a missing/corrupt one is a
            # non-event (older files predate recipes) → recipe: null, defaults hold.
            recipe = None
            recipe_path = RECIPE_DIR / (f.name + ".json")
            if recipe_path.is_file():
                try:
                    with _open_output(f.name + ".json", ".recipes") as handle:
                        recipe = json.load(handle)
                    if not isinstance(recipe, dict):
                        recipe = None
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
    if not _regular_output(safe):
        raise HTTPException(status_code=404, detail="File not found.")
    if not _unlink_output(safe):
        raise HTTPException(status_code=404, detail="Regular file not found.")
    # Remove the sidecar recipe alongside its file (no-op if it never had one).
    try:
        _unlink_output(safe + ".json", ".recipes")
    except OSError:
        pass
    return {"deleted": safe}


@app.delete("/files/temp/{filename}")
async def release_temp_file(filename: str):
    """Discard a not-kept output once the client has it. Deliberately separate
    from delete_file: it accepts ONLY outputs this server registered as
    temporary, so the cleanup step that follows a download can never be aimed
    at a file the user chose to keep. Un-released temporaries still expire via
    _sweep_tmp_outputs, so a client that never calls this leaks nothing."""
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    entries = _read_temp_registry()
    if safe not in entries:
        raise HTTPException(status_code=403, detail="Not a temporary output.")
    if not _regular_output(safe):
        raise HTTPException(status_code=404, detail="File not found.")
    if not _discard_temp_output(safe):
        raise HTTPException(status_code=500, detail="Could not release the file.")
    entries.pop(safe, None)
    _write_temp_registry(entries)
    return {"released": safe}


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
        if _regular_output(name):
            try:
                if _unlink_output(name):
                    deleted.append(name)
                    try:
                        _unlink_output(name + ".json", ".recipes")
                    except OSError:
                        pass
                else:
                    missing.append(name)
            except OSError:
                missing.append(name)  # vanished mid-batch — treat as missing
        else:
            missing.append(name)
    return {"deleted": deleted, "missing": missing}


@app.post("/files/archive")
def archive_files(payload: dict = Body(...)):
    """Stream a ZIP of the selected outputs. The archive is built on the scratch
    volume and streamed from there (see _zip_response), so its memory cost does
    not scale with the selection."""
    names = _validate_filename_batch(payload)
    paths = [OUTPUT_DIR / name for name in names]
    paths = [path for path in paths if _regular_output(path.name)]
    if not paths:
        raise HTTPException(status_code=404, detail="No matching files to archive.")
    return _zip_response(paths, "magicflow_files.zip")


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
@with_job_slot
def convert(
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
    pdf_page_size: str = Form("original"),
    pdf_orientation: str = Form("portrait"),
    pdf_width_mm: float = Form(210),
    pdf_height_mm: float = Form(297),
    pdf_margin_mm: float = Form(10),
    pdf_fit: str = Form("fit"),
):
    # ── Input validation ──────────────────────
    fmt = format.lower().strip()
    if fmt not in ALLOWED_OUTPUT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format must be one of: {', '.join(_supported('animation_outputs', ALLOWED_OUTPUT_FORMATS))}.")
    _reject_unavailable_format("animation_outputs", fmt, ALLOWED_OUTPUT_FORMATS)

    pdf_params = dict(pdf_page_size=pdf_page_size, pdf_orientation=pdf_orientation,
                      pdf_width_mm=pdf_width_mm, pdf_height_mm=pdf_height_mm,
                      pdf_margin_mm=pdf_margin_mm, pdf_fit=pdf_fit) if fmt == "pdf" else {}
    try:
        pdf_layout = resolve_layout(pdf_page_size, pdf_orientation, pdf_width_mm,
                                    pdf_height_mm, pdf_margin_mm, pdf_fit) if fmt == "pdf" else None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

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
    # so it is safe as a dict key. Otherwise generate one.
    if job_id is not None:
        try:
            job_id = str(uuid.UUID(job_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid job_id.")
    else:
        job_id = str(uuid.uuid4())
    # Storage identity is the server's, never the client's: job_id is a progress
    # correlation token only. Two requests may legitimately carry the same one
    # (a retry, a re-submitted form), and letting it name the work directories
    # and the default output made them share a work dir and overwrite each
    # other's result.
    storage_id = str(uuid.uuid4())
    job_input = INPUT_DIR / storage_id
    job_tmp = TMP_DIR / storage_id

    # Map format to file extension
    ext_map = {"gif": ".gif", "webp": ".webp", "apng": ".apng", "tiff": ".tiff",
               "pdf": ".pdf", "mp4": ".mp4", "webm": ".webm"}
    out_ext = ext_map[fmt]
    staging_path = STAGING_DIR / f"{storage_id}{out_ext}"
    staging_fd = None

    try:
        job_input.mkdir(parents=True, exist_ok=True)
        job_tmp.mkdir(parents=True, exist_ok=True)
        staging_path, staging_fd = _pinned_staging_path(staging_path.name)

        frame_paths: list[str] = []

        _require_upload_quota(files)

        for i, upload in enumerate(files):
            content = _read_upload(upload, f"Frame {i + 1} ('{upload.filename}')")

            ok, msg = validate_file(upload.filename or "", upload.content_type or "")
            if not ok:
                raise HTTPException(status_code=400, detail=f"Frame {i + 1}: {msg}")

            safe_name = sanitize_filename(upload.filename or f"frame_{i:03d}")
            dest = job_input / f"{i:03d}_{safe_name}"
            dest.write_bytes(content)
            frame_paths.append(str(dest))

        # ── Decompression-bomb guard (mirror of /explode's, app-wide invariant):
        # multi-frame inputs (GIF/WebP/TIFF/PSD…) expand into all their sub-frames
        # inside the convert command, so cap the TOTAL expanded frame count — and
        # the pixels those frames decode to, which the byte limits do not bound.
        # One metadata read per input answers both; formats that cannot expand
        # still get probed, because a single still can be the bomb.
        expanded_total = 0
        multi_frame_inputs = 0
        decoded_pixels = 0
        for i, p in enumerate(frame_paths):
            frames, source_width, source_height = _probe_frames_and_size(Path(p))
            _require_measurable(source_width, source_height, f"Frame {i + 1}")
            _require_within_dimension_budget(source_width, source_height, f"Frame {i + 1}")
            if Path(p).suffix.lower() in SINGLE_FRAME_EXTENSIONS:
                n = 1
            else:
                n = frames or 1  # unknown → 1; ImageMagick limits still apply
                if n > 1:
                    multi_frame_inputs += 1
            expanded_total += n
            decoded_pixels += source_width * source_height * n
        _require_within_pixel_budget(decoded_pixels)
        effective_total = len(_boomerang_indices(expanded_total)) if boomerang else expanded_total
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

        # The name the output will claim once it is complete. Collisions are not
        # checked here: _publish_output reserves a free name atomically, so there
        # is no check-then-write window and no overwrite.
        if keep_output:
            output_name = f"magicflow_{storage_id}{out_ext}"
            if custom_name:
                safe_custom = sanitize_filename(custom_name)
                safe_stem = safe_custom.rsplit(".", 1)[0] if "." in safe_custom else safe_custom
                _reject_reserved_output_name(safe_stem)
                if safe_stem:
                    output_name = f"{safe_stem}{out_ext}"
        else:
            # Not kept: registered as temporary once produced, so it is hidden
            # from /files, released by the client after download, and expired by
            # _sweep_tmp_outputs otherwise. Custom naming is meaningless for a
            # file that must vanish.
            output_name = f"{TMP_OUTPUT_PREFIX}{storage_id}{out_ext}"

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

        # Boomerang / ping-pong plays forward then backward (1,2,3 → 1,2,3,2).
        # The frames are mirrored during preparation, once each input has been
        # expanded — mirroring the input FILES here left a single animated file
        # unmirrored however many frames it held. Only the delays are mirrored
        # here, because per-frame delays address input files (a job that mixes
        # them with an animated input is rejected above), so the two lists stay
        # the same length.
        if boomerang and frame_delays is not None:
            frame_delays = [frame_delays[i] for i in _boomerang_indices(len(frame_delays))]

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
        _set_job(job_id, JOB_PHASE_TRANSFORMING, 0, effective_total)
        if fmt == "apng":
            _encode_apng(frame_paths, str(staging_path), delay_cs, loop, common, job_tmp, run_env,
                         job_id, effective_total, boomerang=boomerang,
                         frame_delays=frame_delays)
        elif fmt in ("mp4", "webm"):
            _encode_video(frame_paths, str(staging_path), delay_cs, fmt, common, job_tmp, run_env,
                          job_id, effective_total, quality=quality, boomerang=boomerang,
                          frame_delays=frame_delays)
        else:
            prep_pattern = _prep_frames(frame_paths, common, job_tmp, run_env, "Conversion",
                                        job_id, effective_total, boomerang=boomerang)
            _set_job(job_id, JOB_PHASE_ENCODING, effective_total, effective_total)
            prep_files = [str(p) for p in sorted(job_tmp.glob("prep_*.png"),
                                                 key=_frame_order_key)]
            if pdf_layout:
                prep_files = _layout_pdf_pages(prep_files, pdf_layout, job_tmp, run_env)
            cmd = build_pdf_command(prep_files, staging_path) if pdf_layout else build_assembly_command(
                prep_files, str(staging_path), fmt, delay_cs, loop, quality,
                colors=colors, dither=dither, optimize_layers=optimize_layers,
                frame_delays=frame_delays,
            )
            _run_or_500(cmd, job_tmp, run_env, "Conversion failed")
            _require_output(staging_path, "Conversion")

        # Validated — give it an identity. Everything below reports the name the
        # output actually got, which differs from the requested one when that was
        # already taken.
        output_name = _publish_output(staging_path, output_name, storage_id)
        output_path = OUTPUT_DIR / output_name

        # What the output actually contains, not the pre-flight estimate: an
        # estimate that disagreed with the file is how a boomerang that never
        # happened still reported four frames.
        produced_frames = _probe_frames_and_size(output_path)[0] or effective_total
        timeline_ms = _effective_timeline_ms(output_path, delay_cs, frame_delays)

        # Sidecar recipe for kept outputs (F14).
        if keep_output:
            out_w, out_h = _read_output_dims(output_path)
            _write_recipe(
                output_name, "convert",
                params={
                    "format": fmt, "delay": delay, "loop": loop,
                    "width": width, "height": height, "delays": delays,
                    "resize_mode": resize_mode, "quality": quality,
                    "boomerang": boomerang, "rotate": rotate, "flip": flip, "flop": flop,
                    "brightness": brightness, "saturation": saturation, "hue": hue,
                    "grayscale": grayscale, "negate": negate, "sepia": sepia,
                    "auto_level": auto_level, "normalize": normalize,
                    "sharpen": sharpen, "blur": blur,
                    "colors": colors, "dither": dither, "optimize_layers": optimize_layers,
                    "strip": strip, "background": background, **pdf_params,
                    **({"pdf_layout": pdf_layout.facts()} if pdf_layout else {}),
                },
                sources=[u.filename or "" for u in files],
                frames=produced_frames, width=out_w, height=out_h,
                encoder_delays_ms=([d * 10 for d in frame_delays] if frame_delays is not None
                                   else [delay_cs * 10] * effective_total),
                legacy_delays_ms=timeline_ms,
                timing_mode="per_input" if frame_delays is not None else "uniform",
            )
        else:
            _register_temp_output(output_name)

        return JSONResponse({
            **_result_payload(output_name, output_path, keep_output),
            "frame_count": produced_frames,
            "effective_delays_ms": timeline_ms,
        })

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Conversion timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        # Covers every exit including a timeout: an unpublished encode leaves
        # nothing behind, published or not.
        if staging_fd is not None:
            try:
                staging_path.unlink(missing_ok=True)
            finally:
                os.close(staging_fd)
        shutil.rmtree(job_input, ignore_errors=True)
        shutil.rmtree(job_tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Explode (animation / multi-frame image → ZIP of still frames)
# ─────────────────────────────────────────────

def _run_or_500(cmd: list[str], job_tmp: Path, run_env: dict, label: str):
    """Run one step of a job, raising HTTP 500 with stderr detail on failure.
    The step's timeout is whatever the job has left, so many steps cannot add up
    past the job's budget (see _step_timeout)."""
    r = _run_subprocess(cmd, _step_timeout(), cwd=str(job_tmp), env=run_env)
    if r.returncode != 0:
        detail = r.stderr.strip() or r.stdout.strip() or "Unknown error."
        raise HTTPException(status_code=500, detail=f"{label}: {detail}")
    return r


def _output_is_readable(path: Path) -> bool:
    """Decode-probe a finished encode, so a truncated-but-nonzero file cannot be
    published. Only the first frame is read, so the cost is one header parse
    whatever the frame count. Fails OPEN when the probe itself cannot run: an
    absent or timing-out `identify` says nothing about the output, and the size
    check still applies. Separate from _read_output_dims, which is best-effort
    metadata and cannot distinguish "unreadable file" from "no probe"."""
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return True  # ffmpeg's own exit status is the only signal available
    try:
        probe = _run_subprocess(["identify", "-format", "%w", f"{path}[0]"],
                                PROBE_TIMEOUT_S)
    except Exception:
        return True
    return probe.returncode == 0 and probe.stdout.strip().isdigit()


def _require_output(output_path, label: str) -> None:
    """Encoders can exit 0 yet write nothing (see ANIMATED_WEBP_PROFILE_STRIP) or
    leave a partial file. A missing, 0-byte or undecodable output is deleted and
    reported as a 500, so it can never be published to My Files as if it had
    succeeded. Runs on the staging file, before the output has any identity."""
    path = Path(output_path)
    if not (path.is_file() and path.stat().st_size > 0):
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"{label} produced no output file.")
    if not _output_is_readable(path):
        path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"{label} produced an unreadable output file.")


# A free name is found on the first or second try in practice; the bound only
# keeps a pathological directory from spinning.
MAX_PUBLISH_ATTEMPTS = 20


def _publish_output(staging_path: Path, preferred_name: str, storage_id: str) -> str:
    """Move a validated staging file into the library under the first free name,
    and return that name.

    os.link gives an atomic exclusive create: a name already taken raises
    FileExistsError instead of being overwritten, so neither a repeated job nor a
    reused custom name can ever replace earlier work — there is no gap between
    the existence check and the write for a second request to slip into. Both
    paths are on the same filesystem by construction (STAGING_DIR)."""
    stem = Path(preferred_name).stem
    ext = Path(preferred_name).suffix
    candidates = [preferred_name, f"{stem}_{storage_id[:8]}{ext}"]
    candidates += [f"{stem}_{storage_id[:8]}_{n}{ext}"
                   for n in range(2, MAX_PUBLISH_ATTEMPTS)]
    root = os.open(OUTPUT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in candidates:
            if Path(name).name != name or name in (".", ".."):
                raise HTTPException(status_code=400, detail="Invalid publication name.")
            if not stat_module.S_ISREG(os.stat(staging_path, follow_symlinks=False).st_mode):
                raise HTTPException(status_code=500, detail="Staging output changed; retry.")
            try:
                os.link(staging_path, name, dst_dir_fd=root, follow_symlinks=False)
            except FileExistsError:
                continue
            if not stat_module.S_ISREG(os.stat(name, dir_fd=root, follow_symlinks=False).st_mode):
                os.unlink(name, dir_fd=root)
                raise HTTPException(status_code=500, detail="Staging output changed; retry.")
            staging_path.unlink(missing_ok=True)
            return name
        raise HTTPException(status_code=500, detail="Could not find a free output name.")
    finally:
        os.close(root)


def _result_payload(output_name: str, output_path: Path, keep_output: bool) -> dict:
    """The one result contract every producer returns, so a single client path
    can finish any job: what the output is called, where to fetch it, how big it
    is, and — for a temporary — both that the client should release it once the
    bytes are delivered and how long it survives if that never happens. Without
    the expiry the client can only promise deletion on download, which is half
    the lifecycle and makes a failed download look like lost work."""
    return {
        "output_filename": output_name,
        "download_url": f"/download/{output_name}",
        "delete_after_download": not keep_output,
        "file_size": output_path.stat().st_size,
        "expires_in": None if keep_output else TEMP_OUTPUT_TTL_S,
    }


def _needs_canonical_decode(source: Path) -> bool:
    """Whether a source must be decoded before it can be transformed.

    A plain still is already a full canvas and goes straight through, which
    keeps the common case at one ImageMagick call per input. Anything that can
    carry delta frames, layers, or animation ImageMagick will not see (an APNG
    under either extension) is decoded first."""
    extension = source.suffix.lower()
    if extension == ".avif":
        return True  # libavif preserves alpha that the IM6 HEIC reader loses
    if extension in SINGLE_FRAME_EXTENSIONS:
        return False
    if extension in LAYERED_EXTENSIONS or is_apng(source):
        return True
    return _probe_source(str(source))[0] > 1


def _canonical_frames(source: Path, index: int, job_tmp: Path, run_env: dict) -> list[str]:
    """One input as full-canvas frames, ready to be transformed.

    This is the stage Combine was missing. It used to hand the raw source to
    the transform command, which saw one frame of an APNG, and scaled a GIF's
    delta rectangles as though each were a whole picture."""
    if not _needs_canonical_decode(source):
        return [str(source)]
    frames = _extract_frames(source, job_tmp, run_env, prefix=f"src{index:05d}")
    return sorted(frames, key=_frame_order_key)


def _boomerang_indices(count: int) -> list[int]:
    """Positions that play a sequence forward then back: 1,2,3 → 1,2,3,2.

    The endpoints are not repeated, so the motion reverses rather than
    stuttering. Fewer than three frames have no middle to mirror."""
    if count < 3:
        return list(range(count))
    return list(range(count)) + list(range(count - 2, 0, -1))


def _apply_boomerang(job_tmp: Path) -> int:
    """Mirror the prepared sequence in place and return its new length.

    Applied here, to the frames that actually exist, rather than to the input
    files: one animated file is a single input but many frames, so mirroring
    the inputs left such a job unmirrored while the frame count said otherwise."""
    frames = sorted(job_tmp.glob("prep_*.png"), key=_frame_order_key)
    indices = _boomerang_indices(len(frames))
    for position, source in enumerate(indices[len(frames):], start=len(frames)):
        shutil.copy2(frames[source], job_tmp / f"prep_{position:05d}.png")
    return len(indices)


def _prepared_png_info(path: Path) -> tuple[int, int, int, int]:
    """Read dimensions, bit depth and color type from a prepared PNG's IHDR.

    These are our generated files. Header reads avoid another media process
    per frame just to validate the encoder input sequence.
    """
    with path.open("rb") as handle:
        header = handle.read(26)
    if len(header) != 26 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise HTTPException(status_code=500, detail="A prepared frame is not a readable PNG; encoding stopped.")
    width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    if not width or not height:
        raise HTTPException(status_code=500, detail="A prepared frame has no usable dimensions; encoding stopped.")
    return width, height, header[24], header[25]


def _normalize_animation_canvas(job_tmp: Path, fmt: str, background: str, run_env: dict) -> None:
    """Give timed outputs one canvas after rotation/resize and before encoding.

    Fit keeps its bounding-box/shrink-only meaning. Padding to the maximum
    prepared width/height never enlarges or crops the content. Documents keep
    independent pages and never enter this stage.
    """
    if fmt not in ANIMATION_CANVAS_FORMATS:
        return
    frames = sorted(job_tmp.glob("prep_*.png"), key=_frame_order_key)
    if not frames:
        raise HTTPException(status_code=500, detail="No prepared frames to place on a canvas.")
    infos = [_prepared_png_info(path) for path in frames]
    width, height = max(info[0] for info in infos), max(info[1] for info in infos)
    canvas = (width + width % 2, height + height % 2) if fmt == "mp4" else (width, height)
    _require_within_dimension_budget(*canvas, "Animation canvas")
    _require_within_pixel_budget(canvas[0] * canvas[1] * len(frames))
    # 8-bit RGB for opaque MP4; 8-bit RGBA for APNG/VP9. ImageMagick's GIF/WebP
    # assembler can consume different PNG color types, but still needs one size.
    pixel_type = (8, 2 if fmt == "mp4" else 6)
    fixed_pixels = fmt in FFMPEG_FRAME_CODERS
    for path, info in zip(frames, infos):
        if info[:2] != canvas or (fixed_pixels and info[2:] != pixel_type):
            normalized = job_tmp / "canvas.png"
            cmd = build_canvas_command(str(path), str(normalized), width, height, fmt, background)
            _run_or_500(cmd, job_tmp, run_env, "Could not place a frame on the animation canvas")
            normalized.replace(path)
        actual = _prepared_png_info(path)
        if actual[:2] != canvas or (fixed_pixels and actual[2:] != pixel_type):
            raise HTTPException(status_code=500, detail=(
                "Prepared frames have incompatible dimensions or pixel formats; "
                "encoding stopped to avoid damaged or missing frames."))


def _prep_frames(frame_paths, common_kwargs, job_tmp, run_env, label,
                 job_id=None, progress_total=0, boomerang=False):
    """Phase 1 of the two-phase pipeline: transform each INPUT into PNG frames,
    one ImageMagick call per input, then renumber everything into one contiguous
    sequence prep_00000.png, prep_00001.png…. Per-input processing is what makes
    live progress possible (a counter ticks per input). Animation frames then
    share one validated canvas; document pages retain their individual sizes.
    Returns the numbered-frame input pattern (prep_%05d.png).

    Each input writes to its own tmpNNNNN_%05d.png so a multi-frame input (a GIF
    dropped into Combine) can expand without ever colliding with another input;
    the final rename gives ffmpeg / the assembler a clean 0-based sequence.

    Reports the transforming phase per input, with the frame count produced so
    far, so /progress tracks real expansion rather than an estimate."""
    n_inputs = len(frame_paths)
    for i, fp in enumerate(frame_paths):
        per_input_pattern = str(job_tmp / f"tmp{i:05d}_{FRAME_NUMBER_PATTERN}.png")
        sources = _canonical_frames(Path(fp), i, job_tmp, run_env)
        cmd = build_command(
            frames=sources, output=per_input_pattern, frames_pattern=per_input_pattern, **common_kwargs,
        )
        _run_or_500(cmd, job_tmp, run_env, f"{label} frame preparation failed")
        produced = len(list(job_tmp.glob("tmp*_*.png")))
        if produced > MAX_EXPLODE_FRAMES:
            raise HTTPException(status_code=400, detail=f"Too many frames ({produced}).")
        _set_job(job_id, JOB_PHASE_TRANSFORMING, produced,
                 max(progress_total, produced, n_inputs))  # tracks real expansion

    # Renumber the per-input frames into one contiguous 0-based sequence. Sorting
    # by name keeps global order: the zero-padded outer index dominates, the
    # inner frame index orders an expanded input's own frames.
    tmp_frames = sorted(job_tmp.glob("tmp*_*.png"), key=_frame_order_key)
    if not tmp_frames:
        raise HTTPException(status_code=500, detail=f"{label} frame preparation produced no frames.")
    for seq, src in enumerate(tmp_frames):
        src.rename(job_tmp / f"prep_{seq:05d}.png")
    if boomerang:
        _apply_boomerang(job_tmp)
    _normalize_animation_canvas(job_tmp, common_kwargs["fmt"], common_kwargs.get("background", ""), run_env)
    return str(job_tmp / f"prep_{FRAME_NUMBER_PATTERN}.png")


def _effective_timeline_ms(output_path: Path, delay_cs: int,
                           frame_delays: Optional[list[int]]) -> list[int]:
    """The per-frame timing the produced file actually carries, in milliseconds.

    Reported rather than assumed: a container quantizes to its own timebase
    (GIF counts in centiseconds), and the caller's request is not evidence of
    what was written. An empty list means the timing could not be read, which
    is not the same as uniform."""
    frames = _probe_frames_and_size(output_path)[0]
    if not frames:
        return []
    if frame_delays and len(frame_delays) == frames:
        return [max(1, delay) * 10 for delay in frame_delays]
    return [max(1, delay_cs) * 10] * frames


def _write_timeline(job_tmp: Path, delays_cs: Optional[list[int]]) -> Optional[str]:
    """Write the per-frame timeline ffmpeg will read, or None for uniform timing.

    The delays must line up with the frames that exist after preparation —
    expansion and boomerang included — so this is called once those frames are
    on disk, never from the request's own frame list."""
    if not delays_cs:
        return None
    frames = [str(p) for p in sorted(job_tmp.glob("prep_*.png"), key=_frame_order_key)]
    if len(frames) != len(delays_cs):
        return None  # a timeline that cannot address these frames is not used
    timeline = job_tmp / CONCAT_LIST_NAME
    timeline.write_text(build_concat_list(frames, delays_cs))
    return str(timeline)


def _encode_apng(frame_paths, output_path, delay_cs, loop, common_kwargs, job_tmp, run_env,
                 job_id=None, progress_total=0, boomerang=False, frame_delays=None):
    """Produce a clean APNG: transform frames to numbered PNGs with ImageMagick,
    then assemble them with ffmpeg (one APNG frame per PNG). Per-frame delays
    are carried through as real durations — APNG stores a delay per frame."""
    prep_pattern = _prep_frames(frame_paths, common_kwargs, job_tmp, run_env, "APNG",
                                job_id, progress_total, boomerang=boomerang)
    _set_job(job_id, JOB_PHASE_ENCODING, progress_total, progress_total)
    timeline = _write_timeline(job_tmp, frame_delays)
    cmd_enc = build_apng_encode_command(
        prep_pattern, str(output_path), delay_cs, loop, concat_file=timeline,
        final_delay_cs=(frame_delays[-1] if timeline and frame_delays else None))
    _run_or_500(cmd_enc, job_tmp, run_env, "APNG encoding failed")
    _require_output(output_path, "APNG encoding")


def _encode_video(frame_paths, output_path, delay_cs, fmt, common_kwargs, job_tmp, run_env,
                  job_id=None, progress_total=0, quality=DEFAULT_QUALITY,
                  boomerang=False, frame_delays=None):
    """Produce an MP4/WebM: same two-step model as APNG — ImageMagick prepares
    the transformed frames, ffmpeg encodes them (H.264 / VP9). `loop` is ignored
    (no video equivalent). Per-frame delays become per-frame durations."""
    prep_pattern = _prep_frames(frame_paths, common_kwargs, job_tmp, run_env, fmt.upper(),
                                job_id, progress_total, boomerang=boomerang)
    _set_job(job_id, JOB_PHASE_ENCODING, progress_total, progress_total)
    timeline = _write_timeline(job_tmp, frame_delays)
    cmd_enc = build_video_encode_command(prep_pattern, str(output_path), delay_cs, fmt,
                                         quality=quality, concat_file=timeline)
    _run_or_500(cmd_enc, job_tmp, run_env, f"{fmt.upper()} encoding failed")
    _require_output(output_path, f"{fmt.upper()} encoding")


def _video_srgb_filter(source: Path) -> str:
    result = _run_subprocess(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height,color_space,color_transfer,color_primaries:stream_side_data=rotation", "-of", "json", str(source)], PROBE_TIMEOUT_S)
    streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
    if not streams:
        raise HTTPException(status_code=400, detail="Could not establish the video's SDR color encoding.")
    info = streams[0]
    transfer = info.get("color_transfer", "unknown")
    primaries = info.get("color_primaries", "unknown")
    if transfer in ("unknown", "unspecified", "reserved"):
        transfer = "bt709"
    if primaries in ("unknown", "unspecified", "reserved"):
        primaries = "bt709"
    matrices = {"bt709": "bt709", "bt470bg": "bt601", "smpte170m": "bt601",
                "smpte240m": "smpte240m", "fcc": "fcc", "bt2020nc": "bt2020"}
    source_matrix = info.get("color_space", "unknown")
    if source_matrix not in matrices and source_matrix not in ("unknown", "unspecified", "reserved", "gbr"):
        raise HTTPException(status_code=400, detail="Unsupported video color matrix; preconvert to SDR BT.709.")
    matrix = matrices.get(source_matrix, "bt709")
    width, height = int(info["width"]), int(info["height"])
    for side in info.get("side_data_list", []):
        rotation = float(side.get("rotation", 0))
        if rotation % 90:
            raise HTTPException(status_code=400, detail="Video rotation must be a multiple of 90 degrees; preconvert this source first.")
        if abs(rotation) % 180 == 90:
            width, height = height, width
            break
    # Convert matrix/range first (also accepts RGB sources), then primaries/TRC.
    # The colorspace filter requires even geometry; crop its temporary pad away.
    return ("pad=ceil(iw/2)*2:ceil(ih/2)*2,"
            f"scale=in_color_matrix={matrix}:out_color_matrix=bt709:out_range=tv,format=yuv444p,"
            f"colorspace=ispace=bt709:iprimaries={primaries}:itrc={transfer}:irange=tv:"
            "all=bt709:trc=srgb:range=tv:format=yuv444p,"
            f"crop={width}:{height}:0:0:exact=1")


def _decode_command(source: Path, pattern: str) -> list[str]:
    """How to turn one source into full canonical frames.

    Dispatch is on CONTENT, not the file's name: an animated PNG may be called
    .apng or .png, and ImageMagick reports both as a single-frame PNG, so the
    same bytes used to decode to three frames or one depending on the
    extension. Animation deltas are coalesced before transforms; document pages
    are independent images and keep their own dimensions."""
    if source.suffix.lower() in VIDEO_EXTENSIONS or is_apng(source):
        # build_apng_decode_command is a generic ffmpeg frame dumper
        # (-vsync 0 passthrough): it decodes APNG and MP4/WebM identically.
        cmd = build_apng_decode_command(str(source), pattern)
        if is_apng(source):
            at = cmd.index("-i")
            cmd[at:at] = ["-f", "apng"]
        if source.suffix.lower() in VIDEO_EXTENSIONS:
            cmd[-1:-1] = ["-vf", _video_srgb_filter(source)]
        return cmd
    if source.suffix.lower() == ".pdf":
        # -density is read-time and must precede the input so PDF pages rasterize
        # at a readable resolution instead of the 72-dpi default.
        return ["convert", "-density", "150", str(source), "+repage", *color_normalization_args(), pattern]
    if source.suffix.lower() in (".tif", ".tiff"):
        # Pages are independent images, not animation deltas. Coalescing would
        # clip/pad all pages to page one's size before canvas policy is applied.
        return ["convert", str(source), "+repage", *color_normalization_args(), pattern]
    if source.suffix.lower() in LAYERED_EXTENSIONS:
        # A PSD's extra "frames" are layers, not moments in time. Reading the
        # flattened composite is what the user sees when they open the file;
        # treating the layers as animation frames was never intended.
        return ["convert", f"{source}[0]", *color_normalization_args(), pattern]
    return ["convert", str(source), "-coalesce", *color_normalization_args(), pattern]


def _png_icc_profile(source: Path) -> Optional[bytes]:
    """Read a bounded global PNG profile independent of decoder frame side data."""
    limit = 4 * 1024 * 1024
    with source.open("rb") as handle:
        if handle.read(8) != PNG_SIGNATURE:
            return None
        for _ in range(4096):
            header = handle.read(8)
            if len(header) != 8:
                return None
            size, kind = int.from_bytes(header[:4], "big"), header[4:]
            if kind in (b"IDAT", b"IEND"):
                return None
            if kind == b"iCCP":
                if size > limit:
                    raise HTTPException(status_code=400, detail="PNG color profile is too large.")
                try:
                    data = handle.read(size).split(b"\0", 1)[1]
                    if not data or data[0] != 0:
                        raise ValueError()
                    decoder = zlib.decompressobj()
                    profile = decoder.decompress(data[1:], limit + 1)
                    if len(profile) > limit or not decoder.eof:
                        raise ValueError()
                    return profile
                except (ValueError, IndexError, zlib.error):
                    raise HTTPException(status_code=400, detail="PNG color profile is invalid or too large.")
            handle.seek(size + 4, 1)
    raise HTTPException(status_code=400, detail="Too many PNG metadata chunks.")


def _layout_pdf_pages(frames, layout, work, run_env):
    _require_within_dimension_budget(layout.width, layout.height, "PDF page")
    _require_within_pixel_budget(layout.width * layout.height * len(frames))
    pages = []
    for index, source in enumerate(frames):
        page = work / f"pdf-page_{index:05d}.png"
        _run_or_500(build_page_command(source, page, layout), work, run_env, "PDF page layout failed")
        pages.append(str(page))
    return pages


def _extract_frames(source: Path, job_tmp: Path, run_env: dict, prefix: str = "frame") -> list[str]:
    """Decode any source into numbered full-canvas PNG frames and return their
    sorted paths. The single input stage for every direction. Enforces the
    frame-count cap."""
    source = _prepare_avif_still(source, job_tmp)
    pattern = str(job_tmp / f"{prefix}_{FRAME_NUMBER_PATTERN}.png")
    _run_or_500(_decode_command(source, pattern), job_tmp, run_env, "Could not read source")

    frames = [str(p) for p in sorted(job_tmp.glob(f"{prefix}_*.png"),
                                     key=_frame_order_key)]
    if not frames:
        raise HTTPException(status_code=500, detail="Source produced no frames.")
    if len(frames) > MAX_EXPLODE_FRAMES:
        raise HTTPException(status_code=400, detail=f"Too many frames ({len(frames)}).")
    if is_apng(source):
        # APNG demuxing does not reliably copy the global ICC profile to every
        # frame. Apply it explicitly, including for direct PNG extraction.
        profile = _png_icc_profile(source)
        if profile is None:
            return frames  # untagged RGB is already the policy's sRGB working space
        profile_args = []
        if profile is not None:
            profile_path = job_tmp / "source-color.icc"
            profile_path.write_bytes(profile)
            profile_args = ["+profile", "icc", "-profile", str(profile_path)]
        for frame in frames:
            normalized = job_tmp / "color-normalized.png"
            _run_or_500(["convert", frame, *profile_args, *color_normalization_args(), str(normalized)],
                        job_tmp, run_env, "Could not normalize decoded color")
            normalized.replace(frame)
    return frames


def _prepare_avif_still(source: Path, work: Path) -> Path:
    if source.suffix.lower() != ".avif":
        return source
    frames, width, height = _probe_source(str(source))
    _require_measurable(width, height, "AVIF image")
    _require_within_dimension_budget(width, height, "AVIF image")
    _require_within_pixel_budget(width * height)
    if frames != 1:
        raise HTTPException(status_code=400, detail="AVIF needs one primary still image.")
    directory = work / "avif-decoded"
    directory.mkdir(exist_ok=True)
    decoded = directory / (source.stem + ".png")
    try:
        result = _run_subprocess(["avifdec", "-j", "2", "--size-limit", str(min(MAX_DECODED_PIXELS, 268435456)), "--dimension-limit", str(MAX_IMAGE_DIMENSION), str(source), str(decoded)], min(PROBE_TIMEOUT_S, _step_timeout()))
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="The AVIF still decoder is unavailable in this build.")
    if result.returncode != 0 or not decoded.is_file():
        raise HTTPException(status_code=400, detail="This AVIF variant could not be decoded as a still image.")
    # This pinned decoder exports the untransformed pixels. Apply its active
    # rotation explicitly; do not silently lose other presentation transforms.
    details = result.stdout or ""
    if "imir (" in details or "clap (" in details:
        raise HTTPException(status_code=400, detail="This AVIF mirror/crop variant is unsupported; export an ordinary still image first.")
    rotation = re.search(r"irot \(Rotation\)\s*:\s*([0-3])", details)
    if rotation and int(rotation.group(1)):
        oriented = directory / (source.stem + "-oriented.png")
        _run_or_500(["convert", str(decoded), "-rotate", str(-90 * int(rotation.group(1))), str(oriented)], work, os.environ.copy(), "Could not orient AVIF still")
        oriented.replace(decoded)
    output_width, output_height = _read_output_dims(decoded)
    _require_measurable(output_width, output_height, "Decoded AVIF")
    _require_within_dimension_budget(output_width, output_height, "Decoded AVIF")
    _require_within_pixel_budget(output_width * output_height)
    return decoded


def _probe_source(path: str) -> tuple[int, int, int]:
    """(frames, width, height) from one metadata read, or zeros when they can't
    be determined. One call answers both the frame cap and the pixel budget, so
    measuring an input costs no more than counting it did."""
    if Path(path).suffix.lower() == ".avif":
        _reject_unsupported_color(Path(path))
    try:
        r = _run_subprocess(["identify", "-format", "%n %w %h\n", path], PROBE_TIMEOUT_S)
        if r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            if lines:
                parts = lines[0].split()
                if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                    if Path(path).suffix.lower() == ".avif" and int(parts[0]) != 1:
                        raise HTTPException(status_code=400, detail="AVIF collections are unsupported; use one still image.")
                    return int(parts[0]), int(parts[1]), int(parts[2])
    except HTTPException:
        raise
    except Exception:
        pass
    return 0, 0, 0  # unknown → frame cap + ImageMagick resource limits still apply


def _count_frames(path: str) -> int:
    """Number of frames/pages in an image, or 0 if it can't be determined."""
    return _probe_source(path)[0]


def _reject_unsupported_color(source: Path) -> None:
    if source.suffix.lower() == ".avif":
        if ".avif" not in _supported("input_extensions", ALLOWED_EXTENSIONS):
            reason = CAPABILITIES.get("unavailable", {}).get("input:.avif", "The AVIF read/write smoke did not pass.")
            raise HTTPException(status_code=400, detail="AVIF input is not available in this build. " + reason)
        try:
            validate_avif_still(source)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
    # HDR needs tone mapping; the SDR policy must not silently reinterpret it.
    if source.suffix.lower() not in VIDEO_EXTENSIONS:
        with source.open("rb") as handle:
            if handle.read(8) == PNG_SIGNATURE:
                while True:
                    header = handle.read(8)
                    if len(header) != 8:
                        break
                    size, kind = int.from_bytes(header[:4], "big"), header[4:]
                    if kind == b"cICP":
                        color = handle.read(min(size, 4))
                        if len(color) >= 2 and color[1] in (16, 18):
                            raise HTTPException(status_code=400, detail="HDR PNG is unsupported; convert to SDR sRGB first.")
                        handle.seek(max(0, size - 4) + 4, 1)
                    else:
                        handle.seek(size + 4, 1)
                    if kind in (b"IDAT", b"IEND"):
                        break
    if source.suffix.lower() in VIDEO_EXTENSIONS:
        probe = _run_subprocess(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_transfer", "-of", "json", str(source)], PROBE_TIMEOUT_S)
        if probe.returncode == 0:
            streams = json.loads(probe.stdout).get("streams", [])
            if any(stream.get("color_transfer") in ("smpte2084", "arib-std-b67") for stream in streams):
                raise HTTPException(status_code=400, detail="HDR video is unsupported; tone-map to SDR sRGB/BT.709 first.")


def _probe_frames_and_size(source: Path) -> tuple[int, int, int]:
    """(frames, width, height) measured the way the source will actually be
    decoded. Measuring and decoding have to agree: `identify` reports an APNG
    as one frame, so a budget built on it would wave through an animation that
    ffmpeg then expands into thousands, and it reports a PSD's layer count for
    a file the app reads as a single composite."""
    _reject_unsupported_color(source)
    if source.suffix.lower() in VIDEO_EXTENSIONS or is_apng(source):
        return _probe_video(str(source))
    frames, width, height = _probe_source(str(source))
    if source.suffix.lower() in LAYERED_EXTENSIONS:
        return 1, width, height
    return frames, width, height


def _probe_video(path: str) -> tuple[int, int, int]:
    """(frames, width, height) for a video, or zeros when undeterminable.
    `identify` can't read video, so MP4/WebM use ffprobe — one call for both the
    frame cap and the pixel budget, mirroring _probe_source."""
    try:
        r = _run_subprocess(
            ["ffprobe", "-v", "error", "-count_packets",
             "-select_streams", "v:0",
             "-show_entries", "stream=width,height,nb_read_packets",
             "-of", "csv=p=0", *(["-f", "apng"] if is_apng(path) else []), path],
            PROBE_TIMEOUT_S)
        if r.returncode == 0:
            line = r.stdout.strip().splitlines()
            if line:
                parts = [p.strip() for p in line[0].split(",")]
                if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                    width, height, frames = (int(parts[0]), int(parts[1]), int(parts[2]))
                    return frames, width, height
    except Exception:
        pass
    return 0, 0, 0  # unknown → post-decode count + resource limits still apply


def _count_video_frames(path: str) -> int:
    """Number of frames in a video, or 0 if it can't be determined."""
    return _probe_video(path)[0]


# How many times a source says it should repeat, as ImageMagick reports it.
# There is no format specifier for this in ImageMagick-6 ("%[iterations]" is
# not a known property here), so it is read from the verbose dump — of the
# FIRST frame only, which still carries the property and costs a fraction of
# dumping every frame of a long animation.
LOOP_PROPERTY = "Iterations:"


def _read_loop_count(source: Path, run_env: dict, cwd: Path) -> Optional[int]:
    """The source's own loop count, or None when this build cannot read it.

    None means unknown, not "loops forever": a caller must leave its own
    setting alone rather than assume one."""
    try:
        probe = _run_subprocess(["identify", "-verbose", f"{source}[0]"],
                                PROBE_TIMEOUT_S, cwd=str(cwd), env=run_env)
    except Exception:
        return None
    if probe.returncode != 0:
        return None
    for line in probe.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(LOOP_PROPERTY):
            value = stripped[len(LOOP_PROPERTY):].strip()
            if value.isdigit():
                return int(value)
    return None


def _read_frame_delays(path: str) -> Optional[list[int]]:
    """Return each frame's own delay in centiseconds (identify %T), so /reconvert
    can preserve the source animation's original rhythm. Returns None — meaning
    "fall back to a uniform delay" — when the delays can't be read or every value
    is 0 (stills and multi-page TIFF/PDF report 0, i.e. no animation timing).
    Values are clamped to a 1 cs floor (a 0-delay frame among non-zero ones is
    degenerate; 1 cs keeps it visible). The caller reconciles the length against
    the decoded frame count and drops these on any mismatch."""
    if is_apng(path) and _png_icc_profile(Path(path)) is not None:
        try:
            result = _run_subprocess(["ffprobe", "-v", "error", "-f", "apng", "-select_streams", "v:0", "-show_entries", "frame=pkt_duration_time", "-of", "json", path], PROBE_TIMEOUT_S)
            values = json.loads(result.stdout).get("frames", []) if result.returncode == 0 else []
            durations = [max(1, round(float(frame["pkt_duration_time"]) * 100)) for frame in values]
            return durations or None
        except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            return None
    try:
        r = _run_subprocess(["identify", "-format", "%T\n", path], PROBE_TIMEOUT_S)
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
def inspect(file: UploadFile = File(...)):
    content = _read_upload(file, "File")
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")

    # Accept anything at least one direction can consume (inspection is read-only)
    ok_convert, _ = validate_file(file.filename or "", file.content_type or "")
    ok_explode, _ = validate_explode_input(file.filename or "", file.content_type or "")
    if not (ok_convert or ok_explode):
        raise HTTPException(status_code=400, detail="Unsupported file type.")

    nulls = {"frames": None, "width": None, "height": None, "format": None,
             "delays_cs": None, "loop": None}

    job_id = str(uuid.uuid4())
    job_tmp = TMP_DIR / job_id

    try:
        job_tmp.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(file.filename or "input")
        src = job_tmp / safe_name
        src.write_bytes(content)

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}
        r = _run_subprocess(["identify", "-format", "%n %w %h %m %T\n", str(src)],
                            PROBE_TIMEOUT_S, cwd=str(job_tmp), env=run_env)
        if r.returncode != 0 or not r.stdout.strip():
            return nulls

        # One line per frame: "<total> <width> <height> <format> <delay_cs>"
        lines = r.stdout.strip().splitlines()
        first = lines[0].split()
        if len(first) < 5:
            return nulls
        # identify's own count is not the truth for every format: it sees one
        # frame in an APNG and a layer count in a PSD. The card must badge what
        # a conversion will actually produce, measured the same way.
        identified_frames = int(first[0])
        frames = _probe_frames_and_size(src)[0] or identified_frames
        delays_cs = []
        for line in lines[:MAX_EXPLODE_FRAMES]:
            parts = line.split()
            delays_cs.append(int(parts[4]) if len(parts) >= 5 and parts[4].lstrip("-").isdigit() else 0)
        if len(delays_cs) != frames:
            delays_cs = []  # these timings cannot address the real frames
        return {
            "frames": frames,
            "width": int(first[1]),
            "height": int(first[2]),
            "format": "AVIF" if src.suffix.lower() == ".avif" else first[3],
            "delays_cs": delays_cs,
            # A loop count only means something for an animation: ImageMagick
            # reports Iterations: 0 for a plain still, which is not the same as
            # a source asking to repeat forever.
            "loop": _read_loop_count(src, run_env, job_tmp) if frames > 1 else None,
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
@with_job_slot
def explode(
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
            detail=f"Output format must be one of: {', '.join(_supported('still_outputs', EXPLODE_OUTPUT_FORMATS))}.",
        )
    _reject_unavailable_format("still_outputs", fmt, EXPLODE_OUTPUT_FORMATS)
    if not (1 <= quality <= 100):
        raise HTTPException(status_code=400, detail="Quality must be 1–100.")
    if start < 0:
        raise HTTPException(status_code=400, detail="Start frame must be 0 or greater.")
    if end is not None and end < start:
        raise HTTPException(status_code=400, detail="End frame must be greater than or equal to the start frame.")
    if not (1 <= step <= 100):
        raise HTTPException(status_code=400, detail="Every Nth (step) must be 1–100.")

    content = _read_upload(file, "File")
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")

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

        # ── Budgets, all from metadata, before a single frame is decoded ──
        # Video is measured with ffprobe (identify can't read video); both paths
        # reject over the caps before any decoding happens.
        frame_count, width, height = _probe_frames_and_size(src)
        if frame_count > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"File has {frame_count} frames; the limit is {MAX_EXPLODE_FRAMES}.",
            )
        _require_measurable(width, height, "This file")
        _require_within_dimension_budget(width, height, "This file")
        _require_within_pixel_budget(width * height * max(frame_count, 1))

        # A range that selects nothing is a client mistake, not a result: answer
        # it from the frame count rather than decoding and re-encoding the whole
        # file first and discovering it afterwards.
        if frame_count and start >= frame_count:
            raise HTTPException(
                status_code=400,
                detail=f"Start frame {start} is beyond the last frame ({frame_count - 1}).",
            )

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}

        # Selection happens per branch, not after: for a re-encoded target it
        # has to precede the conversion, or every frame the user did not ask for
        # is encoded and thrown away. Arcnames keep each frame's original
        # 0-based index, so a selected ZIP still reads frame_00010.png.
        if src.suffix.lower() in VIDEO_EXTENSIONS or is_apng(src):
            # ffmpeg decodes exact frames; the chosen format comes after.
            # Detected from the bytes, because an APNG may be named .png and
            # ImageMagick would then hand back only its first frame.
            decoded = sorted((Path(p) for p in
                              _extract_frames(src, job_tmp, run_env, prefix="frame")),
                             key=_frame_order_key)
            selected = _select_frames(decoded, start, end, step)
            if out_ext == "png":
                frames = selected
            else:
                frames = []
                for source_frame in selected:
                    converted = source_frame.with_suffix(f".{out_ext}")
                    _run_or_500(
                        ["convert", str(source_frame), *color_normalization_args(), *(["-background", "white", "-alpha", "remove", "-alpha", "off"] if out_ext == "jpg" else []), "-quality", str(quality), str(converted)],
                        job_tmp, run_env, "Extraction failed",
                    )
                    frames.append(converted)
        else:
            # ImageMagick must coalesce from frame 0: a GIF frame can be a delta
            # on the ones before it, so a sub-range cannot be decoded alone.
            out_pattern = str(job_tmp / f"frame_{FRAME_NUMBER_PATTERN}.{out_ext}")
            cmd = build_explode_command(str(src), out_pattern, fmt, quality)
            _run_or_500(cmd, job_tmp, run_env, "Extraction failed")
            decoded = sorted(
                (p for p in job_tmp.iterdir()
                 if p.is_file() and p.suffix.lower() == f".{out_ext}"),
                key=_frame_order_key,
            )
            if not decoded:
                raise HTTPException(status_code=500, detail="No frames were extracted.")
            frames = _select_frames(decoded, start, end, step)

        if not frames:
            raise HTTPException(
                status_code=400,
                detail=f"The selected range covers no frames of this "
                       f"{len(decoded)}-frame file.")

        if len(frames) > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Too many frames extracted ({len(frames)}); the limit is {MAX_EXPLODE_FRAMES}.",
            )

        stem = sanitize_filename((file.filename or "frames").rsplit(".", 1)[0]) or "frames"
        return _zip_response(frames, f"{stem}_frames.zip",
                             headers={"X-Frame-Count": str(len(frames))})

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
@with_job_slot
def convert_image(
    file: UploadFile = File(...),
    out_format: str = Form(...),
    quality: int = Form(90),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    keep_output: bool = Form(True),
    custom_name: Optional[str] = Form(None),
    icon_size: int = Form(256),
):
    fmt = out_format.lower().strip()
    if fmt not in STILL_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Output format must be one of: {', '.join(_supported('still_outputs', STILL_OUTPUT_FORMATS))}.",
        )
    _reject_unavailable_format("still_outputs", fmt, STILL_OUTPUT_FORMATS)
    if fmt == "ico":
        if icon_size not in ICO_SIZES:
            raise HTTPException(status_code=400, detail="ICO size must be one of: " + ", ".join(map(str, ICO_SIZES)) + ".")
        if width is not None or height is not None:
            raise HTTPException(status_code=400, detail="Use icon_size for ICO; generic width/height must be empty.")
    if fmt != "ico" and not (1 <= quality <= 100):
        raise HTTPException(status_code=400, detail="Quality must be 1–100.")
    if width is not None and not (1 <= width <= 8000):
        raise HTTPException(status_code=400, detail="Width must be 1–8000 px.")
    if height is not None and not (1 <= height <= 8000):
        raise HTTPException(status_code=400, detail="Height must be 1–8000 px.")

    content = _read_upload(file, "File")
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")

    ok, msg = validate_file(file.filename or "", file.content_type or "")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    storage_id = str(uuid.uuid4())
    job_input = INPUT_DIR / storage_id
    job_tmp = TMP_DIR / storage_id
    out_ext = STILL_OUTPUT_EXT[fmt]
    staging_path = STAGING_DIR / f"{storage_id}{out_ext}"
    staging_fd = None

    try:
        job_input.mkdir(parents=True, exist_ok=True)
        job_tmp.mkdir(parents=True, exist_ok=True)
        staging_path, staging_fd = _pinned_staging_path(staging_path.name)

        safe_name = sanitize_filename(file.filename or "image")
        src = job_input / safe_name
        src.write_bytes(content)

        # A small file can still decode to an enormous image, so measure before
        # handing it to a decoder.
        frames, source_width, source_height = _probe_source(str(src))
        _require_measurable(source_width, source_height, "This image")
        _require_within_dimension_budget(source_width, source_height, "This image")
        _require_within_pixel_budget(source_width * source_height * max(frames, 1))

        # Claimed atomically after validation; see _publish_output.
        if keep_output:
            output_name = f"magicflow_{storage_id}{out_ext}"
            if custom_name:
                safe_custom = sanitize_filename(custom_name)
                safe_stem = safe_custom.rsplit(".", 1)[0] if "." in safe_custom else safe_custom
                _reject_reserved_output_name(safe_stem)
                if safe_stem:
                    output_name = f"{safe_stem}{out_ext}"
        else:
            # Not kept: registered as temporary once produced, so it is hidden
            # from /files, released by the client after download, and expired by
            # _sweep_tmp_outputs otherwise. Custom naming is meaningless for a
            # file that must vanish.
            output_name = f"{TMP_OUTPUT_PREFIX}{storage_id}{out_ext}"

        src = _prepare_avif_still(src, job_tmp)
        # Read only the first frame/page so multi-frame inputs collapse to one still
        _reject_unsupported_color(src)
        cmd = build_command(
            frames=[f"{src}[0]"],
            output=str(staging_path),
            fmt=fmt,
            delay_cs=0,
            loop=0,
            width=width,
            height=height,
            quality=quality,
            still=True,
            icon_size=icon_size,
        )

        run_env = {**os.environ, "MAGICK_TMPDIR": str(job_tmp)}
        result = _run_subprocess(cmd, _step_timeout(), cwd=str(job_tmp), env=run_env)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown ImageMagick error."
            raise HTTPException(status_code=500, detail=f"Conversion failed: {detail}")
        _require_output(staging_path, "Conversion")
        if fmt == "ico":
            with staging_path.open("rb") as handle:
                header = handle.read(6)
            if header != b"\0\0\1\0\1\0" or _read_output_dims(staging_path) != (icon_size, icon_size):
                raise HTTPException(status_code=500, detail="ICO encoder did not produce the requested single square icon.")
        output_name = _publish_output(staging_path, output_name, storage_id)
        output_path = OUTPUT_DIR / output_name

        # Sidecar recipe for kept stills (F14).
        if keep_output:
            out_w, out_h = _read_output_dims(output_path)
            _write_recipe(
                output_name, "convert-image",
                params={"format": fmt, "quality": None if fmt == "ico" else quality, "width": width, "height": height,
                        **({"icon_size": icon_size, "icon_images": 1} if fmt == "ico" else {})},
                sources=[file.filename or ""],
                frames=1, width=out_w, height=out_h,
            )
        else:
            _register_temp_output(output_name)

        return JSONResponse(_result_payload(output_name, output_path, keep_output))

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Conversion timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if staging_fd is not None:
            try:
                staging_path.unlink(missing_ok=True)
            finally:
                os.close(staging_fd)
        shutil.rmtree(job_input, ignore_errors=True)
        shutil.rmtree(job_tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Re-convert a stored output with new settings
# (coalesce back to frames, then re-run build_command)
# ─────────────────────────────────────────────

@app.post("/reconvert/{filename}")
@with_job_slot
def reconvert(
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
    pdf_page_size: str = Form("original"),
    pdf_orientation: str = Form("portrait"),
    pdf_width_mm: float = Form(210),
    pdf_height_mm: float = Form(297),
    pdf_margin_mm: float = Form(10),
    pdf_fit: str = Form("fit"),
):
    # ── Locate the source file in OUTPUT_DIR ──
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    source = OUTPUT_DIR / safe
    if not _regular_output(safe):
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
            detail=f"Format must be one of: {', '.join(_supported('animation_outputs', ALLOWED_OUTPUT_FORMATS))}.",
        )
    _reject_unavailable_format("animation_outputs", fmt, ALLOWED_OUTPUT_FORMATS)

    pdf_params = dict(pdf_page_size=pdf_page_size, pdf_orientation=pdf_orientation,
                      pdf_width_mm=pdf_width_mm, pdf_height_mm=pdf_height_mm,
                      pdf_margin_mm=pdf_margin_mm, pdf_fit=pdf_fit) if fmt == "pdf" else {}
    try:
        pdf_layout = resolve_layout(pdf_page_size, pdf_orientation, pdf_width_mm,
                                    pdf_height_mm, pdf_margin_mm, pdf_fit) if fmt == "pdf" else None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    _validate_transform_params(
        delay=delay, loop=loop, width=width, height=height,
        resize_mode=resize_mode, quality=quality, rotate=rotate,
        brightness=brightness, saturation=saturation, hue=hue,
        sharpen=sharpen, blur=blur, colors=colors, dither=dither, background=background,
    )

    storage_id = str(uuid.uuid4())
    job_tmp = TMP_DIR / storage_id

    ext_map = {"gif": ".gif", "webp": ".webp", "apng": ".apng", "tiff": ".tiff",
               "pdf": ".pdf", "mp4": ".mp4", "webm": ".webm"}
    out_ext = ext_map[fmt]
    staging_path = STAGING_DIR / f"{storage_id}{out_ext}"
    staging_fd = None

    try:
        job_tmp.mkdir(parents=True, exist_ok=True)
        staging_path, staging_fd = _pinned_staging_path(staging_path.name)

        source = _snapshot_output(safe, job_tmp)

        # ── Budgets before coalescing the stored file back into frames ──
        frame_count, source_width, source_height = _probe_frames_and_size(source)
        if frame_count > MAX_EXPLODE_FRAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Source has {frame_count} frames; the limit is {MAX_EXPLODE_FRAMES}.",
            )
        _require_measurable(source_width, source_height, "This source")
        _require_within_dimension_budget(source_width, source_height, "This source")
        _require_within_pixel_budget(source_width * source_height * max(frame_count, 1))

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

        # ── Name the result (never overwrite the source, or anything else) ──
        # Claimed atomically after validation; see _publish_output. Re-converting
        # with default settings twice therefore yields two files, not one
        # replaced file.
        if keep_output:
            output_name = f"magicflow_{storage_id}{out_ext}"
            if custom_name:
                safe_custom = sanitize_filename(custom_name)
                safe_stem = safe_custom.rsplit(".", 1)[0] if "." in safe_custom else safe_custom
                _reject_reserved_output_name(safe_stem)
                if safe_stem:
                    output_name = f"{safe_stem}{out_ext}"
            else:
                output_name = f"{source.stem}_re{out_ext}"
        else:
            # Not kept: registered as temporary once produced, so it is hidden
            # from /files, released by the client after download, and expired by
            # _sweep_tmp_outputs otherwise.
            output_name = f"{TMP_OUTPUT_PREFIX}{storage_id}{out_ext}"

        delay_cs = max(1, round(delay / 10))

        # Every target can now carry a frame's own duration: APNG stores a delay
        # per frame, and MP4/WebM carry per-frame timestamps. This used to
        # collapse an uneven source to its mean for those formats, on the
        # premise that they were uniform-framerate — they are not, and the
        # averaging was reported only after the file had been written.
        encode_delay_cs = delay_cs
        if frame_delays is not None and len(set(frame_delays)) == 1:
            encode_delay_cs = frame_delays[0]  # already clamped >= 1

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

        # A failed or timed-out encode leaves only a staging file, which the
        # finally block removes — the library never sees a partial result.
        if fmt == "apng":
            _encode_apng(frame_paths, str(staging_path), encode_delay_cs, loop, common, job_tmp,
                         run_env, frame_delays=frame_delays)
        elif fmt in ("mp4", "webm"):
            _encode_video(frame_paths, str(staging_path), encode_delay_cs, fmt, common, job_tmp,
                          run_env, quality=quality, frame_delays=frame_delays)
        elif fmt in ("gif", "webp"):
            _prep_frames(frame_paths, common, job_tmp, run_env, "Re-conversion")
            prepared = [str(p) for p in sorted(job_tmp.glob("prep_*.png"), key=_frame_order_key)]
            cmd = build_assembly_command(prepared, str(staging_path), fmt, encode_delay_cs, loop,
                                         quality, colors=colors, dither=dither,
                                         optimize_layers=optimize_layers, frame_delays=frame_delays)
            _run_or_500(cmd, job_tmp, run_env, "Re-conversion failed")
            _require_output(staging_path, "Re-conversion")
        elif fmt == "pdf" and pdf_layout:
            _prep_frames(frame_paths, common, job_tmp, run_env, "Re-conversion")
            prepared = [str(p) for p in sorted(job_tmp.glob("prep_*.png"), key=_frame_order_key)]
            pages = _layout_pdf_pages(prepared, pdf_layout, job_tmp, run_env)
            _run_or_500(build_pdf_command(pages, staging_path), job_tmp, run_env, "PDF assembly failed")
            _require_output(staging_path, "Re-conversion")
        else:
            cmd = build_command(frames=frame_paths, output=str(staging_path), **common)
            _run_or_500(cmd, job_tmp, run_env, "Re-conversion failed")
            _require_output(staging_path, "Re-conversion")

        output_name = _publish_output(staging_path, output_name, storage_id)
        output_path = OUTPUT_DIR / output_name

        produced_frames = _probe_frames_and_size(output_path)[0] or len(frame_paths)
        timeline_ms = _effective_timeline_ms(output_path, encode_delay_cs, frame_delays)

        # Sidecar recipe for kept re-conversions (F14); records the source it
        # was derived from so the chain stays traceable.
        if keep_output:
            out_w, out_h = _read_output_dims(output_path)
            _write_recipe(
                output_name, "reconvert",
                params={
                    "format": fmt, "delay": delay, "keep_timing": keep_timing, "loop": loop,
                    "width": width, "height": height,
                    "resize_mode": resize_mode, "quality": quality,
                    "boomerang": boomerang, "rotate": rotate, "flip": flip, "flop": flop,
                    "brightness": brightness, "saturation": saturation, "hue": hue,
                    "grayscale": grayscale, "negate": negate, "sepia": sepia,
                    "auto_level": auto_level, "normalize": normalize,
                    "sharpen": sharpen, "blur": blur,
                    "colors": colors, "dither": dither, "optimize_layers": optimize_layers,
                    "strip": strip, "background": background, "source": safe, **pdf_params,
                    **({"pdf_layout": pdf_layout.facts()} if pdf_layout else {}),
                },
                sources=[safe],
                frames=produced_frames, width=out_w, height=out_h,
                encoder_delays_ms=([d * 10 for d in frame_delays] if frame_delays is not None
                                   else [encode_delay_cs * 10] * len(frame_paths)),
                legacy_delays_ms=timeline_ms,
                timing_mode=("source" if frame_delays is not None else
                             "fallback_uniform" if keep_timing else "uniform"),
            )
        else:
            _register_temp_output(output_name)

        return JSONResponse(
            {
                **_result_payload(output_name, output_path, keep_output),
                "frame_count": produced_frames,
                "effective_delays_ms": timeline_ms,
                # "preserved": the source's own per-frame timing was carried
                # through; absent when the user opted out or the source had no
                # per-frame timing. Nothing is averaged any more, so that value
                # is gone — an older client treats an unknown value as neither.
                "timing": "preserved" if frame_delays is not None else None,
            }
        )

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Re-conversion timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if staging_fd is not None:
            try:
                staging_path.unlink(missing_ok=True)
            finally:
                os.close(staging_fd)
        shutil.rmtree(job_tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# Thumbnail (first frame/page → 480px JPEG, cached by mtime)
# Cards use this so TIFF/PDF/RAW/HEIC/video get real previews and the
# grid stops downloading multi-megabyte animations to paint a small card.
# ─────────────────────────────────────────────

COMPARE_PREVIEW_SIZE = 640


def _comparison_preview(source: Path, size: int, work: Path) -> Response:
    """One bounded, orientation-correct SDR frame; never publish or retain sources."""
    def run(command):
        result = _run_subprocess(command, min(PROBE_TIMEOUT_S, _step_timeout()), cwd=str(work), env=environment)
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail="This source could not be rendered for comparison.")
    source = _prepare_avif_still(source, work)
    _reject_unsupported_color(source)
    animated_png = is_apng(source)
    video = source.suffix.lower() in VIDEO_EXTENSIONS
    if video:
        probe = _run_subprocess(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_side_data=rotation", "-of", "json", str(source)], PROBE_TIMEOUT_S)
        streams = json.loads(probe.stdout).get("streams", []) if probe.returncode == 0 else []
        info = streams[0] if streams else {}
        width, height = int(info.get("width", 0)), int(info.get("height", 0))
        if any(abs(float(side.get("rotation", 0))) % 180 == 90 for side in info.get("side_data_list", [])):
            width, height = height, width
    elif animated_png:
        with source.open("rb") as handle:
            header = handle.read(24)
        width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    else:
        probe = _run_subprocess(["identify", "-density", "72", "-format", "%w %h %W %H %[orientation]", f"{source}[0]"], PROBE_TIMEOUT_S)
        parts = probe.stdout.split() if probe.returncode == 0 else []
        width = max(int(parts[0]), int(parts[2])) if len(parts) >= 4 and all(p.isdigit() for p in parts[:4]) else 0
        height = max(int(parts[1]), int(parts[3])) if len(parts) >= 4 and all(p.isdigit() for p in parts[:4]) else 0
        if len(parts) > 4 and parts[4] in ("LeftTop", "RightTop", "RightBottom", "LeftBottom"):
            width, height = height, width
    _require_measurable(width, height, "Comparison source")
    _require_within_dimension_budget(width, height, "Comparison source")
    _require_within_pixel_budget(width * height)
    environment = {**os.environ, "MAGICK_TMPDIR": str(work)}
    first = f"{source}[0]"
    profile_args = []
    if video or animated_png:
        first = str(work / "first.png")
        command = _decode_command(source, first)
        scale = f"scale={COMPARE_PREVIEW_SIZE}:{COMPARE_PREVIEW_SIZE}:force_original_aspect_ratio=decrease"
        if "-vf" in command:
            index = command.index("-vf") + 1
            command[index] += "," + scale
        else:
            command[-1:-1] = ["-vf", scale]
        command[-1:-1] = ["-frames:v", "1", "-update", "1"]
        run(command)
        if animated_png:
            profile = _png_icc_profile(source)
            if profile:
                profile_path = work / "source.icc"
                profile_path.write_bytes(profile)
                profile_args = ["+profile", "icc", "-profile", str(profile_path)]
    preview = work / "preview.png"
    command = ["convert", "-density", "72", first, "-coalesce", *profile_args,
        *color_normalization_args(), "+repage", "-thumbnail", f"{COMPARE_PREVIEW_SIZE}x{COMPARE_PREVIEW_SIZE}>",
        "-depth", "8", "-strip", "PNG32:" + str(preview)]
    run(command)
    preview_width, preview_height = _read_output_dims(preview)
    if not preview_width or not preview_height or max(preview_width, preview_height) > COMPARE_PREVIEW_SIZE or preview.stat().st_size > 4 * COMPARE_PREVIEW_SIZE ** 2 + 65536:
        raise HTTPException(status_code=500, detail="Comparison preview exceeded its bounds.")
    return Response(preview.read_bytes(), media_type="image/png", headers={
        "Cache-Control": "no-store", "X-Compare-Width": str(width), "X-Compare-Height": str(height),
        "X-Compare-Bytes": str(size), "X-Compare-Preview-Width": str(preview_width),
        "X-Compare-Preview-Height": str(preview_height), "X-Compare-Mode": "first-frame-srgb-8bit",
    })


@app.post("/compare-preview")
@with_job_slot
def compare_uploaded_preview(file: UploadFile = File(...)):
    ok, message = validate_file(file.filename or "", file.content_type or "")
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    content = _read_upload(file, "Comparison source")
    if not content:
        raise HTTPException(status_code=400, detail="Empty comparison source.")
    _require_free_space()
    work = TMP_DIR / str(uuid.uuid4())
    work.mkdir(parents=True)
    try:
        source = work / ("source" + Path(file.filename).suffix.lower())
        source.write_bytes(content)
        return _comparison_preview(source, len(content), work)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Comparison preview timed out.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.get("/compare-preview/{filename}")
@with_job_slot
def compare_saved_preview(filename: str):
    with _open_output(filename) as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > MAX_TOTAL_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="This file is too large for a comparison preview.")
        _require_free_space()
        work = TMP_DIR / str(uuid.uuid4())
        work.mkdir(parents=True)
        try:
            source = _snapshot_output(filename, work, handle)
            return _comparison_preview(source, size, work)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Comparison preview timed out.")
        finally:
            shutil.rmtree(work, ignore_errors=True)


@app.get("/thumb/{filename}")
def thumb(filename: str):
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    with _open_output(safe) as handle:
        info = os.fstat(handle.fileno())
        mtime = info.st_mtime_ns
        cache_path = THUMB_DIR / f"{safe}.contained-{mtime}.jpg"
        if cache_path.exists() or cache_path.is_symlink():
            return _PinnedFileResponse(_open_output(cache_path.name, directory=THUMB_DIR), media_type="image/jpeg")
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_DIR) as job:
            source = _snapshot_output(safe, Path(job), handle)
            return _render_thumb(source, safe, mtime)


def _render_thumb(path: Path, safe: str, mtime: int):
    path = _prepare_avif_still(path, path.parent.parent)
    # Cache key ties the thumbnail to the source's mtime, so replacing the
    # output (same name, new content) invalidates the old thumbnail.
    cache_path = THUMB_DIR / f"{safe}.contained-{mtime}.jpg"

    if not cache_path.exists():
        rendered_path = path.parent.parent / "thumbnail-render.jpg"
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
        if ext in VIDEO_EXTENSIONS:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(path),
                   "-frames:v", "1", "-vf", "scale=480:-1", str(rendered_path)]
        else:
            # [0] = first frame/page; -density helps PDF rasterize legibly and
            # is harmless for raster inputs. List args only — never shell=True.
            cmd = ["convert", f"{path}[0]", "-density", "150",
                   *color_normalization_args(), "-thumbnail", "480x", "-background", "white", "-alpha", "remove", "-quality", "80", str(rendered_path)]

        import os
        run_env = {**os.environ, "MAGICK_TMPDIR": str(path.parent.parent)}
        try:
            r = _run_subprocess(cmd, PROBE_TIMEOUT_S, env=run_env)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=404, detail="Thumbnail render timed out.")
        # Degrade to the card's icon fallback (404) rather than a 500 on any
        # unrenderable / corrupt source.
        if r.returncode != 0 or not rendered_path.exists():
            rendered_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="Thumbnail unavailable.")

        rendered_path.replace(cache_path)

    return _PinnedFileResponse(_open_output(cache_path.name, directory=THUMB_DIR), media_type="image/jpeg")


@app.get("/download/{filename}")
async def download(filename: str, inline: bool = False):
    """Read-only. A download never deletes anything, so an interrupted transfer
    can be retried and a stray link or cross-site navigation cannot destroy an
    output. Clients release a temporary result afterwards via
    DELETE /files/temp/{filename}."""
    safe = sanitize_filename(filename)
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    path = OUTPUT_DIR / safe
    handle = _open_output(safe)

    ext = path.suffix.lower()
    media_type = OUTPUT_MEDIA_TYPES.get(ext, "application/octet-stream")

    # inline=1 serves the file for in-browser preview (the full-size viewer) so a
    # PDF renders in an <iframe> instead of triggering a download. Only known
    # media types from OUTPUT_MEDIA_TYPES are served (no HTML/SVG), so inline
    # rendering can't execute injected markup.
    disposition = "inline" if inline else "attachment"
    return _PinnedFileResponse(handle, media_type=media_type, filename=safe,
                        content_disposition_type=disposition)
