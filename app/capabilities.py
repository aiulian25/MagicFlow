"""What this deployment can actually do, as opposed to what it offers.

The tables in utils.py are the product's intent: the formats MagicFlow is built
around. What a given image can really read and write depends on three things it
cannot know statically — which tools were installed, what the ImageMagick policy
grants, and whether a format's delegate program exists. The previous code
advertised intent: it offered ten camera RAW types whose decoder is not in the
image, and listed every output whether or not the policy allowed it, so users
chose a format they were told was supported and got a coder error afterwards.

Capability is therefore *discovered*, once at startup, by doing the work on a
few pixels and keeping what succeeded. A smoke test cannot be wrong the way
reading `-list format` can: that listing reports a coder's compiled-in rights,
which the security policy then overrides (the mistake FIX-005 was built on).
Formats with no coder to exercise — camera RAW, which cannot be synthesized —
fall back to checking that the delegate's program exists.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from utils import (
    ALLOWED_EXTENSIONS,
    ICO_SIZES,
    build_command,
    ALLOWED_OUTPUT_FORMATS,
    EXPLODE_INPUT_EXTENSIONS,
    RAW_EXTENSIONS,
    STILL_OUTPUT_EXT,
    STILL_OUTPUT_FORMATS,
)

PROBE_TIMEOUT_S = 30
# Two 4x4 frames: large enough that every encoder (including video, which pads
# to even dimensions) produces a real file, small enough to cost nothing.
PROBE_SIZE = "4x4"
PROBE_FRAMES = 2
PROBE_DELAY_CS = "10"

# The delegate ImageMagick uses to decode camera RAW, and the program it runs.
RAW_DELEGATE = "dng:decode"

# Extensions that identify one animation output format, for mapping an Extract
# input back to the sample written for it.
ANIMATION_OUTPUT_EXT = {
    "gif": ".gif", "webp": ".webp", "apng": ".apng", "tiff": ".tiff",
    "pdf": ".pdf", "mp4": ".mp4", "webm": ".webm",
}

# Input extensions proven by a still-format probe rather than an animation one.
STILL_INPUT_EXT = {
    ".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".webp": "webp",
    ".bmp": "bmp", ".tga": "tga", ".tif": "tiff", ".tiff": "tiff",
    ".heic": "heic", ".heif": "heic", ".avif": "avif",
}

VIDEO_FORMATS = frozenset({"mp4", "webm"})


def _run(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    """True plus empty reason when the command succeeds, else its first line of
    error. Probes never raise: an undiscoverable capability is an unavailable
    one, not a failed startup."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=PROBE_TIMEOUT_S, cwd=str(cwd))
    except Exception as error:  # noqa: BLE001 - reported, never raised
        return False, str(error)[:120]
    if result.returncode == 0:
        return True, ""
    message = (result.stderr or result.stdout).strip().splitlines()
    return False, (message[0][:160] if message else "the tool reported an error")


def _readable(path: Path, cwd: Path) -> tuple[bool, str]:
    """A file the app can read back. Written-but-unreadable is a real state: a
    coder's read and write rights are granted separately."""
    if not path.is_file() or path.stat().st_size == 0:
        return False, "the encoder produced no output"
    if path.suffix.lower() in (".mp4", ".webm"):
        return _run(["ffprobe", "-v", "error", "-show_entries", "stream=width",
                     "-of", "csv=p=0", str(path)], cwd)
    return _run(["identify", "-format", "%w", f"{path}[0]"], cwd)


