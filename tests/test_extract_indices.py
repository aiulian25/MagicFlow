"""Regression coverage for FIX-010: a frame index means the same thing at every
step — decode, select, archive, and re-import.

Needs the app's runtime and the media tools, so it runs inside the container
with the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_extract_indices.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import io
import json
import os
import shutil
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
PORT = int(os.environ.get("MAGICFLOW_INDEX_TEST_PORT", "8929"))
BASE = f"http://{HOST}:{PORT}"

FIXTURES = Path("/data/tmp/fix010-fixtures")
# Enough pages to cross the 999/1000 boundary, where three-digit names stop
# sorting into numeric order.
PAGE_COUNT = 1002
LAST_INDEX = PAGE_COUNT - 1
APNG_FRAMES = 5


def indexed_png(index, width=4, height=4):
    """A 4x4 image whose colour encodes its own index, so an extracted frame can
    be asked which source position it came from."""
    rgb = (index // 256, index % 256, 42)
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


def source_index_of(png_path):
    """Read an extracted frame's colour back into the index it encodes."""
    probe = run(["convert", str(png_path), "-format", "%[pixel:p{0,0}]", "info:"])
    text = probe.stdout.strip()            # srgb(0,101,42)
    try:
        numbers = [int(n) for n in text[text.index("(") + 1:text.index(")")].split(",")]
    except (ValueError, IndexError):
        return None
    return numbers[0] * 256 + numbers[1] if len(numbers) >= 2 else None


def build_fixtures():
    if FIXTURES.exists():
        return
    FIXTURES.mkdir(parents=True)
    pages = []
    for index in range(PAGE_COUNT):
        page = FIXTURES / f"page_{index:05d}.png"
        page.write_bytes(indexed_png(index))
        pages.append(str(page))
    # Multi-page TIFF: lossless, so each page's colour — and therefore its
    # index — survives the round trip exactly. Read by ImageMagick.
    run(["convert", *pages, str(FIXTURES / "pages.tiff")])
    # An APNG for the other decoder, so both backends can be compared.
    run(["ffmpeg", "-y", "-v", "error", "-framerate", "10",
         "-i", str(FIXTURES / "page_%05d.png"), "-frames:v", str(APNG_FRAMES),
         "-plays", "0", str(FIXTURES / "anim.apng")])


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


class FrameOrderTest(unittest.TestCase):
    """The ordering rule itself, before any of it reaches a request."""

    def test_numbers_order_numerically_not_as_text(self):
        """The reproduced defect in miniature: as text, frame_1000 sorts between
        frame_100 and frame_101."""
        names = ["frame_1000.png", "frame_100.png", "frame_101.png", "frame_99.png"]
        self.assertEqual(
            [p.name for p in sorted((Path(n) for n in names), key=main._frame_order_key)],
            ["frame_99.png", "frame_100.png", "frame_101.png", "frame_1000.png"])

    def test_two_field_scratch_names_order_by_input_then_frame(self):
        names = ["tmp00001_00000.png", "tmp00000_00012.png", "tmp00000_00002.png"]
        self.assertEqual(
            [p.name for p in sorted((Path(n) for n in names), key=main._frame_order_key)],
            ["tmp00000_00002.png", "tmp00000_00012.png", "tmp00001_00000.png"])

    def test_a_name_without_numbers_does_not_break_the_order(self):
        names = ["frame_2.png", "cover.png", "frame_10.png"]
        ordered = [p.name for p in sorted((Path(n) for n in names),
                                          key=main._frame_order_key)]
        self.assertEqual(ordered, ["cover.png", "frame_2.png", "frame_10.png"])

    def test_every_decoder_writes_the_same_zero_based_width(self):
        """One sequence for every backend: ffmpeg numbered from 1 while the
        API's selection was zero-based."""
        self.assertEqual(main.FRAME_NUMBER_PATTERN, "%05d")
        import utils
        command = utils.build_apng_decode_command("in.apng", "out_%05d.png")
        self.assertIn("-start_number", command)
        self.assertEqual(command[command.index("-start_number") + 1], "0")


