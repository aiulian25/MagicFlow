"""Regression coverage for FIX-008: the app offers what this build can actually
do, its two validators agree, and the Quality control it shows for video works.

Needs the app's runtime and the media tools, so it runs inside the container
with the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_capabilities.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import json
import os
import shutil
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

sys.path.insert(0, "/app")

import capabilities  # noqa: E402
import main  # noqa: E402
import utils  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_CAPABILITY_TEST_PORT", "8919"))
BASE = f"http://{HOST}:{PORT}"

PSD_MIME_TYPES = ("image/vnd.adobe.photoshop", "application/x-photoshop", "image/psd")
# MIME types browsers and file managers have been seen to report for camera RAW.
RAW_MIME_TYPES = ("application/octet-stream", "image/x-canon-cr2", "image/x-nikon-nef", "")

CLIENT_PAGE = Path("/app/static/index.html")

# Quality values whose encodes must differ materially if the control works.
LOW_QUALITY, HIGH_QUALITY = 5, 100


def tiny_png(width=16, height=16, rgb=(10, 200, 10)):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def noisy_png(width=200, height=200, seed=1):
    """Detailed frames: flat colour compresses to nothing at any quality, so it
    cannot show whether a quality setting reached the encoder."""
    state = seed
    rows = []
    for _ in range(height):
        row = bytearray(b"\x00")
        for _ in range(width):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            row += bytes(((state >> 16) & 0xFF, (state >> 8) & 0xFF, state & 0xFF))
        rows.append(bytes(row))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows)))
            + chunk(b"IEND", b""))


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
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except ValueError:
            return error.code, {"detail": raw[:200].decode("utf-8", "replace")}


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read())


class DiscoveryTest(unittest.TestCase):
    """What detect() finds, and what it does when a tool or coder is missing."""

    @classmethod
    def setUpClass(cls):
        cls.found = capabilities.detect()

    def test_this_build_reports_the_formats_it_can_really_produce(self):
        """Every format the product offers and this image can do must survive
        discovery — a probe that under-reports is as wrong as one that
        over-reports."""
        self.assertEqual(set(self.found["animation_outputs"]), set(utils.ALLOWED_OUTPUT_FORMATS))
        self.assertEqual(set(self.found["still_outputs"]), set(utils.STILL_OUTPUT_FORMATS))
        self.assertEqual(set(self.found["explode_inputs"]), set(utils.EXPLODE_INPUT_EXTENSIONS))

    def test_apng_survives_discovery(self):
        """A regression guard with a story: the first probe wrote palette PNGs,
        which the APNG encoder refuses across frames, and APNG was wrongly
        reported unavailable. Real frames are truecolour."""
        self.assertIn("apng", self.found["animation_outputs"])

    def test_raw_is_not_offered_without_its_decoder(self):
        """The reproduced defect: ten RAW types advertised, and the delegate
        program ImageMagick would run is not installed."""
        raw_offered = set(self.found["input_extensions"]) & set(utils.RAW_EXTENSIONS)
        if self.found["raw_available"]:
            self.assertTrue(raw_offered)
            return
        self.assertEqual(raw_offered, set(), "RAW offered with no decoder")
        self.assertIn("input:raw", self.found["unavailable"])
        self.assertIn("not installed", self.found["unavailable"]["input:raw"])

    def test_a_missing_delegate_program_is_reported_not_assumed(self):
        original = shutil.which
        shutil.which = lambda _program: None
        try:
            available, reason = capabilities._probe_raw_decoder()
        finally:
            shutil.which = original
        self.assertFalse(available)
        self.assertTrue(reason)

    def test_a_format_whose_encoder_fails_is_withheld_with_its_reason(self):
        """Stands in for a tightened policy or a missing codec: the probe is the
        only thing that decides, so making it fail must remove the format."""
        original = capabilities._run

        def failing(cmd, cwd):
            if any(str(arg).endswith(".webp") for arg in cmd):
                return False, "attempt to perform an operation not allowed by the security policy"
            return original(cmd, cwd)

        capabilities._run = failing
        try:
            found = capabilities.detect()
        finally:
            capabilities._run = original

        self.assertNotIn("webp", found["still_outputs"])
        self.assertNotIn("webp", found["animation_outputs"])
        self.assertNotIn(".webp", found["explode_inputs"])
        self.assertIn("security policy", found["unavailable"]["still:webp"])
        # Everything else still works: one broken coder is not an outage.
        self.assertIn("gif", found["animation_outputs"])
        self.assertIn("png", found["still_outputs"])

    def test_discovery_never_raises_when_a_tool_is_absent(self):
        original = capabilities._run
        capabilities._run = lambda cmd, cwd: (_ for _ in ()).throw(FileNotFoundError("gone"))
        try:
            with self.assertRaises(FileNotFoundError):
                capabilities._run(["convert"], Path("/tmp"))
        finally:
            capabilities._run = original
        # _run itself swallows tool failures rather than propagating them.
        ok, reason = capabilities._run(["definitely-not-a-program"], Path("/tmp"))
        self.assertFalse(ok)
        self.assertTrue(reason)


class ValidationTest(unittest.TestCase):
    """The rules the server applies, and whether the browser applies the same."""

    def test_a_psd_is_accepted_with_the_mime_a_browser_sends(self):
        """The reproduced defect: .psd was allow-listed but every real PSD MIME
        was rejected."""
        for mime in PSD_MIME_TYPES:
            with self.subTest(mime=mime):
                accepted, reason = utils.validate_file("layers.psd", mime)
                self.assertTrue(accepted, reason)

    def test_raw_is_accepted_whatever_mime_the_browser_invents(self):
        for mime in RAW_MIME_TYPES:
            with self.subTest(mime=mime):
                accepted, reason = utils.validate_file("shot.cr2", mime)
                self.assertTrue(accepted, reason)

    def test_an_unknown_mime_is_still_refused(self):
        accepted, reason = utils.validate_file("a.png", "application/x-msdownload")
        self.assertFalse(accepted)
        self.assertIn("MIME", reason)

    def test_the_browser_applies_the_servers_raw_exemption(self):
        """The reproduced mismatch: the server skips the MIME check for RAW and
        the browser did not, so the browser refused files the server accepts."""
        page = CLIENT_PAGE.read_text()
        self.assertIn("RAW_EXTS", page, "the browser has no RAW MIME exemption")
        self.assertIn("function typeRejection(", page,
                      "the browser must apply one shared type rule")
        self.assertIn("CONFIG.raw_input_exts", page,
                      "the exemption must come from the server, not a second list")

    def test_the_browser_takes_the_mime_rule_from_the_server(self):
        """The same mismatch in the other direction: the browser kept its own
        MIME list, so the server accepted a PSD that the browser refused."""
        page = CLIENT_PAGE.read_text()
        self.assertIn("CONFIG.allowed_mime_types", page,
                      "the browser must adopt the server's MIME list")
        for mime in PSD_MIME_TYPES:
            with self.subTest(mime=mime):
                self.assertIn(mime, page, "the fallback list must also accept PSD")

    def test_the_browser_disables_what_the_server_withheld(self):
        page = CLIENT_PAGE.read_text()
        self.assertIn("function applyCapabilities(", page)
        self.assertIn("CONFIG.unavailable", page,
                      "the reason shown must be the server's")


class VideoQualityTest(unittest.TestCase):

    def test_quality_maps_onto_each_encoders_rate_factor(self):
        for fmt in ("webm", "mp4"):
            with self.subTest(fmt=fmt):
                values = [utils.video_crf_for_quality(fmt, q) for q in range(1, 101)]
                self.assertEqual(values, sorted(values, reverse=True),
                                 "a higher quality must never mean a worse CRF")

    def test_the_default_quality_reproduces_the_previous_encode(self):
        """The mapping is anchored so that only non-default values change
        anything: VP9 was pinned at CRF 32 and H.264 took libx264's own
        default of 23."""
        self.assertEqual(utils.video_crf_for_quality("webm", utils.DEFAULT_QUALITY), 32)
        self.assertEqual(utils.video_crf_for_quality("mp4", utils.DEFAULT_QUALITY), 23)

    def test_quality_is_clamped_to_each_encoders_range(self):
        for fmt in ("webm", "mp4"):
            with self.subTest(fmt=fmt):
                self.assertEqual(utils.video_crf_for_quality(fmt, 0),
                                 utils.video_crf_for_quality(fmt, 1))
                self.assertEqual(utils.video_crf_for_quality(fmt, 500),
                                 utils.video_crf_for_quality(fmt, 100))
                self.assertLessEqual(utils.video_crf_for_quality(fmt, 100),
                                     utils.VIDEO_CRF_BEST[fmt])
                self.assertGreaterEqual(utils.video_crf_for_quality(fmt, 1),
                                        utils.VIDEO_CRF_AT_DEFAULT[fmt])

    def test_the_encoder_command_carries_the_chosen_rate_factor(self):
        for fmt in ("webm", "mp4"):
            with self.subTest(fmt=fmt):
                cmd = utils.build_video_encode_command("f_%05d.png", f"out.{fmt}", 10,
                                                       fmt, quality=20)
                self.assertIn("-crf", cmd)
                self.assertEqual(cmd[cmd.index("-crf") + 1],
                                 str(utils.video_crf_for_quality(fmt, 20)))


class ServiceTest(unittest.TestCase):
    """What /config promises, and what the endpoints then accept."""

    @classmethod
    def setUpClass(cls):
        import uvicorn

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
                if get_json("/health"):
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

    def tearDown(self):
        for leftover in {f.name for f in main.OUTPUT_DIR.iterdir()
                         if f.is_file()} - self.before:
            (main.OUTPUT_DIR / leftover).unlink(missing_ok=True)
            (main.RECIPE_DIR / (leftover + ".json")).unlink(missing_ok=True)

    def test_config_reports_capabilities_and_the_reasons_for_the_rest(self):
        config = get_json("/config")
        self.assertIn("unavailable", config)
        self.assertIn("raw_input_exts", config)
        if not main.CAPABILITIES.get("raw_available", True):
            self.assertFalse(set(config["allowed_input_exts"]) & set(utils.RAW_EXTENSIONS),
                             "RAW must not be advertised without a decoder")
            self.assertIn("input:raw", config["unavailable"])

    def test_an_unavailable_output_is_refused_with_its_reason(self):
        """The reproduced defect: a format the policy denied was still offered,
        and the request died inside the coder."""
        original = dict(main.CAPABILITIES)
        main.CAPABILITIES["still_outputs"] = [f for f in original.get("still_outputs", [])
                                              if f != "webp"]
        main.CAPABILITIES.setdefault("unavailable", {})["still:webp"] = \
            "this build cannot write WEBP: denied by policy"
        try:
            self.assertNotIn("webp", get_json("/config")["still_output_formats"])
            status, payload = post("/convert-image",
                                   {"out_format": "webp", "keep_output": "false"},
                                   [("file", "a.png", tiny_png(), "image/png")])
        finally:
            main.CAPABILITIES.clear()
            main.CAPABILITIES.update(original)
        self.assertEqual(status, 400, payload)
        self.assertIn("not available in this build", payload["detail"])
        self.assertIn("denied by policy", payload["detail"])

    def test_the_still_formats_this_build_supports_all_still_work(self):
        """Nothing usable was lost to the new gate."""
        for fmt in get_json("/config")["still_output_formats"]:
            with self.subTest(fmt=fmt):
                status, payload = post("/convert-image",
                                       {"out_format": fmt, "keep_output": "false"},
                                       [("file", "a.png", tiny_png(), "image/png")])
                self.assertEqual(status, 200, payload)

    def test_a_psd_upload_reaches_the_converter(self):
        status, payload = post("/convert", {"format": "gif", "keep_output": "true"},
                               [("files", "layers.psd", tiny_png(),
                                 "image/vnd.adobe.photoshop")])
        # The bytes are a PNG, so the job itself may fail later; what matters is
        # that the type rule let it through instead of a 400.
        self.assertNotEqual(status, 400, payload)

    def test_video_quality_changes_the_encode(self):
        """The reproduced defect: the control was shown and recorded, and the
        two encodes came out byte for byte the same size."""
        frames = [("files", "a.png", noisy_png(seed=1), "image/png"),
                  ("files", "b.png", noisy_png(seed=2), "image/png")]
        sizes = {}
        for quality in (LOW_QUALITY, HIGH_QUALITY):
            status, payload = post("/convert",
                                   {"format": "webm", "keep_output": "true",
                                    "quality": str(quality), "delay": "100",
                                    "custom_name": f"quality_{quality}"}, frames)
            self.assertEqual(status, 200, payload)
            sizes[quality] = (main.OUTPUT_DIR / payload["output_filename"]).stat().st_size
        self.assertGreater(sizes[HIGH_QUALITY], sizes[LOW_QUALITY] * 2,
                           f"quality had little effect: {sizes}")

    def test_a_video_recipe_records_the_setting_the_encoder_was_given(self):
        status, payload = post("/convert",
                               {"format": "webm", "keep_output": "true",
                                "quality": "40", "delay": "100",
                                "custom_name": "recipe_crf"},
                               [("files", "a.png", tiny_png(32, 32), "image/png"),
                                ("files", "b.png", tiny_png(32, 32, (200, 10, 10)), "image/png")])
        self.assertEqual(status, 200, payload)
        recipe = json.loads((main.RECIPE_DIR /
                             (payload["output_filename"] + ".json")).read_text())
        self.assertEqual(recipe["video_crf"], utils.video_crf_for_quality("webm", 40))

    def test_a_still_recipe_does_not_claim_a_video_setting(self):
        status, payload = post("/convert", {"format": "gif", "keep_output": "true",
                                            "custom_name": "recipe_still"},
                               [("files", "a.png", tiny_png(32, 32), "image/png"),
                                ("files", "b.png", tiny_png(32, 32, (200, 10, 10)), "image/png")])
        self.assertEqual(status, 200, payload)
        recipe = json.loads((main.RECIPE_DIR /
                             (payload["output_filename"] + ".json")).read_text())
        self.assertNotIn("video_crf", recipe)


if __name__ == "__main__":
    unittest.main(verbosity=2)
