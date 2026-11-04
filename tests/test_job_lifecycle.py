"""Regression coverage for FIX-006: media work runs off the event loop, under a
bounded queue, with a job-wide budget and children the server can actually stop.

Needs the app's runtime, so it runs inside the container with the source
mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_job_lifecycle.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
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
import zlib
from pathlib import Path

sys.path.insert(0, "/app")

import main  # noqa: E402  (after sys.path setup)

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_JOB_TEST_PORT", "8907"))
BASE = f"http://{HOST}:{PORT}"

# A request answered on the event loop must not wait on encoding. The encode
# below lasts SLOW_ENCODE_S, so anything near that means the loop was blocked;
# the allowance is generous enough to survive a loaded CI box.
SLOW_ENCODE_S = 5
RESPONSIVE_S = 1.0

# A sentinel long enough that it can only end by being killed.
SENTINEL_SLEEP_S = 900

DATA_DIRS = ("INPUT_DIR", "OUTPUT_DIR", "TMP_DIR", "CONFIG_DIR", "THUMB_DIR",
             "RECIPE_DIR", "STAGING_DIR")


def ensure_data_dirs():
    for name in DATA_DIRS:
        getattr(main, name).mkdir(parents=True, exist_ok=True)


def tiny_png(rgb=(10, 200, 10), width=16, height=16):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def post(path, fields, files=()):
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
    request = urllib.request.Request(BASE + path, method="POST", data=body)
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except ValueError:
            return error.code, {"detail": raw[:120].decode("utf-8", "replace")}


def timed_get(path):
    """(seconds, body) — what a browser polling this endpoint would experience."""
    started = time.monotonic()
    with urllib.request.urlopen(BASE + path, timeout=120) as response:
        body = json.loads(response.read())
    return time.monotonic() - started, body


def slow_encoder(seconds):
    """An assembly command that takes its time and spawns a child of its own, so
    both responsiveness and descendant cleanup can be observed."""
    def build(frames, output, *args, **kwargs):
        return ["sh", "-c", f"sleep {seconds} & wait; : > '{output}'"]
    return build


def live_sentinel_pids():
    """Running `sleep SENTINEL_SLEEP_S` processes, read from /proc (the slim
    image has no ps)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"sleep" in argv and str(SENTINEL_SLEEP_S).encode() in argv:
            found.append(int(entry))
    return found


