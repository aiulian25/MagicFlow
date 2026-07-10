import re
from pathlib import Path

# ── Input allow-lists ────────────────────────────────────────────────────────
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    # Common web/raster
    ".jpg", ".jpeg", ".png", ".apng", ".webp", ".gif", ".bmp", ".tga",
    # TIFF / PSD
    ".tif", ".tiff", ".psd",
    # HEIC/HEIF
    ".heic", ".heif",
    # Camera RAW
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw",
})

ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/apng", "image/webp", "image/gif", "image/bmp",
    "image/tiff", "image/heic", "image/heif",
    "image/x-tga", "image/x-tiff",
    "video/mp4", "video/webm",  # video sources for Extract (frame dump)
    "application/pdf",  # PDF sources for Extract / Re-convert
    # RAW files: browsers typically report these as octet-stream; we skip MIME
    # check for known RAW extensions (handled in validate_file below).
    "application/octet-stream",
    "",  # some browsers send empty type for local files
})

RAW_EXTENSIONS: frozenset[str] = frozenset({
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw",
})

# ── Output formats ───────────────────────────────────────────────────────────
ALLOWED_OUTPUT_FORMATS: frozenset[str] = frozenset({"gif", "webp", "apng", "tiff", "pdf", "mp4", "webm"})

OUTPUT_MEDIA_TYPES: dict[str, str] = {
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".apng": "image/apng",
    ".tiff": "image/tiff",
    ".pdf":  "application/pdf",
    # Video outputs (frames → H.264 MP4 / VP9 WebM)
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    # Still image outputs (single still → still converter)
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".bmp":  "image/bmp",
    ".tga":  "image/x-tga",
    ".avif": "image/avif",
    ".heic": "image/heic",
}

OUTPUT_EXTENSIONS: set[str] = set(OUTPUT_MEDIA_TYPES.keys())

# ── Single still → still conversion ──────────────────────────────────────────
# Output formats for the image converter. All verified read/write in the
# container's ImageMagick build (libheif gives AVIF/HEIC, libwebp gives WebP).
STILL_OUTPUT_FORMATS: frozenset[str] = frozenset({
    "jpg", "png", "webp", "avif", "heic", "tiff", "bmp", "tga",
})
STILL_OUTPUT_EXT: dict[str, str] = {
    "jpg": ".jpg", "png": ".png", "webp": ".webp", "avif": ".avif",
    "heic": ".heic", "tiff": ".tiff", "bmp": ".bmp", "tga": ".tga",
}

# ── Explode (animation → frames) ─────────────────────────────────────────────
# Input formats we can reliably split into frames with the installed stack
# (ImageMagick-6 + libwebp + ffmpeg for APNG/video). PDF is rasterized via the
# ghostscript delegate (see Dockerfile: package + read-only PDF policy). MP4/WebM
# are frame-dumped by ffmpeg — the reverse of the MP4/WebM video output (F2/F3).
EXPLODE_INPUT_EXTENSIONS: frozenset[str] = frozenset({
    ".gif", ".webp", ".apng", ".png", ".tif", ".tiff", ".pdf", ".mp4", ".webm",
})

# Still output formats for extracted frames
EXPLODE_OUTPUT_FORMATS: frozenset[str] = frozenset({"png", "jpg", "webp"})
EXPLODE_OUTPUT_EXT: dict[str, str] = {"png": "png", "jpg": "jpg", "webp": "webp"}


def sanitize_filename(name: str) -> str:
    """Return a safe filename: basename only, spaces to underscores,
    only [a-zA-Z0-9._-] characters, no leading dots, max 200 chars."""
    name = Path(name).name
    name = name.replace(" ", "_")
    name = re.sub(r"[^a-zA-Z0-9._\-]", "", name)
    name = name.lstrip(".")
    if len(name) > 200:
        suffix = Path(name).suffix
        name = name[: 200 - len(suffix)] + suffix
    return name or "file"


