import re
from pathlib import Path

# ── Input allow-lists ────────────────────────────────────────────────────────
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    # Common web/raster
    ".jpg", ".jpeg", ".png", ".apng", ".webp", ".gif", ".bmp", ".tga", ".avif",
    # TIFF / PSD
    ".tif", ".tiff", ".psd",
    # HEIC/HEIF
    ".heic", ".heif",
    # Camera RAW
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw",
})

ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/avif", "image/png", "image/apng", "image/webp", "image/gif", "image/bmp",
    "image/tiff", "image/heic", "image/heif",
    "image/x-tga", "image/x-tiff",
    # Photoshop: browsers and file managers report any of these for a .psd,
    # and without them a PSD was rejected despite the extension being accepted.
    "image/vnd.adobe.photoshop", "application/x-photoshop", "image/psd",
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

# Processing policy 1: SDR sRGB pixels, orientation baked before user transforms.
PROCESSING_POLICY = "sdr-srgb-v1"
SRGB_PROFILE = "/usr/share/color/icc/ghostscript/srgb.icc"

def color_normalization_args() -> list[str]:
    return ["-auto-orient", "-profile", SRGB_PROFILE, "-colorspace", "sRGB", "-type", "TrueColorAlpha"]

# ── Output formats ───────────────────────────────────────────────────────────
ALLOWED_OUTPUT_FORMATS: frozenset[str] = frozenset({"gif", "webp", "apng", "tiff", "pdf", "mp4", "webm"})
ANIMATION_CANVAS_FORMATS: frozenset[str] = frozenset({"gif", "webp", "apng", "mp4", "webm"})
FFMPEG_FRAME_CODERS = {"apng": "PNG32", "webm": "PNG32", "mp4": "PNG24"}


def canvas_background(fmt: str, background: str = "") -> str:
    """Animations keep transparent padding; opaque MP4 defaults to black."""
    return background or ("black" if fmt == "mp4" else "none")


def build_canvas_command(source: str, output: str, width: int, height: int,
                         fmt: str, background: str = "") -> list[str]:
    """Center a transformed frame without scaling/cropping its pixels.

    MP4's extra even-size row/column belongs on the bottom/right, after
    centering on the shared canvas. Explicitly flatten alpha before YUV.
    """
    cmd = ["convert", source, "+repage", "-background", canvas_background(fmt, background),
           "-gravity", "center", "-extent", f"{width}x{height}"]
    if fmt == "mp4":
        cmd += ["-gravity", "northwest", "-extent", f"{width + width % 2}x{height + height % 2}",
                "-alpha", "remove", "-alpha", "off"]
    cmd += ["-depth", "8", ("PNG24:" if fmt == "mp4" else "PNG32:") + output]
    return cmd

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
    ".ico": "image/vnd.microsoft.icon",
}

OUTPUT_EXTENSIONS: set[str] = set(OUTPUT_MEDIA_TYPES.keys())

# ImageMagick 6.9.11's animated-WebP writer silently emits a 0-byte file (and
# still exits 0) when the FIRST frame carries an ICC or XMP profile — e.g. any
# GIMP/camera JPEG. Dropping every profile before the write avoids it.
# Policy 1 converts tagged pixels to sRGB before this workaround; removing
# profiles here no longer reinterprets wide-gamut source pixels.
ANIMATED_WEBP_PROFILE_STRIP: list[str] = ["+profile", "*"]

# ── Single still → still conversion ──────────────────────────────────────────
# Output formats for the image converter. All verified read/write in the
# container's ImageMagick build (libheif gives AVIF/HEIC, libwebp gives WebP).
STILL_OUTPUT_FORMATS: frozenset[str] = frozenset({
    "jpg", "png", "webp", "avif", "heic", "tiff", "bmp", "tga", "ico",
})
STILL_OUTPUT_EXT: dict[str, str] = {
    "jpg": ".jpg", "png": ".png", "webp": ".webp", "avif": ".avif",
    "heic": ".heic", "tiff": ".tiff", "bmp": ".bmp", "tga": ".tga", "ico": ".ico",
}

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


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


# ── Content detection ────────────────────────────────────────────────────────
# An APNG is a PNG that carries an animation control chunk, so its extension may
# be .apng OR .png and `identify` reports it as a single-frame PNG either way.
# Only the bytes can say, and only the bytes are trusted here: dispatching on
# the extension is what made the same file decode to three frames or one
# depending on what it was called.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
APNG_ANIMATION_CHUNK = b"acTL"
APNG_FIRST_FRAME_CHUNK = b"IDAT"
# acTL must precede IDAT, and the chunks before it are small, so a short read
# settles it without pulling a large file into memory.
APNG_DETECTION_BYTES = 64 * 1024


