"""Regression coverage for FIX-011: the timeline the user asked for reaches the
file, and boomerang mirrors the frames that actually exist.

Needs the app's runtime and the media tools, so it runs inside the container
with the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_timing.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
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
import zlib
from pathlib import Path

sys.path.insert(0, "/app")

import main  # noqa: E402
import utils  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_TIMING_TEST_PORT", "8933"))
BASE = f"http://{HOST}:{PORT}"

FIXTURES = Path("/data/tmp/fix011-fixtures")
SOURCE_FRAMES = 3
# An obviously uneven rhythm: none of these differences is a rounding artefact.
DELAYS_MS = [100, 300, 200]
# Containers quantize to their own timebase; this slack is far below the
# 100/200/300 ms differences under test.
TIMEBASE_SLACK_MS = 20


def coloured_png(index, width=32, height=24):
    rgb = (20 + index * 60, 200 - index * 50, 90)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def build_fixtures():
    if FIXTURES.exists():
        return
    FIXTURES.mkdir(parents=True)
    for index in range(SOURCE_FRAMES):
        (FIXTURES / f"f{index}.png").write_bytes(coloured_png(index))
    # One animated source holding three frames: a single input FILE, which is
    # what made the old boomerang a no-op.
    run(["convert", "-delay", "10",
         *[str(FIXTURES / f"f{i}.png") for i in range(SOURCE_FRAMES)],
         str(FIXTURES / "three.gif")])
    # The same three frames with an uneven rhythm, for the re-convert side.
    run(["convert", "-delay", "10", str(FIXTURES / "f0.png"),
         "-delay", "30", str(FIXTURES / "f1.png"),
         "-delay", "20", str(FIXTURES / "f2.png"),
         str(FIXTURES / "uneven.gif")])


def frame_durations_ms(path):
    """Per-frame durations of a produced animation, in milliseconds.

    GIF and WebP carry centisecond delays that `identify` reports. APNG carries
    packet durations. VP9 packets carry no duration at all, so video timing is
    read from the presentation timestamps: a frame lasts until the next starts."""
    suffix = Path(path).suffix.lower()
    if suffix in (".gif", ".webp"):
        probe = run(["identify", "-format", "%T\n", str(path)])
        return [int(v) * 10 for v in probe.stdout.split() if v.lstrip("-").isdigit()]
    probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "packet=duration_time", "-of", "csv=p=0", str(path)])
    durations = [round(float(v.strip().rstrip(",")) * 1000)
                 for v in probe.stdout.strip().splitlines()
                 if v.strip().rstrip(",").replace(".", "", 1).isdigit()]
    if durations:
        return durations
    probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)])
    stamps = [float(v.strip().rstrip(",")) for v in probe.stdout.strip().splitlines()
              if v.strip().rstrip(",").replace(".", "", 1).isdigit()]
    return [round((stamps[i + 1] - stamps[i]) * 1000) for i in range(len(stamps) - 1)]


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


class TimelineBuilderTest(unittest.TestCase):
    """The list ffmpeg reads, before any encoding happens."""

    def test_each_frame_states_its_own_duration(self):
        body = utils.build_concat_list(["/t/a.png", "/t/b.png"], [10, 30])
        self.assertIn("duration 0.1000", body)
        self.assertIn("duration 0.3000", body)

    def test_the_last_frame_is_not_written_twice(self):
        """Repeating the final entry is the usual way to give it a duration,
        but ffmpeg writes the repeat as a real frame — four frames for three
        pictures. The muxer sets the last duration instead."""
        body = utils.build_concat_list(["/t/a.png", "/t/b.png"], [10, 30])
        self.assertEqual(body.count("file '/t/b.png'"), 1)

    def test_the_apng_muxer_is_told_the_final_frames_duration(self):
        """Without it the last frame inherits the previous frame's delay: a
        100/300/200 timeline came out 100/300/300."""
        cmd = utils.build_apng_encode_command("p_%05d.png", "o.apng", 10, 0,
                                              concat_file="/t/list.txt",
                                              final_delay_cs=20)
        self.assertIn("-final_delay", cmd)
        self.assertEqual(cmd[cmd.index("-final_delay") + 1], "0.2000")

    def test_uniform_timing_needs_no_final_delay(self):
        cmd = utils.build_apng_encode_command("p_%05d.png", "o.apng", 10, 0)
        self.assertNotIn("-final_delay", cmd)
        self.assertNotIn("concat", cmd)

    def test_the_timebase_hint_is_fine_enough_for_the_durations(self):
        """Without it the nested image demuxer runs at 25fps and every duration
        snaps to a 40 ms grid: 100/300/200 came out as 120/280/200."""
        body = utils.build_concat_list(["/t/a.png"], [10])
        self.assertIn(f"option framerate {utils.CONCAT_FRAMERATE_HINT}", body)
        self.assertGreaterEqual(utils.CONCAT_FRAMERATE_HINT, 100)

    def test_a_timeline_is_used_only_when_it_fits_the_frames(self):
        """Delays that cannot address the prepared frames are dropped rather
        than applied to the wrong ones."""
        work = Path("/data/tmp") / f"timeline-{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True)
        try:
            for index in range(2):
                (work / f"prep_{index:05d}.png").write_bytes(coloured_png(index))
            self.assertIsNone(main._write_timeline(work, [10, 20, 30]))
            self.assertIsNone(main._write_timeline(work, None))
            self.assertIsNotNone(main._write_timeline(work, [10, 20]))
        finally:
            shutil.rmtree(work, ignore_errors=True)


class BoomerangTest(unittest.TestCase):

    def test_a_sequence_plays_forward_then_back_without_repeating_the_ends(self):
        self.assertEqual(main._boomerang_indices(3), [0, 1, 2, 1])
        self.assertEqual(main._boomerang_indices(4), [0, 1, 2, 3, 2, 1])

    def test_too_short_to_mirror_is_left_alone(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                self.assertEqual(main._boomerang_indices(count), list(range(count)))


class ConvertTimingTest(unittest.TestCase):
    """What the produced file actually plays."""

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
        self.before = {f.name for f in main.OUTPUT_DIR.iterdir() if f.is_file()}

    def tearDown(self):
        for leftover in {f.name for f in main.OUTPUT_DIR.iterdir()
                         if f.is_file()} - self.before:
            (main.OUTPUT_DIR / leftover).unlink(missing_ok=True)
            (main.RECIPE_DIR / (leftover + ".json")).unlink(missing_ok=True)

    def stills(self):
        return [("files", f"f{i}.png", (FIXTURES / f"f{i}.png").read_bytes(), "image/png")
                for i in range(SOURCE_FRAMES)]

    def combine(self, files, fmt, **fields):
        form = {"format": fmt, "keep_output": "true",
                "custom_name": f"t_{uuid.uuid4().hex[:8]}"}
        form.update({k: str(v) for k, v in fields.items()})
        status, payload = post("/convert", form, files)
        self.assertEqual(status, 200, payload)
        return main.OUTPUT_DIR / payload["output_filename"], payload

    def recipe_for(self, produced):
        return json.loads((main.RECIPE_DIR / (produced.name + ".json")).read_text())

    def assertTimeline(self, produced, expected_ms):
        """Compare the durations the file carries with the ones asked for.

        MP4 and WebM encode no duration for the final frame — there is no
        following timestamp to measure it against — so for those the comparison
        covers every gap the container does express. The frame count is checked
        separately, so a dropped frame cannot hide here."""
        measured = frame_durations_ms(produced)
        comparable = len(expected_ms)
        if produced.suffix.lower() in (".mp4", ".webm"):
            comparable = len(expected_ms) - 1
        self.assertEqual(len(measured), comparable,
                         f"{produced.suffix}: expected {expected_ms}, read {measured}")
        for asked, got in zip(expected_ms[:comparable], measured):
            self.assertLessEqual(abs(asked - got), TIMEBASE_SLACK_MS,
                                 f"{produced.suffix}: expected {expected_ms}, read {measured}")

    # ── Uneven timing reaches every format ───────────────────────────────────

    def test_per_frame_delays_survive_in_every_animated_format(self):
        """The reproduced defect: GIF kept 100/300/200 while APNG and video
        were flattened to the uniform delay, silently."""
        for fmt in ("gif", "apng", "webm", "mp4"):
            with self.subTest(fmt=fmt):
                produced, _ = self.combine(
                    self.stills(), fmt,
                    delays=",".join(str(d) for d in DELAYS_MS), delay=100)
                self.assertTimeline(produced, DELAYS_MS)

    def test_a_uniform_delay_is_still_exactly_uniform(self):
        for fmt in ("gif", "apng"):
            with self.subTest(fmt=fmt):
                produced, _ = self.combine(self.stills(), fmt, delay=250)
                self.assertTimeline(produced, [250] * SOURCE_FRAMES)

    def test_an_explicit_uniform_override_beats_the_sources_rhythm(self):
        """keep_timing off on Re-convert means the user wants one rate."""
        source, _ = self.combine(self.stills(), "gif",
                                 delays=",".join(str(d) for d in DELAYS_MS), delay=100)
        status, payload = post("/reconvert/" + source.name,
                               {"format": "apng", "keep_output": "true",
                                "keep_timing": "false", "delay": "50"})
        self.assertEqual(status, 200, payload)
        produced = main.OUTPUT_DIR / payload["output_filename"]
        self.assertTimeline(produced, [50] * SOURCE_FRAMES)

    def test_the_response_states_the_timeline_the_file_carries(self):
        produced, payload = self.combine(
            self.stills(), "apng",
            delays=",".join(str(d) for d in DELAYS_MS), delay=100)
        self.assertEqual(payload["effective_delays_ms"], DELAYS_MS)
        self.assertEqual(payload["frame_count"], SOURCE_FRAMES)
        self.assertEqual(self.recipe_for(produced)["effective_delays_ms"], DELAYS_MS)

    # ── Boomerang mirrors the frames that exist ──────────────────────────────

    def test_boomerang_mirrors_an_animated_source(self):
        """The reproduced defect: the mirror was applied to input FILES, so a
        single three-frame animation came out unmirrored — while its recipe
        claimed the four frames a boomerang would have produced."""
        produced, payload = self.combine(
            [("files", "three.gif", (FIXTURES / "three.gif").read_bytes(), "image/gif")],
            "gif", boomerang="true", delay=100)
        expected = len(main._boomerang_indices(SOURCE_FRAMES))
        self.assertEqual(main._probe_frames_and_size(produced)[0], expected)
        self.assertEqual(payload["frame_count"], expected)

    def test_boomerang_on_separate_stills_still_works(self):
        produced, payload = self.combine(self.stills(), "gif", boomerang="true",
                                         delay=100)
        expected = len(main._boomerang_indices(SOURCE_FRAMES))
        self.assertEqual(main._probe_frames_and_size(produced)[0], expected)

    def test_boomerang_mirrors_the_timing_with_the_frames(self):
        produced, _ = self.combine(
            self.stills(), "gif", boomerang="true",
            delays=",".join(str(d) for d in DELAYS_MS), delay=100)
        mirrored = [DELAYS_MS[i] for i in main._boomerang_indices(SOURCE_FRAMES)]
        self.assertTimeline(produced, mirrored)

    def test_boomerang_reaches_the_ffmpeg_formats_too(self):
        produced, payload = self.combine(
            [("files", "three.gif", (FIXTURES / "three.gif").read_bytes(), "image/gif")],
            "apng", boomerang="true", delay=100)
        self.assertEqual(payload["frame_count"],
                         len(main._boomerang_indices(SOURCE_FRAMES)))

    # ── The recipe describes the file, not the estimate ──────────────────────

    def test_the_recipe_frame_count_matches_the_file(self):
        produced, _ = self.combine(
            [("files", "three.gif", (FIXTURES / "three.gif").read_bytes(), "image/gif")],
            "gif", boomerang="true", delay=100)
        self.assertEqual(self.recipe_for(produced)["frames"],
                         main._probe_frames_and_size(produced)[0])

    def test_the_recipe_records_quantized_timing_not_the_request(self):
        """GIF counts in centiseconds, so a millisecond request is rounded to
        the format's grid — and the report must be what the file plays, not
        what was asked for."""
        requested_ms = 105
        produced, payload = self.combine(self.stills(), "gif", delay=requested_ms)
        quantized = max(1, round(requested_ms / 10)) * 10
        self.assertEqual(set(payload["effective_delays_ms"]), {quantized})
        self.assertNotEqual(quantized, requested_ms, "the fixture must be off-grid")
        self.assertEqual(self.recipe_for(produced)["effective_delays_ms"],
                         payload["effective_delays_ms"])

    # ── Re-convert no longer averages ────────────────────────────────────────

    def test_reconvert_preserves_uneven_timing(self):
        """The reproduced behaviour: mixed delays were collapsed to their mean
        for APNG and video, and the user was told only after the file existed."""
        status, made = post("/convert",
                            {"format": "gif", "keep_output": "true",
                             "custom_name": f"src_{uuid.uuid4().hex[:6]}",
                             "delays": ",".join(str(d) for d in DELAYS_MS),
                             "delay": "100"}, self.stills())
        self.assertEqual(status, 200, made)
        for fmt in ("apng", "webm"):
            with self.subTest(fmt=fmt):
                status, payload = post("/reconvert/" + made["output_filename"],
                                       {"format": fmt, "keep_output": "true",
                                        "keep_timing": "true"})
                self.assertEqual(status, 200, payload)
                produced = main.OUTPUT_DIR / payload["output_filename"]
                self.assertTimeline(produced, DELAYS_MS)
                self.assertEqual(payload["timing"], "preserved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