def _probe_avif_input(cwd: Path) -> tuple[bool, str]:
    source, encoded, decoded = (cwd / name for name in ("avif-alpha.png", "avif-alpha.avif", "avif-decoded.png"))
    for command in (["convert", "-size", "16x16", "xc:none", "-fill", "red", "-draw", "rectangle 4,4 11,11", str(source)],
                    ["avifenc", "-j", "2", "--min", "0", "--max", "0", str(source), str(encoded)],
                    ["identify", "-format", "%w", str(encoded)],
                    ["avifdec", "-j", "2", str(encoded), str(decoded)]):
        ok, reason = _run(command, cwd)
        if not ok:
            return False, "AVIF still/alpha decode probe failed: " + reason
    try:
        result = subprocess.run(["convert", str(decoded), "-alpha", "extract", "-format", "%w %h %[fx:minima] %[fx:maxima]", "info:"], cwd=str(cwd), capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        if result.returncode == 0 and result.stdout.split() == ["16", "16", "0", "1"]:
            return True, ""
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, "AVIF verification failed: " + str(error)[:120]
    return False, "AVIF decoder did not preserve the still's dimensions and alpha"


def _probe_still(fmt: str, cwd: Path) -> tuple[bool, str]:
    target = cwd / f"still_probe{STILL_OUTPUT_EXT[fmt]}"
    if fmt == "ico":
        source = cwd / "icon-alpha.png"
        ok, reason = _run(["convert", "-size", "16x16", "xc:none", "-fill", "red", "-draw", "rectangle 4,4 11,11", str(source)], cwd)
        if not ok:
            return False, "ICO transparency probe could not be prepared: " + reason
        for size in ICO_SIZES:
            command = build_command([str(source)], str(target), "ico", 0, 0, None, None, 90, still=True, icon_size=size)
            ok, reason = _run(command, cwd)
            if not ok:
                return False, f"this build cannot write ICO at {size}px: {reason}"
            try:
                result = subprocess.run(["convert", str(target), "-alpha", "extract", "-format", "%w %h %[fx:minima] %[fx:maxima]", "info:"], cwd=str(cwd), capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
                with target.open("rb") as handle:
                    header = handle.read(6)
            except (OSError, subprocess.TimeoutExpired) as error:
                return False, "ICO verification failed: " + str(error)[:120]
            if result.returncode != 0 or result.stdout.split() != [str(size), str(size), "0", "1"] or header != b"\0\0\1\0\1\0":
                return False, f"ICO at {size}px failed the single-image, dimensions or transparency check"
        return True, ""
    written, reason = _run(["convert", "-size", PROBE_SIZE, "xc:red", str(target)], cwd)
    if not written:
        return False, f"this build cannot write {fmt.upper()}: {reason}"
    readable, reason = _readable(target, cwd)
    return (True, "") if readable else (False, f"this build cannot read {fmt.upper()}: {reason}")


def _probe_animation(fmt: str, source_frames: list[Path], cwd: Path) -> tuple[bool, str]:
    target = cwd / f"anim_probe{ANIMATION_OUTPUT_EXT[fmt]}"
    if fmt in VIDEO_FORMATS:
        command = ["ffmpeg", "-y", "-v", "error", "-framerate", "10",
                   "-i", str(cwd / "frame_%02d.png")]
        command += (["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "40", "-pix_fmt", "yuva420p"]
                    if fmt == "webm" else
                    ["-c:v", "libx264", "-pix_fmt", "yuv420p"])
        command.append(str(target))
    elif fmt == "apng":
        command = ["ffmpeg", "-y", "-v", "error", "-framerate", "10",
                   "-i", str(cwd / "frame_%02d.png"), "-plays", "0", str(target)]
    else:
        command = ["convert", "-delay", PROBE_DELAY_CS,
                   *[str(frame) for frame in source_frames], str(target)]
    written, reason = _run(command, cwd)
    if not written:
        return False, f"this build cannot write {fmt.upper()}: {reason}"
    readable, reason = _readable(target, cwd)
    return (True, "") if readable else (False, f"this build cannot read back {fmt.upper()}: {reason}")


def _probe_raw_decoder() -> tuple[bool, str]:
    """RAW cannot be smoke-tested — a camera file cannot be synthesized — so the
    delegate ImageMagick would invoke is checked instead. ImageMagick lists the
    RAW formats as readable whether or not that program exists, which is exactly
    how this build came to advertise RAW support it does not have."""
    listed, _ = _run(["convert", "-list", "delegate"], Path(tempfile.gettempdir()))
    if not listed:
        return False, "the RAW delegate could not be inspected"
    result = subprocess.run(["convert", "-list", "delegate"],
                            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    program = ""
    for line in result.stdout.splitlines():
        if line.strip().startswith(RAW_DELEGATE):
            _, _, command = line.partition('"')
            program = command.split("'")[0].strip()
            break
    if not program:
        return False, "this build has no camera RAW delegate"
    if not shutil.which(program):
        return False, (f"camera RAW needs '{program}', which is not installed in "
                       f"this image")
    return True, ""


def _write_probe_frames(cwd: Path) -> list[Path]:
    frames = []
    for index in range(PROBE_FRAMES):
        frame = cwd / f"frame_{index:02d}.png"
        # PNG24: forces truecolour, the way _prep_frames writes real frames.
        # A solid-colour PNG would otherwise be written with a palette, which
        # the APNG encoder refuses across frames — an artefact of the probe,
        # not a limitation of the build.
        ok, _ = _run(["convert", "-size", PROBE_SIZE,
                      f"xc:{'red' if index == 0 else 'blue'}",
                      f"PNG24:{frame}"], cwd)
        if ok:
            frames.append(frame)
    return frames


def detect() -> dict:
    """Discover what this deployment supports. Returns the verified sets plus a
    reason for everything withheld, so the UI can explain itself rather than
    silently offering less."""
    unavailable: dict[str, str] = {}
    still_outputs: set[str] = set()
    animation_outputs: set[str] = set()
    explode_inputs: set[str] = set()
    input_extensions: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="magicflow-capabilities-") as work_name:
        work = Path(work_name)
        frames = _write_probe_frames(work)

        for fmt in sorted(STILL_OUTPUT_FORMATS):
            ok, reason = _probe_still(fmt, work)
            if ok:
                still_outputs.add(fmt)
            else:
                unavailable[f"still:{fmt}"] = reason

        for fmt in sorted(ALLOWED_OUTPUT_FORMATS):
            ok, reason = _probe_animation(fmt, frames, work)
            if ok:
                animation_outputs.add(fmt)
                extension = ANIMATION_OUTPUT_EXT[fmt]
                if extension in EXPLODE_INPUT_EXTENSIONS:
                    explode_inputs.add(extension)
                input_extensions.add(extension)
            else:
                unavailable[f"animation:{fmt}"] = reason

        avif_ok, avif_reason = _probe_avif_input(work)
        raw_ok, raw_reason = _probe_raw_decoder()

    # An input is available when the probe proved its format readable. The still
    # probes cover the plain raster inputs; the animation probes above covered
    # the animated and multi-page ones.
    for extension, fmt in STILL_INPUT_EXT.items():
        if extension == ".avif":
            if avif_ok:
                input_extensions.add(extension)
            continue
        if fmt in still_outputs and extension in ALLOWED_EXTENSIONS:
            input_extensions.add(extension)
        if fmt in still_outputs and extension in EXPLODE_INPUT_EXTENSIONS:
            explode_inputs.add(extension)

    # PSD is read-only here: ImageMagick reads layered files but the app never
    # writes one, so it rides on the TIFF-class coder probe being healthy.
    if ".psd" in ALLOWED_EXTENSIONS and "tiff" in still_outputs:
        input_extensions.add(".psd")

    if raw_ok:
        input_extensions |= {e for e in RAW_EXTENSIONS if e in ALLOWED_EXTENSIONS}
    else:
        unavailable["input:raw"] = raw_reason

    if ".avif" not in input_extensions:
        unavailable["input:.avif"] = avif_reason
    missing_inputs = sorted(ALLOWED_EXTENSIONS - input_extensions - RAW_EXTENSIONS)
    for extension in missing_inputs:
        unavailable.setdefault(f"input:{extension}",
                               "this build cannot decode this format")

    return {
        "still_outputs": sorted(still_outputs),
        "animation_outputs": sorted(animation_outputs),
        "explode_inputs": sorted(explode_inputs),
        "input_extensions": sorted(input_extensions),
        "raw_available": raw_ok,
        "unavailable": unavailable,
    }