class JobRuntimeTest(unittest.TestCase):
    """Unit-level behaviour of the job machinery; no server needed."""

    @classmethod
    def setUpClass(cls):
        ensure_data_dirs()

    def tearDown(self):
        for pid in live_sentinel_pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    # ── Children the server can stop ─────────────────────────────────────────

    def test_a_timed_out_step_takes_its_descendants_with_it(self):
        """The reproduced defect: Python's timeout kills only the direct child,
        so a delegate (ghostscript, ffmpeg) kept running after the job gave up."""
        self.assertEqual(live_sentinel_pids(), [], "a stale sentinel is still alive")
        with self.assertRaises(subprocess.TimeoutExpired):
            main._run_subprocess(
                ["sh", "-c", f"sleep {SENTINEL_SLEEP_S} & wait"], timeout=1)
        time.sleep(0.5)
        self.assertEqual(live_sentinel_pids(), [],
                         "the child's own children must not survive the timeout")

    def test_shutdown_stops_a_running_child(self):
        """A SIGTERM must not leave encoders burning CPU on a dying container."""
        finished = threading.Event()

        def run():
            try:
                main._run_subprocess(
                    ["sh", "-c", f"sleep {SENTINEL_SLEEP_S} & wait"], timeout=120)
            except Exception:  # noqa: BLE001 - the kill is the point
                pass
            finished.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not live_sentinel_pids():
            time.sleep(0.1)
        self.assertTrue(live_sentinel_pids(), "the child never started")

        main._stop_running_jobs()
        self.assertTrue(finished.wait(timeout=15), "the step never returned")
        time.sleep(0.5)
        self.assertEqual(live_sentinel_pids(), [])

    def test_a_finished_step_leaves_nothing_registered(self):
        main._run_subprocess(["sh", "-c", "exit 0"], timeout=30)
        self.assertEqual(main._live_processes, set())

    # ── One job-wide budget, not one per step ────────────────────────────────

    def test_a_step_may_not_outlast_the_job_budget(self):
        """Many steps used to multiply the per-step ceiling without limit."""
        token = main._job_deadline.set(time.monotonic() + 5)
        try:
            self.assertLessEqual(main._step_timeout(), 5.0)
            self.assertLess(main._step_timeout(), main.SUBPROCESS_TIMEOUT_S)
        finally:
            main._job_deadline.reset(token)

    def test_a_step_outside_a_job_keeps_its_own_ceiling(self):
        self.assertEqual(main._step_timeout(), main.SUBPROCESS_TIMEOUT_S)

    def test_a_spent_budget_ends_the_job_instead_of_starting_a_step(self):
        token = main._job_deadline.set(time.monotonic() - 1)
        try:
            with self.assertRaises(main.HTTPException) as caught:
                main._step_timeout()
            self.assertEqual(caught.exception.status_code, 504)
        finally:
            main._job_deadline.reset(token)

    # ── A bounded queue, with an honest phase ────────────────────────────────

    def test_a_waiting_job_says_it_is_queued_and_does_not_start_early(self):
        """One job at a time, and the one in line says so rather than looking
        stuck or, worse, appearing finished."""
        job_id = str(uuid.uuid4())
        holder_in, release_holder = threading.Event(), threading.Event()
        waiter_in, release_waiter = threading.Event(), threading.Event()

        def hold(entered, release, slot_job_id=None):
            with main.job_slot(slot_job_id):
                entered.set()
                release.wait(timeout=30)

        holder = threading.Thread(target=hold, args=(holder_in, release_holder),
                                  daemon=True)
        holder.start()
        self.assertTrue(holder_in.wait(timeout=10), "the first job never started")

        waiter = threading.Thread(target=hold,
                                  args=(waiter_in, release_waiter, job_id),
                                  daemon=True)
        waiter.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if main._read_job(job_id)["phase"] == main.JOB_PHASE_QUEUED:
                    break
                time.sleep(0.05)
            self.assertEqual(main._read_job(job_id)["phase"], main.JOB_PHASE_QUEUED)
            self.assertFalse(waiter_in.is_set(),
                             "the second job must not run beside the first")
        finally:
            release_holder.set()
            entered = waiter_in.wait(timeout=10)
            release_waiter.set()
            holder.join(timeout=10)
            waiter.join(timeout=10)
        self.assertTrue(entered, "the waiting job never got its turn")

    def test_the_queue_is_bounded_rather_than_unbounded(self):
        """Past the cap the server says so, instead of accepting work it cannot
        start and holding the memory until it can."""
        original = main.MAX_WAITING_JOBS
        main.MAX_WAITING_JOBS = 0
        try:
            with self.assertRaises(main.HTTPException) as caught:
                with main.job_slot():
                    pass
            self.assertEqual(caught.exception.status_code, 503)
        finally:
            main.MAX_WAITING_JOBS = original

    def test_the_slot_is_released_even_when_the_job_fails(self):
        with self.assertRaises(ValueError):
            with main.job_slot():
                raise ValueError("job blew up")
        with main.job_slot():  # would block forever if the slot leaked
            pass

    # ── Abandoned scratch, and only that ─────────────────────────────────────

    def test_a_restart_reclaims_scratch_an_interrupted_job_left(self):
        abandoned = str(uuid.uuid4())
        for root in (main.INPUT_DIR, main.TMP_DIR):
            (root / abandoned).mkdir(parents=True, exist_ok=True)
            (root / abandoned / "leftover.png").write_bytes(tiny_png())

        main._reclaim_abandoned_jobs()

        for root in (main.INPUT_DIR, main.TMP_DIR):
            self.assertFalse((root / abandoned).exists(), root)

    def test_reclaiming_never_touches_outputs_thumbnails_or_hand_made_folders(self):
        kept = main.OUTPUT_DIR / "kept_across_restart.gif"
        kept.write_bytes(tiny_png())
        main.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        cached = main.THUMB_DIR / "cached.jpg"
        cached.write_bytes(b"thumbnail bytes")
        mine = main.TMP_DIR / "my own folder"
        mine.mkdir(parents=True, exist_ok=True)
        try:
            main._reclaim_abandoned_jobs()
            self.assertTrue(kept.is_file(), "a kept output must survive a restart")
            self.assertTrue(cached.is_file(), "the thumbnail cache is not job scratch")
            self.assertTrue(mine.is_dir(), "only job-shaped directories are reclaimed")
        finally:
            kept.unlink(missing_ok=True)
            cached.unlink(missing_ok=True)
            mine.rmdir()

    def test_only_job_shaped_names_count_as_scratch(self):
        self.assertTrue(main._is_job_directory(str(uuid.uuid4())))
        self.assertFalse(main._is_job_directory("thumbs"))
        self.assertFalse(main._is_job_directory("magicflow_tmp_something"))


