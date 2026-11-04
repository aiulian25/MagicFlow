"""Regression coverage for FIX-007: resource budgets are enforced before the
work is paid for, not discovered afterwards.

Needs the app's runtime and ImageMagick, so it runs inside the container with
the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_resource_limits.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import json
import os
import resource
import shutil
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, "/app")

import main  # noqa: E402  (after sys.path setup)

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_LIMITS_TEST_PORT", "8909"))
BASE = f"http://{HOST}:{PORT}"
MIB = 1024 * 1024

# The server runs in this process, so a memory measurement only means something
# when the test's own buffers are built and consumed outside the sampled window.
# This is the headroom allowed for the server's own bookkeeping during a send.
ALLOWED_RSS_GROWTH_MIB = 40

# Dimensions ImageMagick's policy itself refuses, used to prove the app answers
# before the coder does.
OVERSIZE_SIDE = 18000


def tiny_png(width=16, height=16, rgb=(10, 200, 10)):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def current_rss_mib():
    """Resident set right now. ru_maxrss is a high-water mark that never falls,
    so it cannot measure one request after an earlier spike."""
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


class RssSampler:
    """Highest resident set observed inside the `with` block."""

    def __enter__(self):
        self.baseline = current_rss_mib()
        self.peak = self.baseline
        self._stop = threading.Event()

        def sample():
            while not self._stop.wait(0.02):
                self.peak = max(self.peak, current_rss_mib())

        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    @property
    def growth(self):
        return self.peak - self.baseline


def build_body(fields, files=()):
    boundary = "mfbound" + uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{key}"\r\n\r\n{value}\r\n').encode()
    for key, filename, data in files:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{key}"; filename="{filename}"\r\n'
                 f"Content-Type: image/png\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def send(path, body, content_type, timeout=300):
    request = urllib.request.Request(BASE + path, method="POST", data=body)
    request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except ValueError:
            return error.code, {"detail": raw[:200].decode("utf-8", "replace")}


def post(path, fields, files=(), timeout=300):
    body, content_type = build_body(fields, files)
    return send(path, body, content_type, timeout)


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read())


class ResourceLimitTest(unittest.TestCase):

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
        deadline = time.monotonic() + 40
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

    # ── Upload bytes: refused before they are allocated ─────────────────────

    def test_an_oversize_file_is_refused_without_being_allocated(self):
        """The reproduced defect: the body was received, spooled and read into a
        Python bytes object, and only then measured."""
        body, content_type = build_body(
            {"format": "gif", "keep_output": "true"},
            [("files", "huge.png", b"x" * (120 * MIB))])
        with RssSampler() as rss:
            status, payload = send("/convert", body, content_type)
        self.assertEqual(status, 413, payload)
        self.assertLess(rss.growth, ALLOWED_RSS_GROWTH_MIB,
                        f"server RSS grew {rss.growth:.0f} MiB answering a refusal")

    def test_files_within_the_per_file_limit_still_have_a_job_quota(self):
        """Sixty admissible files are still three gigabytes of upload."""
        original = main.MAX_TOTAL_UPLOAD_BYTES
        main.MAX_TOTAL_UPLOAD_BYTES = 4 * MIB
        try:
            status, payload = post(
                "/convert", {"format": "gif", "keep_output": "true"},
                [("files", f"f{i}.png", b"y" * (3 * MIB)) for i in range(2)])
        finally:
            main.MAX_TOTAL_UPLOAD_BYTES = original
        self.assertEqual(status, 413, payload)
        self.assertIn("one job may upload", payload["detail"])

    def test_a_body_far_past_the_quota_is_refused_before_it_is_parsed(self):
        """Content-Length is answered by middleware, so the multipart parser
        never receives a body that cannot possibly be within budget."""
        request = urllib.request.Request(
            BASE + "/convert", method="POST", data=b"",
            headers={"Content-Type": "multipart/form-data; boundary=x",
                     "Content-Length": str(main.MAX_REQUEST_BYTES + 1)})
        # urllib sends the declared length with an empty body; the middleware
        # must answer from the header alone, without waiting for bytes.
        try:
            urllib.request.urlopen(request, timeout=30)
            self.fail("an over-limit Content-Length must be refused")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 413)
            self.assertIn("per-job limit", json.loads(error.read())["detail"])

    def test_a_normal_upload_is_unaffected(self):
        status, payload = post("/convert",
                               {"format": "gif", "keep_output": "true",
                                "custom_name": "within_budget"},
                               [("files", "a.png", tiny_png()),
                                ("files", "b.png", tiny_png(16, 16, (200, 10, 10)))])
        self.assertEqual(status, 200, payload)
        self.assertTrue((main.OUTPUT_DIR / payload["output_filename"]).is_file())

    # ── Decoded size: measured from metadata, before decoding ───────────────

    def test_a_small_file_that_decodes_huge_is_refused_with_a_usable_message(self):
        """The reproduced defect: a 975 KiB file decoding to 324 MP produced
        `no images defined` from the coder, with no mention of any limit."""
        status, payload = post("/convert-image",
                               {"out_format": "png", "keep_output": "false"},
                               [("file", "bomb.png",
                                 tiny_png(OVERSIZE_SIDE, OVERSIZE_SIDE))])
        self.assertEqual(status, 400, payload)
        detail = payload["detail"]
        self.assertTrue("limit" in detail or "megapixel" in detail, detail)
        self.assertNotIn("no images defined", detail)

    def test_the_pixel_budget_counts_the_whole_job_not_one_frame(self):
        """Frames well inside every per-image limit still add up."""
        original = main.MAX_DECODED_PIXELS
        main.MAX_DECODED_PIXELS = 1000  # a few small frames already exceed this
        try:
            status, payload = post("/convert",
                                   {"format": "gif", "keep_output": "true"},
                                   [("files", f"f{i}.png", tiny_png(40, 40))
                                    for i in range(3)])
        finally:
            main.MAX_DECODED_PIXELS = original
        self.assertEqual(status, 400, payload)
        self.assertIn("megapixel", payload["detail"])

    def test_the_dimension_budget_reports_the_offending_size(self):
        original = main.MAX_IMAGE_DIMENSION
        main.MAX_IMAGE_DIMENSION = 32
        try:
            status, payload = post("/convert-image",
                                   {"out_format": "png", "keep_output": "false"},
                                   [("file", "wide.png", tiny_png(64, 16))])
        finally:
            main.MAX_IMAGE_DIMENSION = original
        self.assertEqual(status, 400, payload)
        self.assertIn("64×16", payload["detail"])

    def test_an_unmeasurable_source_is_refused_rather_than_handed_to_a_coder(self):
        """identify refuses an over-policy image silently — exit 1, no output —
        so treating 'unmeasurable' as 'unlimited' is how the budget was skipped."""
        self.assertEqual(main._probe_source("/data/does-not-exist.png"), (0, 0, 0))
        with self.assertRaises(main.HTTPException) as caught:
            main._require_measurable(0, 0, "This image")
        self.assertEqual(caught.exception.status_code, 400)

    def test_one_probe_answers_both_the_frame_cap_and_the_pixel_budget(self):
        """Measuring an input must not cost more than counting it did."""
        sample = main.TMP_DIR / f"probe-{uuid.uuid4().hex}.png"
        sample.write_bytes(tiny_png(70, 50))
        try:
            frames, width, height = main._probe_source(str(sample))
            self.assertEqual((frames, width, height), (1, 70, 50))
            self.assertEqual(main._count_frames(str(sample)), 1)
        finally:
            sample.unlink(missing_ok=True)

    # ── Disk: reported before the job starts ────────────────────────────────

    def test_a_nearly_full_volume_is_reported_before_work_starts(self):
        original = main.MIN_FREE_DISK_BYTES
        main.MIN_FREE_DISK_BYTES = shutil.disk_usage(main.TMP_DIR).free + (1 << 30)
        try:
            status, payload = post("/convert",
                                   {"format": "gif", "keep_output": "true"},
                                   [("files", "a.png", tiny_png())])
        finally:
            main.MIN_FREE_DISK_BYTES = original
        self.assertEqual(status, 507, payload)
        self.assertIn("free disk space", payload["detail"])
        self.assertIn("Delete some saved files", payload["detail"])

    def test_a_broken_free_space_probe_does_not_block_work(self):
        """A guard that cannot measure must not become an outage."""
        original = shutil.disk_usage
        shutil.disk_usage = lambda _: (_ for _ in ()).throw(OSError("no statvfs"))
        try:
            main._require_free_space()  # must not raise
        finally:
            shutil.disk_usage = original

    # ── Archives: bounded memory, cleaned up ────────────────────────────────

    def test_an_archive_does_not_scale_the_servers_memory(self):
        """The reproduced defect: BytesIO plus getvalue() cost about twice the
        archive's size in resident memory."""
        blob = os.urandom(12 * MIB)  # incompressible: the ZIP is the same size
        names = []
        for i in range(5):
            name = f"archive_src_{i}.gif"
            (main.OUTPUT_DIR / name).write_bytes(blob)
            names.append(name)
        del blob
        request = urllib.request.Request(
            BASE + "/files/archive", method="POST",
            data=json.dumps({"filenames": names}).encode())
        request.add_header("Content-Type", "application/json")
        received = 0
        with RssSampler() as rss:
            with urllib.request.urlopen(request, timeout=300) as response:
                while True:
                    chunk = response.read(MIB)
                    if not chunk:
                        break
                    received += len(chunk)
        self.assertGreater(received, 50 * MIB)
        self.assertLess(rss.growth, ALLOWED_RSS_GROWTH_MIB,
                        f"server RSS grew {rss.growth:.0f} MiB for a "
                        f"{received // MIB} MiB archive")

    def archive_scratch(self):
        """Scratch directories holding a built archive. Other job scratch may
        come and go in TMP_DIR while a suite runs; only these are this test's
        business."""
        return {entry.name for entry in main.TMP_DIR.iterdir()
                if entry.is_dir() and (entry / "archive.zip").exists()}

    def test_an_archive_is_removed_once_it_has_been_sent(self):
        name = "archive_cleanup.gif"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        before = self.archive_scratch()
        request = urllib.request.Request(
            BASE + "/files/archive", method="POST",
            data=json.dumps({"filenames": [name]}).encode())
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=60) as response:
            self.assertEqual(response.status, 200)
            response.read()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.archive_scratch() - before:
            time.sleep(0.1)
        self.assertEqual(self.archive_scratch() - before, set(),
                         "the archive's scratch directory outlived the response")

    def test_an_oversize_selection_is_refused_before_the_archive_is_built(self):
        name = "archive_big.gif"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        original = main.MAX_ARCHIVE_BYTES
        main.MAX_ARCHIVE_BYTES = 1
        try:
            request = urllib.request.Request(
                BASE + "/files/archive", method="POST",
                data=json.dumps({"filenames": [name]}).encode())
            request.add_header("Content-Type", "application/json")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=60)
            self.assertEqual(caught.exception.code, 413)
        finally:
            main.MAX_ARCHIVE_BYTES = original

    # ── Extract: no work before the range is known to be usable ─────────────

    def test_an_impossible_range_never_reaches_the_decoder(self):
        """The reproduced defect: every frame was decoded and re-encoded before
        the range was checked against them."""
        status, payload = post("/convert",
                               {"format": "gif", "keep_output": "true",
                                "custom_name": "range_src", "delay": "100"},
                               [("files", f"f{i}.png", tiny_png(40, 40))
                                for i in range(6)])
        self.assertEqual(status, 200, payload)
        gif = (main.OUTPUT_DIR / payload["output_filename"]).read_bytes()

        calls = []
        original = main.build_explode_command

        def counting(*args, **kwargs):
            calls.append(args[0])
            return original(*args, **kwargs)

        main.build_explode_command = counting
        try:
            status, payload = post("/explode",
                                   {"out_format": "jpg", "start": "500"},
                                   [("file", "src.gif", gif)])
        finally:
            main.build_explode_command = original
        self.assertEqual(status, 400, payload)
        self.assertEqual(calls, [], "the decoder ran for a range that selects nothing")
        self.assertIn("beyond the last frame", payload["detail"])

    def test_a_valid_range_still_returns_exactly_those_frames(self):
        status, payload = post("/convert",
                               {"format": "gif", "keep_output": "true",
                                "custom_name": "range_ok", "delay": "100"},
                               [("files", f"f{i}.png", tiny_png(40, 40))
                                for i in range(6)])
        self.assertEqual(status, 200, payload)
        gif = (main.OUTPUT_DIR / payload["output_filename"]).read_bytes()

        body, content_type = build_body({"out_format": "png", "start": "2", "end": "4"},
                                        [("file", "src.gif", gif)])
        request = urllib.request.Request(BASE + "/explode", method="POST", data=body)
        request.add_header("Content-Type", content_type)
        import io
        with urllib.request.urlopen(request, timeout=120) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Frame-Count"], "3")
            archive = response.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(len(names), 3, names)

    def test_extract_archives_are_stored_not_deflated(self):
        """Measured on real frames: DEFLATE returned 100.0% of the input size
        while costing roughly nineteen times the CPU."""
        self.assertEqual(main.ARCHIVE_COMPRESSION, zipfile.ZIP_STORED)

    # ── The limits are discoverable ─────────────────────────────────────────

    def test_config_reports_every_budget_the_client_is_held_to(self):
        config = get_json("/config")
        for key in ("max_file_size", "max_files", "max_explode_frames",
                    "max_total_upload_bytes", "max_image_dimension",
                    "max_decoded_pixels"):
            with self.subTest(key=key):
                self.assertIn(key, config)
                self.assertGreater(config[key], 0)

    def test_an_operator_override_is_read_but_cannot_remove_a_limit(self):
        os.environ["MAGICFLOW_TEST_BUDGET"] = "4242"
        try:
            self.assertEqual(main._env_int("MAGICFLOW_TEST_BUDGET", 7), 4242)
        finally:
            del os.environ["MAGICFLOW_TEST_BUDGET"]
        for bad in ("0", "-1", "not-a-number", ""):
            with self.subTest(value=bad):
                os.environ["MAGICFLOW_TEST_BUDGET"] = bad
                try:
                    self.assertEqual(main._env_int("MAGICFLOW_TEST_BUDGET", 7), 7)
                finally:
                    del os.environ["MAGICFLOW_TEST_BUDGET"]
        self.assertEqual(main._env_int("MAGICFLOW_ABSENT_BUDGET", 11), 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