class ExtractIndexTest(unittest.TestCase):
    """What a requested index actually returns, end to end."""

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

    def setUp(self):
        self.work = Path("/data/tmp") / f"indices-{uuid.uuid4().hex[:8]}"
        self.work.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def extract(self, source="pages.tiff", mime="image/tiff", **fields):
        form = {"out_format": "png"}
        form.update({k: str(v) for k, v in fields.items()})
        status, raw, headers = post(
            "/explode", form,
            [("file", source, (FIXTURES / source).read_bytes(), mime)])
        self.assertEqual(status, 200, raw[:200])
        extracted = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for name in archive.namelist():
                target = self.work / name
                target.write_bytes(archive.read(name))
                extracted[name] = target
        return extracted, headers

    def source_indices(self, extracted):
        """The source positions the archive actually delivered, in the order a
        consumer sorting by frame number would see them."""
        ordered = sorted(extracted, key=main._frame_order_key)
        return [source_index_of(extracted[name]) for name in ordered]

    # ── A requested index returns that source position ───────────────────────

    def test_selecting_one_page_past_the_boundary_returns_that_page(self):
        """The reproduced defect: asking for index 101 of a 1002-page document
        returned source index 1000, because frame_1000 sorted between
        frame_100 and frame_101."""
        extracted, _ = self.extract(start=101, end=101)
        self.assertEqual(len(extracted), 1, sorted(extracted))
        self.assertEqual(self.source_indices(extracted), [101])

    def test_a_range_across_the_boundary_stays_in_source_order(self):
        extracted, _ = self.extract(start=995, end=1001)
        self.assertEqual(self.source_indices(extracted), list(range(995, 1002)))

    def test_an_inclusive_end_past_the_document_clamps_to_what_exists(self):
        extracted, _ = self.extract(start=1000, end=5000)
        self.assertEqual(self.source_indices(extracted), [1000, 1001])

    def test_a_step_keeps_the_positions_it_names(self):
        extracted, _ = self.extract(start=900, end=1001, step=100)
        self.assertEqual(self.source_indices(extracted), [900, 1000])

    def test_index_zero_is_the_first_page(self):
        extracted, _ = self.extract(start=0, end=0)
        self.assertEqual(self.source_indices(extracted), [0])

    def test_the_last_index_is_reachable(self):
        extracted, _ = self.extract(start=LAST_INDEX, end=LAST_INDEX)
        self.assertEqual(self.source_indices(extracted), [LAST_INDEX])

    def test_a_start_beyond_the_document_explains_itself(self):
        status, raw, _ = post(
            "/explode", {"out_format": "png", "start": str(PAGE_COUNT + 10)},
            [("file", "pages.tiff", (FIXTURES / "pages.tiff").read_bytes(),
              "image/tiff")])
        self.assertEqual(status, 400)
        detail = json.loads(raw)["detail"]
        self.assertIn("beyond the last frame", detail)
        self.assertIn(str(LAST_INDEX), detail)

    # ── The names in the archive mean the same for every backend ─────────────

    def test_archive_names_are_zero_based_and_uniform_width(self):
        extracted, _ = self.extract(start=0, end=2)
        self.assertEqual(sorted(extracted),
                         ["frame_00000.png", "frame_00001.png", "frame_00002.png"])

    def test_an_ffmpeg_decoded_source_starts_at_the_same_index(self):
        """The reproduced mismatch: ImageMagick numbered from 0 and ffmpeg
        from 1, so 'frame 0' meant two different pictures."""
        extracted, headers = self.extract(source="anim.apng", mime="image/apng")
        self.assertEqual(int(headers["x-frame-count"]), APNG_FRAMES)
        self.assertEqual(sorted(extracted)[0], "frame_00000.png")
        self.assertEqual(self.source_indices(extracted)[0], 0)

    def test_both_backends_agree_on_what_index_one_means(self):
        from_imagemagick, _ = self.extract(start=1, end=1)
        from_ffmpeg, _ = self.extract(source="anim.apng", mime="image/apng",
                                      start=1, end=1)
        self.assertEqual(sorted(from_imagemagick), sorted(from_ffmpeg))
        self.assertEqual(self.source_indices(from_imagemagick), [1])
        self.assertEqual(self.source_indices(from_ffmpeg), [1])

    # ── Neighbouring behaviour ───────────────────────────────────────────────

    def test_a_whole_small_extract_is_unchanged_in_content_and_order(self):
        extracted, headers = self.extract(source="anim.apng", mime="image/apng")
        self.assertEqual(len(extracted), APNG_FRAMES)
        self.assertEqual(self.source_indices(extracted), list(range(APNG_FRAMES)))

    def test_the_browser_orders_archive_entries_the_same_way(self):
        """The page re-imports a ZIP for Edit in Combine; a text sort there
        would undo the ordering the server just got right."""
        page = Path("/app/static/index.html").read_text()
        self.assertIn("function compareFrameNames(", page)
        self.assertIn("compareFrameNames(a.name, b.name)", page)
        self.assertNotIn("a.name < b.name ? -1", page,
                         "the lexical comparator must be gone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