def validate_file(filename: str, content_type: str) -> tuple[bool, str]:
    """Return (True, '') if the file is acceptable, else (False, reason)."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, (
            f"Extension '{ext}' is not supported. "
            "Accepted: JPG, PNG, APNG, WebP, GIF, BMP, TGA, TIFF, PSD, HEIC, RAW (CR2/NEF/ARW/DNG…)"
        )
    # RAW files always arrive as octet-stream — skip MIME check for them.
    if ext in RAW_EXTENSIONS:
        return True, ""
    mime = content_type.split(";")[0].strip().lower()
    # Accept empty MIME (Linux file-manager DnD sends no type for many formats)
    if mime and mime not in ALLOWED_MIME_TYPES:
        return False, f"MIME type '{mime}' is not allowed."
    return True, ""


def validate_explode_input(filename: str, content_type: str) -> tuple[bool, str]:
    """Return (True, '') if the file can be split into frames, else (False, reason)."""
    ext = Path(filename).suffix.lower()
    if ext not in EXPLODE_INPUT_EXTENSIONS:
        return False, (
            f"Cannot extract frames from '{ext}'. "
            "Supported: GIF, WebP, APNG, PNG, TIFF, PDF, MP4, WebM."
        )
    mime = content_type.split(";")[0].strip().lower()
    # Accept empty MIME (Linux file-manager DnD sends no type for many formats)
    if mime and mime not in ALLOWED_MIME_TYPES:
        return False, f"MIME type '{mime}' is not allowed."
    return True, ""


def build_explode_command(input_path: str, out_pattern: str, out_format: str, quality: int) -> list[str]:
    """Build an ImageMagick command to split an animation/multi-frame image into
    individual stills. -coalesce rebuilds each frame as a full composited image
    (not a partial delta), so every extracted frame is complete on its own.
    Used for GIF/WebP/TIFF/PDF inputs; APNG is decoded with ffmpeg instead (its
    frames don't survive ImageMagick's coder cleanly).
    All arguments are list items — never concatenated into a shell string."""
    cmd = ["convert"]
    # -density is a read-time setting: for vector PDF it must precede the input
    # so pages rasterize at a readable resolution instead of the 72-dpi default.
    if Path(input_path).suffix.lower() == ".pdf":
        cmd += ["-density", "150"]
    cmd += [input_path, "-coalesce"]
    if out_format in ("jpg", "webp"):
        cmd += ["-quality", str(quality)]
    cmd.append(out_pattern)
    return cmd


def build_apng_encode_command(frames_input_pattern: str, output: str, delay_cs: int, loop: int,
                              start_number: int = 0) -> list[str]:
    """Assemble numbered PNG frames into a proper animated PNG using ffmpeg.
    ImageMagick-6 cannot write APNG natively and its ffmpeg delegate re-times
    frames (duplicating them); driving ffmpeg directly yields exactly one APNG
    frame per input PNG. Uniform per-frame delay is expressed as a framerate.
    -pix_fmt rgba unifies palettes (APNG forbids per-frame palettes) and keeps
    transparency. -plays 0 loops forever."""
    fps = 100.0 / max(1, delay_cs)
    return [
        "ffmpeg", "-y", "-v", "error",
        "-start_number", str(start_number),
        "-framerate", f"{fps:.6f}",
        "-i", frames_input_pattern,
        "-plays", str(max(0, loop)),
        "-pix_fmt", "rgba",
        "-f", "apng",
        output,
    ]


def build_apng_decode_command(input_path: str, out_pattern: str) -> list[str]:
    """Extract the exact frames of an APNG with ffmpeg (-vsync 0 = passthrough,
    no frame-rate conversion), writing numbered PNGs."""
    return ["ffmpeg", "-y", "-v", "error", "-i", input_path, "-vsync", "0", out_pattern]


def build_video_encode_command(frames_input_pattern: str, output: str, delay_cs: int,
                               fmt: str, start_number: int = 0) -> list[str]:
    """Assemble numbered PNG frames into a video, same two-step model as APNG:
    ImageMagick prepares the frames, ffmpeg encodes them. Uniform per-frame delay
    becomes the input framerate (per-frame delays baked into the prepared frames
    are not expressible in a constant-rate video — see caller). `loop` has no
    video equivalent and is intentionally ignored.

    mp4  → H.264 / yuv420p, padded to even dimensions (H.264 requires even
           width/height); +faststart moves the moov atom up for web playback.
    webm → VP9 / yuva420p (keeps alpha), constant-quality (-b:v 0 -crf 32)."""
    fps = 100.0 / max(1, delay_cs)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-start_number", str(start_number),
        "-framerate", f"{fps:.6f}",
        "-i", frames_input_pattern,
    ]
    if fmt == "webm":
        cmd += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-pix_fmt", "yuva420p"]
    else:  # mp4 / H.264
        cmd += [
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-movflags", "+faststart",
        ]
    cmd.append(output)
    return cmd


def build_assembly_command(
    prep_frames: list[str],
    output: str,
    fmt: str,
    delay_cs: int,
    loop: int,
    quality: int,
    colors: int = 256,
    dither: str = "Riemersma",
    optimize_layers: bool = True,
    frame_delays: list[int] | None = None,
) -> list[str]:
    """Phase-2 of the two-phase pipeline: assemble already-transformed PNG frames
    (produced by build_command in frames_pattern mode) into the final GIF / WebP /
    TIFF / PDF. Applies ONLY timing + palette + quality — never pixel transforms,
    which were already baked into the prep frames. This mirrors build_command's
    non-prep timing/GIF/quality tail exactly, so output matches the old single
    pass. (APNG/MP4/WebM assemble with ffmpeg, not here.)
    All arguments are list items — never concatenated into a shell string."""
    cmd = ["convert"]

    animated = fmt in ("gif", "webp")
    per_frame = animated and frame_delays is not None and len(frame_delays) == len(prep_frames)
    if animated:
        if per_frame:
            cmd += ["-loop", str(loop)]
            for d, f in zip(frame_delays, prep_frames):
                cmd += ["-delay", str(max(1, int(d))), f]
        else:
            cmd += ["-delay", str(delay_cs), "-loop", str(loop)]
            cmd.extend(prep_frames)
    else:  # tiff / pdf: multi-page, no animation timing
        cmd.extend(prep_frames)

    # GIF palette (animated GIF only) — same knobs as build_command
    if fmt == "gif":
        cmd += ["-colors", str(max(2, min(256, colors)))]
        cmd += ["-dither", dither if (dither and dither != "None") else "None"]
        if optimize_layers:
            cmd += ["-layers", "optimize"]

    cmd += ["-quality", str(quality)]
    cmd.append(output)
    return cmd


def build_command(
    frames: list[str],
    output: str,
    fmt: str,
    delay_cs: int,
    loop: int,
    width: int | None,
    height: int | None,
    quality: int,
    # Geometry
    rotate: int = 0,
    flip: bool = False,
    flop: bool = False,
    # Color adjustments
    brightness: int = 100,
    saturation: int = 100,
    hue: int = 100,
    # Color effects
    grayscale: bool = False,
    negate: bool = False,
    sepia: bool = False,
    auto_level: bool = False,
    normalize: bool = False,
    # Sharpness / blur
    sharpen: float = 0.0,
    blur: float = 0.0,
    # GIF-specific
    colors: int = 256,
    dither: str = "Riemersma",
    optimize_layers: bool = True,
    # Output
    strip: bool = False,
    background: str = "",
    # When True, produce a single still (no animation timing, no GIF layering),
    # regardless of fmt. Used by the single-image converter.
    still: bool = False,
    # When set (a "...%05d.png" pattern), apply all transforms but write numbered
    # PNG frames instead of a container file. Used to prepare frames for the
    # ffmpeg APNG encoder. Skips animation timing, GIF options, and quality.
    frames_pattern: str | None = None,
    # Resize behaviour when width/height are given:
    #   "fit"     shrink-only, never enlarge (safest for sharpness) — default
    #   "pad"     exact size, scale to fit then letterbox with background
    #   "crop"    exact size, scale to fill then crop the overflow (no bars)
    #   "stretch" exact size, may distort
    # pad/crop need both width and height; otherwise they fall back to a plain
    # (upscale-allowed) resize to the one given dimension.
    resize_mode: str = "fit",
    # Optional per-frame delays in centiseconds (one per frame). When provided and
    # length-matched, each frame gets its own -delay; otherwise the single global
    # delay_cs is used. Ignored for stills / APNG frame-prep.
    frame_delays: list[int] | None = None,
) -> list[str]:
    """Build an ImageMagick convert command list for any supported output format.
    All arguments appended as list items — never concatenated into a shell string."""

    cmd = ["convert"]

    # Animation timing (applies to animated GIF, WebP, APNG — never for stills/frames)
    animated = frames_pattern is None and not still and fmt in ("gif", "webp", "apng")
    per_frame = animated and frame_delays is not None and len(frame_delays) == len(frames)
    if animated:
        if per_frame:
            cmd += ["-loop", str(loop)]
        else:
            cmd += ["-delay", str(delay_cs), "-loop", str(loop)]

    # Input frames (interleave per-frame delays when supplied)
    if per_frame:
        for d, f in zip(frame_delays, frames):
            cmd += ["-delay", str(max(1, int(d))), f]
    else:
        cmd.extend(frames)

    # ── Geometry ──────────────────────────────
    if rotate and rotate in (90, 180, 270):
        cmd += ["-rotate", str(rotate)]
    if flip:
        cmd.append("-flip")
    if flop:
        cmd.append("-flop")
    if width or height:
        w = str(width) if width else ""
        h = str(height) if height else ""
        geom = f"{w}x{h}"
        both = bool(width) and bool(height)
        if resize_mode == "stretch" and both:
            cmd += ["-resize", geom + "!"]
        elif resize_mode == "crop" and both:
            cmd += ["-resize", geom + "^", "-gravity", "center", "-extent", geom]
        elif resize_mode == "pad" and both:
            cmd += ["-resize", geom, "-gravity", "center",
                    "-background", background or "black", "-extent", geom]
        elif resize_mode in ("pad", "crop", "stretch"):
            # exact mode requested but only one dimension → scale to it (allows upscale)
            cmd += ["-resize", geom]
        else:  # "fit" (default): shrink-only, never enlarge
            cmd += ["-resize", geom + ">"]

    # ── Color effects (applied before adjustments) ─
    if grayscale:
        cmd += ["-colorspace", "Gray"]
    if negate:
        cmd.append("-negate")
    if sepia:
        cmd += ["-sepia-tone", "80%"]
    if auto_level:
        cmd.append("-auto-level")
    if normalize:
        cmd.append("-normalize")

    # ── Brightness / Saturation / Hue ─────────
    if brightness != 100 or saturation != 100 or hue != 100:
        cmd += ["-modulate", f"{brightness},{saturation},{hue}"]

    # ── Sharpen / Blur ────────────────────────
    if sharpen > 0:
        cmd += ["-sharpen", f"0x{sharpen}"]
    if blur > 0:
        cmd += ["-blur", f"0x{blur}"]

    # ── Background ────────────────────────────
    if background:
        cmd += ["-background", background, "-alpha", "remove"]

    # ── GIF-specific (animated GIF only) ──────
    if fmt == "gif" and not still and frames_pattern is None:
        cmd += ["-colors", str(max(2, min(256, colors)))]
        if dither and dither != "None":
            cmd += ["-dither", dither]
        else:
            cmd += ["-dither", "None"]
        if optimize_layers:
            cmd += ["-layers", "optimize"]

    # ── Quality (not meaningful for lossless PNG frame prep) ──
    if frames_pattern is None:
        cmd += ["-quality", str(quality)]

    # ── Strip metadata ────────────────────────
    if strip:
        cmd.append("-strip")

    # ── Output ────────────────────────────────
    if frames_pattern is not None:
        # Frame prep: write numbered PNG frames for a downstream assembler.
        cmd.append(frames_pattern)
    elif fmt == "apng":
        # ImageMagick can't write APNG well; callers route APNG through ffmpeg.
        # Kept for completeness / non-APNG-aware callers.
        cmd.append(f"apng:{output}")
    else:
        cmd.append(output)

    return cmd


# Keep legacy names as thin wrappers so existing callers don't break
def build_gif_command(frames, output, delay_cs, loop, width, height, quality):
    return build_command(frames, output, "gif", delay_cs, loop, width, height, quality)


def build_webp_command(frames, output, delay_cs, loop, width, height, quality):
    return build_command(frames, output, "webp", delay_cs, loop, width, height, quality)
