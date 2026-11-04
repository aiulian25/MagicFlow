"""Regression coverage for FIX-013: Edit in Combine carries the source's own
frames, timing and loop, and never truncates work without asking.

The bridge is browser code, so this file covers the two halves a test can hold
onto: what the server gives the bridge to work with, and the contract the page
is written against. The executable parts of the bridge are in
tests/edit-roundtrip.test.mjs; the full click-through is recorded in the audit
notes.

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_edit_roundtrip.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import io
import json
import os
import struct
import subprocess
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

import main  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_EDIT_TEST_PORT", "8939"))
BASE = f"http://{HOST}:{PORT}"

FIXTURES = Path("/data/tmp/fix013-fixtures")
PAGE = Path("/app/static/index.html")

DELAYS_CS = [10, 30, 20]      # an uneven rhythm the round trip must keep
SOURCE_FRAMES = len(DELAYS_CS)
LOOP_COUNT = 3


def coloured_png(index, width=16, height=16):
    rgb = (index % 256, (index * 7) % 256, 200)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def build_fixtures():
    if FIXTURES.exists():
        return
    FIXTURES.mkdir(parents=True)
    for index in range(SOURCE_FRAMES):
        (FIXTURES / f"f{index}.png").write_bytes(coloured_png(index))
    timed = []
    for index, delay in enumerate(DELAYS_CS):
        timed += ["-delay", str(delay), str(FIXTURES / f"f{index}.png")]
    run(["convert", *timed, "-loop", str(LOOP_COUNT), str(FIXTURES / "uneven.gif")])
    run(["convert", "-delay", "10",
         *[str(FIXTURES / f"f{i}.png") for i in range(SOURCE_FRAMES)],
         "-loop", "0", str(FIXTURES / "forever.gif")])
    # A still, which has neither timing nor a loop to report.
    (FIXTURES / "still.png").write_bytes(coloured_png(5))


def post(path, fields, files=(), timeout=600):
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
            return (response.status, response.read(),
                    {k.lower(): v for k, v in response.headers.items()})
    except urllib.error.HTTPError as error:
        return (error.code, error.read(),
                {k.lower(): v for k, v in error.headers.items()})


def inspect(name, mime="image/gif"):
    status, raw, _ = post("/inspect", {},
                          [("file", name, (FIXTURES / name).read_bytes(), mime)])
    return status, json.loads(raw or b"{}")


class SourceFactsTest(unittest.TestCase):
    """What the bridge can learn before deciding anything."""

    @classmethod
    def setUpClass(cls):
        import uvicorn

        for directory in (main.INPUT_DIR, main.OUTPUT_DIR, main.TMP_DIR,
                          main.CONFIG_DIR, main.THUMB_DIR, main.RECIPE_DIR,
                          main.STAGING_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        build_fixtures()

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

    def test_the_frame_count_is_known_before_anything_is_decoded(self):
        """The size question has to be answerable in advance; it used to be
        discovered after the whole archive had been built and downloaded."""
        status, payload = inspect("uneven.gif")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["frames"], SOURCE_FRAMES)

    def test_the_sources_own_timing_is_reported(self):
        """A ZIP of stills carries no timing, so this is the only place an
        edited animation can get its rhythm back."""
        status, payload = inspect("uneven.gif")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["delays_cs"], DELAYS_CS)

    def test_a_finite_loop_count_is_reported(self):
        """The reproduced gap: /inspect offered no loop at all, so an edited
        animation could not keep one."""
        status, payload = inspect("uneven.gif")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["loop"], LOOP_COUNT)

    def test_an_endless_loop_is_reported_as_zero_not_as_unknown(self):
        status, payload = inspect("forever.gif")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["loop"], 0)

    def test_an_unreadable_loop_is_null_rather_than_guessed(self):
        """None means "this build could not read it", which a caller must not
        confuse with "loops forever"."""
        status, payload = inspect("still.png", "image/png")
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["loop"])

    def test_the_loop_reader_never_raises_on_a_bad_source(self):
        missing = Path("/data/tmp/does-not-exist.gif")
        self.assertIsNone(main._read_loop_count(missing, dict(os.environ),
                                                Path("/data/tmp")))

    # ── The subset the bridge asks for is the subset it gets ────────────────

    def test_a_range_returns_exactly_those_frames(self):
        status, raw, headers = post(
            "/explode", {"out_format": "png", "start": "1", "end": "2", "step": "1"},
            [("file", "uneven.gif", (FIXTURES / "uneven.gif").read_bytes(), "image/gif")])
        self.assertEqual(status, 200, raw[:200])
        self.assertEqual(int(headers["x-frame-count"]), 2)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(sorted(archive.namelist()),
                             ["frame_00001.png", "frame_00002.png"])

    def test_a_bounded_subset_of_a_long_source_decodes_only_that_subset(self):
        """What the capacity question leads to: the first N frames, not the
        whole animation followed by a silent truncation."""
        long_source = FIXTURES / "long.gif"
        if not long_source.exists():
            frames = []
            for index in range(20):
                frame = FIXTURES / f"L{index:03d}.png"
                frame.write_bytes(coloured_png(index + 40))
                frames.append(str(frame))
            run(["convert", "-delay", "5", *frames, str(long_source)])
        status, raw, headers = post(
            "/explode", {"out_format": "png", "start": "0", "end": "4", "step": "1"},
            [("file", "long.gif", long_source.read_bytes(), "image/gif")])
        self.assertEqual(status, 200, raw[:200])
        self.assertEqual(int(headers["x-frame-count"]), 5)


class BridgeContractTest(unittest.TestCase):
    """The page is written against the facts above; these are the promises it
    has to keep for the round trip to be honest."""

    @classmethod
    def setUpClass(cls):
        cls.page = PAGE.read_text()
        start = cls.page.index("async function editInCombine(")
        cls.bridge = cls.page[start:cls.page.index("\n}", start)]

    def test_the_bridge_asks_what_the_source_is_before_decoding_it(self):
        self.assertIn("inspectFile(file)", self.bridge)
        self.assertLess(self.bridge.index("inspectFile(file)"),
                        self.bridge.index("/explode"),
                        "the probe must come before the expensive work")

    def test_the_bridge_decodes_only_the_selected_range(self):
        """The reproduced defect: it re-extracted the whole source however
        narrow the user's selection was."""
        for field in ("'start'", "'step'", "'end'"):
            with self.subTest(field=field):
                self.assertIn(field, self.bridge)

    def test_the_extract_panels_edit_button_passes_its_range(self):
        button = self.page[self.page.index("extract-edit-btn').addEventListener"):][:600]
        for control in ("extract-start", "extract-end", "extract-step"):
            with self.subTest(control=control):
                self.assertIn(control, button)

    def test_an_oversized_source_is_questioned_before_the_work(self):
        self.assertIn("confirm(", self.bridge)
        self.assertLess(self.bridge.index("confirm("), self.bridge.index("/explode"),
                        "the question must come before the extraction")

    def test_declining_leaves_the_session_alone(self):
        """setMode clears every mode's work, so it must not run until the load
        has already succeeded."""
        decline = self.bridge.index("if (!proceed)")
        self.assertLess(decline, self.bridge.index("setMode('combine')"))
        self.assertIn("your current work is untouched", self.bridge)

    def test_the_session_is_replaced_only_after_the_frames_are_in_hand(self):
        self.assertLess(self.bridge.index("parseZip"),
                        self.bridge.index("setMode('combine')"),
                        "a failed load must not have cleared the session")

    def test_the_imported_frames_start_from_identity_transforms(self):
        """The frames are already rendered; a leftover rotation or colour shift
        would be applied to them a second time."""
        self.assertIn("resetTransformsToIdentity()", self.bridge)
        self.assertLess(self.bridge.index("resetTransformsToIdentity()"),
                        self.bridge.index("handleCombineFiles(frames)"))

    def test_the_sources_timing_and_loop_are_applied(self):
        self.assertIn("applySourceTiming(info", self.bridge)
        timing = self.page[self.page.index("function applySourceTiming("):]
        timing = timing[:timing.index("\n}") + 2]
        self.assertIn("delays_cs", timing)
        self.assertIn("setting-loop", timing)
        self.assertIn("Number.isFinite(info.loop)", timing,
                      "an unreadable loop must not overwrite the user's setting")

    def test_the_timing_is_mapped_through_the_selected_range(self):
        """Frame i of a stepped selection is not frame i of the source."""
        timing = self.page[self.page.index("function applySourceTiming("):]
        timing = timing[:timing.index("\n}") + 2]
        self.assertIn("selection.start + i * selection.step", timing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