def is_apng(path: str | Path) -> bool:
    """True when these bytes are an animated PNG, whatever the file is called."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(APNG_DETECTION_BYTES)
    except OSError:
        return False
    if not head.startswith(PNG_SIGNATURE):
        return False
    animation = head.find(APNG_ANIMATION_CHUNK)
    if animation < 0:
        return False
    first_frame = head.find(APNG_FIRST_FRAME_CHUNK)
    # A valid APNG declares the animation before the first frame's data; acTL
    # appearing later is not animation metadata.
    return first_frame < 0 or animation < first_frame


def validate_avif_still(path: str | Path) -> None:
    """Bounded BMFF metadata check for the initial SDR, single-image AVIF scope.

    Native decoding still validates the AV1 payload. Do not silently treat an
    AVIF sequence as one still, or reinterpret declared PQ/HLG as SDR.
    """
    with open(path, "rb") as handle:
        size = handle.seek(0, 2)
        boxes_seen = 0
        found_type = False
        def boxes(start, end, depth=0):
            nonlocal boxes_seen, found_type
            cursor = start
            while cursor < end:
                boxes_seen += 1
                if boxes_seen > 4096 or end - cursor < 8:
                    raise ValueError("Unsupported or truncated AVIF metadata.")
                handle.seek(cursor)
                header = handle.read(8)
                length, kind = int.from_bytes(header[:4], "big"), header[4:]
                header_size = 8
                if length == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        raise ValueError("Truncated AVIF box.")
                    length = int.from_bytes(extended, "big");header_size = 16
                elif length == 0:
                    length = end - cursor
                if length < header_size or cursor + length > end:
                    raise ValueError("Truncated or invalid AVIF box.")
                payload = cursor + header_size
                if depth == 0 and kind == b"ftyp":
                    if length - header_size < 8 or length - header_size > 1024 * 1024 or (length - header_size) % 4:
                        raise ValueError("Unsupported AVIF file type metadata.")
                    data = handle.read(length - header_size)
                    brands = {data[:4], *[data[i:i+4] for i in range(8, len(data), 4)]}
                    if b"avis" in brands:
                        raise ValueError("AVIF sequences are unsupported; use one still image.")
                    if b"avif" not in brands:
                        raise ValueError("This file is not a supported AVIF still.")
                    found_type = True
                elif depth == 0 and kind == b"moov":
                    raise ValueError("AVIF tracks/sequences are unsupported; use one still image.")
                elif kind == b"meta" and depth == 0:
                    handle.seek(payload)
                    if handle.read(4) != b"\0\0\0\0":
                        raise ValueError("Unsupported AVIF metadata version.")
                    boxes(payload + 4, cursor + length, 1)
                elif (depth == 1 and kind == b"iprp") or (depth == 2 and kind == b"ipco"):
                    boxes(payload, cursor + length, depth + 1)
                elif depth == 3 and kind == b"colr":
                    handle.seek(payload);color = handle.read(min(length - header_size, 11))
                    if color[:4] == b"nclx":
                        if len(color) < 11:
                            raise ValueError("Truncated AVIF color metadata.")
                        if int.from_bytes(color[6:8], "big") in (16, 18):
                            raise ValueError("HDR AVIF is unsupported; convert to SDR sRGB first.")
                        # avifdec preserves ICC, but does not turn arbitrary
                        # NCLX primaries/transfer functions into a PNG ICC profile.
                        if int.from_bytes(color[4:6], "big") not in (1, 2) or int.from_bytes(color[6:8], "big") not in (2, 13):
                            raise ValueError("This AVIF color space is unsupported; convert to SDR sRGB first.")
                cursor += length
        boxes(0, size)
        if not found_type:
            raise ValueError("Missing AVIF file type metadata.")


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
            "Accepted: JPG, PNG, APNG, WebP, GIF, BMP, TGA, TIFF, PSD, HEIC, AVIF, RAW (CR2/NEF/ARW/DNG…)"
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
    cmd += [input_path, "-coalesce", *color_normalization_args()]
    if out_format == "jpg":
        cmd += ["-background", "white", "-alpha", "remove", "-alpha", "off"]
    if out_format in ("jpg", "webp"):
        cmd += ["-quality", str(quality)]
    cmd.append(out_pattern)
    return cmd


# ── Timing ───────────────────────────────────────────────────────────────────
# A uniform framerate is an implementation choice, not a property of these
# formats: APNG gives every frame its own delay in its fcTL chunk, and MP4/WebM
# carry per-frame durations. Feeding ffmpeg one framerate is what flattened an
# uneven rhythm to its average without telling anyone. The concat demuxer below
# states each frame's duration instead.
CONCAT_LIST_NAME = "timeline.txt"

# The concat demuxer reads each still through a nested image demuxer whose
# default rate is 25fps, and every duration is then snapped to that 40ms grid —
# 100/300/200ms came out as 120/280/200. This raises that grid to 1ms. It is a
# timebase resolution, not a playback rate: the durations below still decide
# how long each frame is shown.
CONCAT_FRAMERATE_HINT = 1000


def build_concat_list(frame_paths: list[str], delays_cs: list[int]) -> str:
    """An ffmpeg concat-demuxer list giving each frame its own duration.

    The final frame is NOT repeated. Repeating it is the usual trick for giving
    it a duration, but ffmpeg then writes it as a real extra frame — a four
    frame file for three pictures. The last frame's duration is set at the
    muxer instead (see build_apng_encode_command's final_delay)."""
    lines = []
    for path, delay in zip(frame_paths, delays_cs):
        lines.append(f"file '{path}'")
        lines.append(f"option framerate {CONCAT_FRAMERATE_HINT}")
        lines.append(f"duration {max(1, delay) / 100:.4f}")
    return "\n".join(lines) + "\n"


def _concat_input(concat_file: str) -> list[str]:
    """-safe 0 because the list holds absolute paths from the job's scratch."""
    return ["-f", "concat", "-safe", "0", "-i", concat_file]


def build_apng_encode_command(frames_input_pattern: str, output: str, delay_cs: int, loop: int,
                              start_number: int = 0,
                              concat_file: str | None = None,
                              final_delay_cs: int | None = None) -> list[str]:
    """Assemble numbered PNG frames into a proper animated PNG using ffmpeg.
    ImageMagick-6 cannot write APNG natively and its ffmpeg delegate re-times
    frames (duplicating them); driving ffmpeg directly yields exactly one APNG
    frame per input PNG. -pix_fmt rgba unifies palettes (APNG forbids per-frame
    palettes) and keeps transparency. -plays 0 loops forever.

    With `concat_file` each frame keeps its own duration; without it the uniform
    delay becomes the input framerate, as before. `final_delay_cs` sets how long
    the LAST frame is shown: it has no following frame to measure against, so
    without it the muxer repeats the previous frame's delay."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if concat_file:
        cmd += _concat_input(concat_file)
    else:
        fps = 100.0 / max(1, delay_cs)
        cmd += ["-start_number", str(start_number), "-framerate", f"{fps:.6f}",
                "-i", frames_input_pattern]
    cmd += ["-plays", str(max(0, loop)), "-pix_fmt", "rgba",
            "-fps_mode", "passthrough"]
    if final_delay_cs:
        cmd += ["-final_delay", f"{max(1, final_delay_cs) / 100:.4f}"]
    cmd += ["-f", "apng", output]
    return cmd


def build_apng_decode_command(input_path: str, out_pattern: str) -> list[str]:
    """Extract the exact frames of an APNG with ffmpeg (-vsync 0 = passthrough,
    no frame-rate conversion), writing numbered PNGs.

    -start_number 0 because ffmpeg otherwise numbers from 1 while the API's
    frame selection is zero-based: the same "frame 0" then meant two different
    pictures depending on which decoder had read the file."""
    return ["ffmpeg", "-y", "-v", "error", "-i", input_path, "-vsync", "0",
            "-start_number", "0", out_pattern]


# ── Video quality ────────────────────────────────────────────────────────────
# The Quality control is shown and recorded for every format, but video ignored
# it: VP9 was pinned at CRF 32 and H.264 took the encoder default. Quality now
# maps onto each encoder's constant-rate factor, where LOWER is better.
# The map is anchored so that the default quality still produces exactly what
# this app produced before, and only other values change anything.
DEFAULT_QUALITY = 85  # the Form default on /convert and /reconvert
VIDEO_CRF_AT_DEFAULT = {"webm": 32, "mp4": 23}  # today's output
VIDEO_CRF_BEST = {"webm": 15, "mp4": 17}        # quality 100
VIDEO_CRF_WORST = {"webm": 63, "mp4": 51}       # quality 0, each encoder's floor


def video_crf_for_quality(fmt: str, quality: int) -> int:
    """The constant-rate factor for a 1–100 quality, interpolated through the
    anchor so `quality == DEFAULT_QUALITY` reproduces the previous encode."""
    quality = min(100, max(1, quality))
    anchor = VIDEO_CRF_AT_DEFAULT[fmt]
    if quality <= DEFAULT_QUALITY:
        worst = VIDEO_CRF_WORST[fmt]
        return round(worst - (worst - anchor) * quality / DEFAULT_QUALITY)
    best = VIDEO_CRF_BEST[fmt]
    span = 100 - DEFAULT_QUALITY
    return round(anchor - (anchor - best) * (quality - DEFAULT_QUALITY) / span)


def build_video_encode_command(frames_input_pattern: str, output: str, delay_cs: int,
                               fmt: str, start_number: int = 0,
                               quality: int = DEFAULT_QUALITY,
                               concat_file: str | None = None) -> list[str]:
    """Assemble numbered PNG frames into a video, same two-step model as APNG:
    ImageMagick prepares the frames, ffmpeg encodes them. Uniform per-frame delay
    becomes the input framerate (per-frame delays baked into the prepared frames
    are not expressible in a constant-rate video — see caller). `loop` has no
    video equivalent and is intentionally ignored.

    mp4  → H.264 / yuv420p, padded to even dimensions (H.264 requires even
           width/height); +faststart moves the moov atom up for web playback.
    webm → VP9 / yuva420p (encodes alpha; decoder support varies), constant-quality (-b:v 0 -crf N).

    Both encoders take the requested quality as a constant-rate factor; see
    video_crf_for_quality for the mapping and why the default is unchanged."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if concat_file:
        # Variable timing: each frame's duration comes from the list, and
        # passthrough stops ffmpeg resampling it back to a constant rate.
        cmd += _concat_input(concat_file) + ["-fps_mode", "passthrough"]
    else:
        fps = 100.0 / max(1, delay_cs)
        cmd += ["-start_number", str(start_number), "-framerate", f"{fps:.6f}",
                "-i", frames_input_pattern]
    crf = str(video_crf_for_quality(fmt, quality))
    if fmt == "webm":
        cmd += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", crf, "-pix_fmt", "yuva420p"]
    else:  # mp4 / H.264
        cmd += [
            "-c:v", "libx264", "-crf", crf, "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,scale=out_color_matrix=bt709:out_range=tv",
            "-movflags", "+faststart",
        ]
    if fmt == "webm":
        cmd += ["-vf", "scale=out_color_matrix=bt709:out_range=tv"]
    cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "iec61966-2-1", "-color_range", "tv"]
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

    if fmt == "webp":
        cmd += ANIMATED_WEBP_PROFILE_STRIP

    cmd += ["-quality", str(quality)]
    if fmt == "tiff":
        cmd += ["-compress", "LZW"]
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
    icon_size: int = 256,
) -> list[str]:
    """Build an ImageMagick convert command list for any supported output format.
    All arguments appended as list items — never concatenated into a shell string."""

    if fmt == "ico" and (not still or len(frames) != 1 or frames_pattern is not None or icon_size not in ICO_SIZES):
        raise ValueError("ICO needs one still source and a supported square icon size.")
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

    cmd += color_normalization_args()

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
                    "-background", (canvas_background(fmt, background)
                                    if not still and fmt in ANIMATION_CANVAS_FORMATS
                                    else background or "black"), "-extent", geom]
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

    # Restore the sRGB working space after effects; coders may compact neutral samples.
    cmd += ["-colorspace", "sRGB", "-type", "TrueColorAlpha"]

    if fmt in ("jpg", "jpeg", "bmp") and still:
        cmd += ["-background", background or "white", "-alpha", "remove", "-alpha", "off"]

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
    if frames_pattern is None and fmt != "ico":
        cmd += ["-quality", str(quality)]

    # ── Strip metadata ────────────────────────
    if strip:
        cmd.append("-strip")
    if animated and fmt == "webp":
        cmd += ANIMATED_WEBP_PROFILE_STRIP

    if fmt == "tiff" and frames_pattern is None:
        cmd += ["-compress", "LZW"]

    if fmt == "ico":
        cmd += ["-resize", f"{icon_size}x{icon_size}", "-background", "none", "-gravity", "center",
                "-extent", f"{icon_size}x{icon_size}", "-depth", "8"]

    # ── Output ────────────────────────────────
    if frames_pattern is not None:
        # Frame prep: write numbered PNG frames for a downstream assembler.
        # A PNG sequence must not switch between palette, gray, RGB and RGBA
        # under ffmpeg's image demuxer. Match the final encoder's input contract.
        if fmt in FFMPEG_FRAME_CODERS:
            if fmt == "mp4":
                cmd += ["-background", canvas_background(fmt, background), "-alpha", "remove", "-alpha", "off"]
            cmd += ["+profile", "*", "-depth", "8"]
            cmd.append(FFMPEG_FRAME_CODERS[fmt] + ":" + frames_pattern)
        else:
            cmd.append(frames_pattern)
    elif fmt == "ico":
        cmd.append("ICO:" + output)
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