class JobServiceTest(unittest.TestCase):
    """End-to-end: what a second request sees while the first one encodes."""

    @classmethod
    def setUpClass(cls):
        import uvicorn

        ensure_data_dirs()

        cls._server = uvicorn.Server(uvicorn.Config(
            main.app, host=HOST, port=PORT, log_level="warning"))
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if timed_get("/health")[1].get("status") == "ok":
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
        for leftover in {f.name for f in main.OUTPUT_DIR.iterdir() if f.is_file()} - self.before:
            (main.OUTPUT_DIR / leftover).unlink(missing_ok=True)
        for pid in live_sentinel_pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def run_slow_combine(self, job_id, seconds=SLOW_ENCODE_S):
        """Start a Combine whose encode step takes `seconds`, and return the
        thread plus a dict that will hold its result."""
        outcome = {}
        original = main.build_assembly_command
        main.build_assembly_command = slow_encoder(seconds)

        def run():
            try:
                outcome["result"] = post(
                    "/convert",
                    {"format": "gif", "keep_output": "true", "job_id": job_id},
                    [("files", "a.png", tiny_png())])
            finally:
                main.build_assembly_command = original

        worker = threading.Thread(target=run)
        worker.start()
        return worker, outcome

    def test_health_and_progress_answer_while_a_job_encodes(self):
        """The reproduced defect: a concurrent poll waited out the whole encode
        because the blocking work owned the only event loop."""
        job_id = str(uuid.uuid4())
        worker, _ = self.run_slow_combine(job_id)
        try:
            time.sleep(1.5)  # well inside the slow encode
            health_latency, health = timed_get("/health")
            progress_latency, progress = timed_get(f"/progress/{job_id}")
        finally:
            worker.join(timeout=120)

        self.assertLess(health_latency, RESPONSIVE_S, f"/health waited {health_latency:.2f}s")
        self.assertEqual(health["status"], "ok")
        self.assertLess(progress_latency, RESPONSIVE_S,
                        f"/progress waited {progress_latency:.2f}s")
        self.assertIn(progress["phase"],
                      (main.JOB_PHASE_QUEUED, main.JOB_PHASE_TRANSFORMING,
                       main.JOB_PHASE_ENCODING),
                      "a running job must report a live phase, not 'unknown'")

    def test_the_outcome_is_still_observable_after_the_response(self):
        """The record used to be deleted as the response was written, so a poll
        already in flight learned nothing about how the job ended."""
        job_id = str(uuid.uuid4())
        status, payload = post("/convert",
                               {"format": "gif", "keep_output": "true",
                                "job_id": job_id},
                               [("files", "a.png", tiny_png((10, 200, 10))),
                                ("files", "b.png", tiny_png((200, 10, 10)))])
        self.assertEqual(status, 200, payload)
        self.assertEqual(timed_get(f"/progress/{job_id}")[1]["phase"],
                         main.JOB_PHASE_DONE)

    def test_a_failed_job_reports_that_it_failed(self):
        job_id = str(uuid.uuid4())
        original = main.build_assembly_command
        main.build_assembly_command = lambda frames, output, *a, **k: [
            "sh", "-c", "exit 1"]
        try:
            status, _ = post("/convert",
                             {"format": "gif", "keep_output": "true",
                              "job_id": job_id},
                             [("files", "a.png", tiny_png())])
        finally:
            main.build_assembly_command = original
        self.assertEqual(status, 500)
        self.assertEqual(timed_get(f"/progress/{job_id}")[1]["phase"],
                         main.JOB_PHASE_FAILED)

    def test_a_terminal_record_is_evicted_once_it_is_stale(self):
        """Observable for a while, not for ever: the map must stay bounded."""
        job_id = str(uuid.uuid4())
        main._finish_job(job_id, main.JOB_PHASE_DONE)
        with main._jobs_lock:
            JOBS = main.JOBS
            JOBS[job_id]["finished"] -= main.JOB_RECORD_TTL_S + 1
        main._finish_job(str(uuid.uuid4()), main.JOB_PHASE_DONE)  # triggers eviction
        self.assertEqual(main._read_job(job_id)["phase"], main.JOB_PHASE_UNKNOWN)

    def test_an_unknown_job_is_still_reported_as_unknown(self):
        self.assertEqual(timed_get(f"/progress/{uuid.uuid4()}")[1]["phase"],
                         main.JOB_PHASE_UNKNOWN)

    def test_two_tabs_each_get_their_own_progress(self):
        """Duplicate work from two tabs must not confuse one job for the other;
        FIX-004 made the outputs distinct, this keeps the progress distinct."""
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        for job_id in (first, second):
            status, payload = post("/convert",
                                   {"format": "gif", "keep_output": "true",
                                    "job_id": job_id},
                                   [("files", "a.png", tiny_png())])
            self.assertEqual(status, 200, payload)
        self.assertEqual(timed_get(f"/progress/{first}")[1]["phase"],
                         main.JOB_PHASE_DONE)
        self.assertEqual(timed_get(f"/progress/{second}")[1]["phase"],
                         main.JOB_PHASE_DONE)

    def test_a_thumbnail_does_not_wait_behind_an_encode(self):
        """Browsing the library stays usable while a conversion runs: only the
        four producers take the job slot."""
        status, payload = post("/convert",
                               {"format": "gif", "keep_output": "true",
                                "custom_name": "thumb_while_busy"},
                               [("files", "a.png", tiny_png((10, 200, 10))),
                                ("files", "b.png", tiny_png((200, 10, 10)))])
        self.assertEqual(status, 200, payload)
        name = payload["output_filename"]
        with urllib.request.urlopen(BASE + f"/thumb/{name}", timeout=60) as warm:
            self.assertEqual(warm.status, 200)  # cache it, so the timing is the wait

        worker, _ = self.run_slow_combine(str(uuid.uuid4()))
        try:
            time.sleep(1.5)
            started = time.monotonic()
            with urllib.request.urlopen(BASE + f"/thumb/{name}", timeout=60) as response:
                self.assertEqual(response.status, 200)
            latency = time.monotonic() - started
        finally:
            worker.join(timeout=120)
        self.assertLess(latency, RESPONSIVE_S, f"/thumb waited {latency:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
