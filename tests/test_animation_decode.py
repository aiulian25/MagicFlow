"""Regression coverage for FIX-009: every entry point decodes an animation into
the same full canonical frames before anything transforms them.

Needs the app's runtime and the media tools, so it runs inside the container
with the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_animation_decode.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, "/app")

import main  # noqa: E402
import utils  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_DECODE_TEST_PORT", "8925"))
BASE = f"http://{HOST}:{PORT}"

FIXTURES = Path("/data/tmp/fix009-fixtures")
FRAME_COUNT = 3
# The canvas, and the small square that is all that moves. A constant
# background is what makes an optimizer emit delta rectangles rather than
# full frames.
CANVAS = "120x90"
RESIZE_TO = ("60", "45")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def build_fixtures():
    """Three frames, then the containers a real tool would write from them."""
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True)
    for index in range(FRAME_COUNT):
        run(["convert", "-size", CANVAS, "xc:navy", "-fill", "white", "-draw",
             f"rectangle {10 + index * 30},10 {40 + index * 30},40",
             f"PNG24:{FIXTURES}/frame_{index:02d}.png"])
    sources = [str(FIXTURES / f"frame_{i:02d}.png") for i in range(FRAME_COUNT)]
    run(["ffmpeg", "-y", "-v", "error", "-framerate", "10",
         "-i", str(FIXTURES / "frame_%02d.png"), "-plays", "0",
         str(FIXTURES / "anim.apng")])
    # The same bytes under a .png name: tools do write APNGs that way, and the
    # extension is all the old dispatch had to go on.
    (FIXTURES / "anim_named.png").write_bytes((FIXTURES / "anim.apng").read_bytes())
    run(["convert", "-delay", "10", *sources, "-layers", "optimize",
         str(FIXTURES / "delta.gif")])
    # The same animation as full canvases: it renders identically, so the app
    # must produce identical output from either.
    run(["convert", "-delay", "10", *sources, "-coalesce", str(FIXTURES / "full.gif")])
    run(["convert", *sources, str(FIXTURES / "layers.psd")])


def post(path, fields, files=(), timeout=300):
    boundary = "mfbound" + uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{key}"\r\n\r\n{value}\r\n').encode()
    for key, filename, data, content_type in files:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{key}"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(BASE + path, method="POST", data=body)
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, response.read(), headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), {k.lower(): v for k, v in error.headers.items()}


def post_json(path, fields, files=(), timeout=300):
    status, raw, _ = post(path, fields, files, timeout)
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {"detail": raw[:200].decode("utf-8", "replace")}


def decoded_frames(path, into):
    """Frames of a produced animation, decoded with ffmpeg. ImageMagick would
    return only the first frame of an APNG — the limitation under test."""
    into.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vsync", "0",
         str(into / "f_%03d.png")])
    return sorted(into.glob("f_*.png"))


def differing_pixels(left, right):
    result = run(["compare", "-metric", "AE", str(left), str(right), "null:"])
    text = (result.stderr or "").strip().split()
    return text[0] if text else "?"


class DetectionTest(unittest.TestCase):
    """Deciding what a file is, from the file rather than its name."""

    @classmethod
    def setUpClass(cls):
        build_fixtures()

    def test_an_apng_is_recognised_under_either_extension(self):
        self.assertTrue(utils.is_apng(FIXTURES / "anim.apng"))
        self.assertTrue(utils.is_apng(FIXTURES / "anim_named.png"),
                        "the same bytes must be recognised when called .png")

    def test_a_still_png_is_not_mistaken_for_an_animation(self):
        self.assertFalse(utils.is_apng(FIXTURES / "frame_00.png"))

    def test_detection_survives_files_that_are_not_png_at_all(self):
        for name in ("delta.gif", "layers.psd"):
            with self.subTest(name=name):
                self.assertFalse(utils.is_apng(FIXTURES / name))
        self.assertFalse(utils.is_apng(FIXTURES / "does-not-exist.png"))

    def test_identify_alone_would_have_got_this_wrong(self):
        """Why detection reads bytes: the tool the app used to ask reports an
        APNG as a single-frame PNG."""
        self.assertEqual(main._probe_source(str(FIXTURES / "anim.apng"))[0], 1)
        self.assertEqual(main._probe_frames_and_size(FIXTURES / "anim.apng")[0],
                         FRAME_COUNT)

    def test_a_psd_is_measured_as_its_composite_not_its_layers(self):
        stored_layers = main._probe_source(str(FIXTURES / "layers.psd"))[0]
        self.assertGreater(stored_layers, 1, "fixture should have several layers")
        self.assertEqual(main._probe_frames_and_size(FIXTURES / "layers.psd")[0], 1)

    def test_the_decoder_routes_an_apng_to_ffmpeg_whatever_it_is_called(self):
        for name in ("anim.apng", "anim_named.png"):
            with self.subTest(name=name):
                command = main._decode_command(FIXTURES / name, "out_%05d.png")
                self.assertEqual(command[0], "ffmpeg", command)

    def test_the_decoder_coalesces_anything_that_can_carry_delta_frames(self):
        command = main._decode_command(FIXTURES / "delta.gif", "out_%05d.png")
        self.assertIn("-coalesce", command)

    def test_the_decoder_reads_only_a_psds_composite(self):
        command = main._decode_command(FIXTURES / "layers.psd", "out_%05d.png")
        self.assertTrue(any(str(arg).endswith("[0]") for arg in command), command)
        self.assertNotIn("-coalesce", command)

    def test_a_plain_still_is_not_decoded_at_all(self):
        """The common case must stay at one pass: decoding every JPEG first
        would double the work of an ordinary Combine."""
        self.assertFalse(main._needs_canonical_decode(FIXTURES / "frame_00.png"))
        for name in ("anim.apng", "anim_named.png", "delta.gif", "layers.psd"):
            with self.subTest(name=name):
                self.assertTrue(main._needs_canonical_decode(FIXTURES / name))


class PipelineTest(unittest.TestCase):
    """What each entry point actually produces."""

    @classmethod
    def setUpClass(cls):
        import uvicorn

        build_fixtures()
        for directory in (main.INPUT_DIR, main.OUTPUT_DIR, main.TMP_DIR,
                          main.CONFIG_DIR, main.THUMB_DIR, main.RECIPE_DIR,
                          main.STAGING_DIR):
            directory.mkdir(parents=True, exist_ok=True)

        cls._server = uvicorn.Server(uvicorn.Config(
            main.app, host=HOST, port=PORT, log_level="warning"))
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(BASE + "/health", timeout=5)
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("test server did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls._server.should_exit = True
        cls._thread.join(timeout=15)

    def setUp(self):
        self.before = {f.name for f in main.OUTPUT_DIR.iterdir() if f.is_file()}
        self.work = Path("/data/tmp") / f"decode-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        for leftover in {f.name for f in main.OUTPUT_DIR.iterdir()
                         if f.is_file()} - self.before:
            (main.OUTPUT_DIR / leftover).unlink(missing_ok=True)
            (main.RECIPE_DIR / (leftover + ".json")).unlink(missing_ok=True)
        shutil.rmtree(self.work, ignore_errors=True)

    def combine(self, name, mime="image/png", fmt="apng", **extra):
        fields = {"format": fmt, "keep_output": "true", "delay": "10",
                  "custom_name": f"decoded_{uuid.uuid4().hex[:8]}"}
        fields.update(extra)
        status, payload = post_json(
            "/convert", fields,
            [("files", name, (FIXTURES / name).read_bytes(), mime)])
        self.assertEqual(status, 200, payload)
        return main.OUTPUT_DIR / payload["output_filename"]

    def test_combine_keeps_every_frame_of_an_apng(self):
        """The reproduced defect: a three-frame APNG came out as one frame,
        because ImageMagick's PNG coder never saw the animation."""
        produced = self.combine("anim.apng", mime="image/apng")
        self.assertEqual(len(decoded_frames(produced, self.work)), FRAME_COUNT)

    def test_an_apng_named_png_decodes_exactly_like_one_named_apng(self):
        from_apng = decoded_frames(self.combine("anim.apng", mime="image/apng"),
                                   self.work / "a")
        from_png = decoded_frames(self.combine("anim_named.png"), self.work / "b")
        self.assertEqual(len(from_apng), len(from_png))
        self.assertEqual(len(from_apng), FRAME_COUNT)
        for left, right in zip(from_apng, from_png):
            with self.subTest(frame=left.name):
                self.assertEqual(differing_pixels(left, right), "0")

    def test_extract_gives_the_same_frames_under_either_extension(self):
        counts = {}
        for name in ("anim.apng", "anim_named.png"):
            status, _, headers = post("/explode", {"out_format": "png"},
                                      [("file", name, (FIXTURES / name).read_bytes(),
                                        "image/png")])
            self.assertEqual(status, 200, name)
            counts[name] = int(headers.get("x-frame-count", 0))
        self.assertEqual(counts["anim.apng"], FRAME_COUNT)
        self.assertEqual(counts["anim_named.png"], counts["anim.apng"])

    def test_an_optimized_source_survives_a_resize_like_a_full_frame_one(self):
        """The reproduced defect: a resize scaled each delta rectangle and its
        offset independently, so only the first full frame came out right.

        Flips and rotations do NOT show this — ImageMagick coalesces internally
        for those — which is why the check resizes, the commonest setting."""
        width, height = RESIZE_TO
        from_delta = decoded_frames(
            self.combine("delta.gif", "image/gif", width=width, height=height,
                         resize_mode="stretch"), self.work / "delta")
        from_full = decoded_frames(
            self.combine("full.gif", "image/gif", width=width, height=height,
                         resize_mode="stretch"), self.work / "full")
        self.assertEqual(len(from_delta), len(from_full))
        self.assertEqual(len(from_delta), FRAME_COUNT)
        differences = [differing_pixels(a, b) for a, b in zip(from_full, from_delta)]
        self.assertEqual(differences, ["0"] * FRAME_COUNT,
                         f"optimized and full-frame sources diverged: {differences}")

    def test_a_layered_psd_contributes_one_composite_frame(self):
        """The reproduced defect: a PSD's layers were concatenated as though
        they were moments in time."""
        produced = self.combine("layers.psd", "image/vnd.adobe.photoshop")
        self.assertEqual(len(decoded_frames(produced, self.work)), 1)

    def test_an_ordinary_still_combine_is_unchanged(self):
        """Nothing was lost to the new decode stage."""
        fields = {"format": "gif", "keep_output": "true", "delay": "10",
                  "custom_name": f"stills_{uuid.uuid4().hex[:8]}"}
        status, payload = post_json(
            "/convert", fields,
            [("files", f"frame_{i:02d}.png",
              (FIXTURES / f"frame_{i:02d}.png").read_bytes(), "image/png")
             for i in range(FRAME_COUNT)])
        self.assertEqual(status, 200, payload)
        produced = main.OUTPUT_DIR / payload["output_filename"]
        self.assertEqual(main._probe_source(str(produced))[0], FRAME_COUNT)

    def test_a_multi_page_tiff_is_still_treated_as_frames(self):
        """Pages are deliberately frames; only PSD layers are not. This is the
        neighbouring contract the PSD change must not disturb."""
        pages = FIXTURES / "pages.tiff"
        run(["convert", *[str(FIXTURES / f"frame_{i:02d}.png") for i in range(FRAME_COUNT)],
             str(pages)])
        self.assertEqual(main._probe_frames_and_size(pages)[0], FRAME_COUNT)
        self.assertNotIn("[0]", " ".join(main._decode_command(pages, "o_%05d.png")))

    def test_inspect_reports_the_frames_a_conversion_will_produce(self):
        for name, expected in (("anim_named.png", FRAME_COUNT),
                               ("anim.apng", FRAME_COUNT),
                               ("layers.psd", 1),
                               ("delta.gif", FRAME_COUNT)):
            with self.subTest(name=name):
                status, payload = post_json(
                    "/inspect", {},
                    [("file", name, (FIXTURES / name).read_bytes(), "image/png")])
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["frames"], expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
