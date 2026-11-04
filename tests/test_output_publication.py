"""Regression coverage for FIX-004: an output gets its identity only when it is
complete, and a name already taken is never overwritten.

Needs the app's runtime (FastAPI/uvicorn) and ImageMagick, so it runs inside the
container with the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_*.py' -v

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
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, "/app")

import main  # noqa: E402  (after sys.path setup)

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_PUBLISH_TEST_PORT", "8901"))
BASE = f"http://{HOST}:{PORT}"

# Marker bytes an injected encoder writes: non-empty, so only the readability
# probe can reject them.
PARTIAL_BYTES = b"PARTIAL-NOT-AN-IMAGE"
TIMEOUT_SENTINEL = "__inject_timeout__"


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
    """POST a multipart form. Returns (status, parsed JSON); HTTP errors are
    results, not raises."""
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
        with urllib.request.urlopen(request, timeout=180) as response:
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


@contextmanager
def encoder(replacement):
    """Swap the GIF/WebP/TIFF assembly command builder for an injected one."""
    original = main.build_assembly_command
    main.build_assembly_command = replacement
    try:
        yield
    finally:
        main.build_assembly_command = original


def shell_encoder(script):
    """Build a replacement command builder that runs `script` with the output
    path substituted for {out}."""
    def build(frames, output, *args, **kwargs):
        return ["sh", "-c", script.format(out=output)]
    return build


class OutputPublicationTest(unittest.TestCase):
    """The server runs once for the whole class; each test cleans up after itself."""

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
        self.before = self.library()

    def tearDown(self):
        for leftover in self.library() - self.before:
            (main.OUTPUT_DIR / leftover).unlink(missing_ok=True)
            (main.RECIPE_DIR / (leftover + ".json")).unlink(missing_ok=True)
        for staged in main.STAGING_DIR.iterdir():
            staged.unlink(missing_ok=True)
        main.TEMP_REGISTRY_FILE.unlink(missing_ok=True)

    # ── helpers ───────────────────────────────────────────────────────────────

    def library(self):
        """Names the library would list, by the same rule /files uses."""
        return {f.name for f in main.OUTPUT_DIR.iterdir()
                if f.is_file() and f.suffix.lower() in main.OUTPUT_EXTENSIONS}

    def combine(self, rgb=(10, 200, 10), **fields):
        form = {"format": "gif", "keep_output": "true", "delay": "100"}
        form.update(fields)
        return post("/convert", form, [("files", "frame.png", tiny_png(rgb))])

    def published(self):
        return sorted(self.library() - self.before)

    # ── A reused job id is a correlation token, not an identity ───────────────

    def test_a_reused_job_id_cannot_overwrite_an_earlier_result(self):
        """The reproduced defect: the client's job_id named the output, so a
        retried or re-submitted job replaced the first result in place."""
        job_id = str(uuid.uuid4())
        status, first = self.combine((10, 200, 10), job_id=job_id)
        self.assertEqual(status, 200, first)
        first_name = first["output_filename"]
        first_bytes = (main.OUTPUT_DIR / first_name).read_bytes()

        status, second = self.combine((200, 10, 10), job_id=job_id)
        self.assertEqual(status, 200, second)

        self.assertNotEqual(second["output_filename"], first_name)
        self.assertEqual((main.OUTPUT_DIR / first_name).read_bytes(), first_bytes,
                         "the earlier result must be untouched")
        self.assertTrue((main.OUTPUT_DIR / second["output_filename"]).is_file())

    def test_a_reused_job_id_is_still_accepted_for_progress(self):
        """Rejecting the duplicate would be the easy fix and the wrong one: the
        UI polls /progress/{job_id} and a retry legitimately reuses it."""
        job_id = str(uuid.uuid4())
        self.assertEqual(self.combine(job_id=job_id)[0], 200)
        self.assertEqual(self.combine(job_id=job_id)[0], 200)
        # Since FIX-006 a finished job keeps a terminal record for a while
        # instead of vanishing, so a poll that lands after the response still
        # learns the outcome. tests/test_job_lifecycle.py owns that rule.
        self.assertEqual(get_json(f"/progress/{job_id}")["phase"], "done",
                         "the second job's outcome is still observable")

    def test_an_invalid_job_id_is_still_rejected(self):
        self.assertEqual(self.combine(job_id="not-a-uuid")[0], 400)

    # ── Nothing incomplete ever gets an identity ──────────────────────────────

    def test_a_failed_encode_publishes_nothing(self):
        """The reproduced defect: the encoder wrote into the library directly, so
        a non-zero exit left its partial file listed in My Files."""
        with encoder(shell_encoder("printf PARTIAL > '{out}'; exit 1")):
            status, payload = self.combine()
        self.assertEqual(status, 500, payload)
        self.assertEqual(self.published(), [])
        self.assertEqual([f for f in get_json("/files")["files"]
                          if f["filename"] not in self.before], [])

    def test_a_timed_out_reconvert_publishes_nothing_and_keeps_its_source(self):
        """The reproduced defect: reconvert's cleanup caught HTTPException only,
        so a TimeoutExpired from the subprocess skipped it."""
        status, made = self.combine((10, 10, 200), custom_name="timeout_source")
        self.assertEqual(status, 200, made)
        source = made["output_filename"]
        source_bytes = (main.OUTPUT_DIR / source).read_bytes()
        after_source = self.library()

        original_build, original_run = main.build_command, main._run_or_500

        def sentinel_build(frames, output, **kwargs):
            return [TIMEOUT_SENTINEL, output]

        def injecting_run(cmd, job_tmp, run_env, label):
            if cmd and cmd[0] == TIMEOUT_SENTINEL:
                Path(cmd[1]).write_bytes(PARTIAL_BYTES)
                raise subprocess.TimeoutExpired(cmd, 120)
            return original_run(cmd, job_tmp, run_env, label)

        main.build_command, main._run_or_500 = sentinel_build, injecting_run
        try:
            status, payload = post(f"/reconvert/{source}",
                                   {"format": "gif", "keep_output": "true"})
        finally:
            main.build_command, main._run_or_500 = original_build, original_run

        self.assertEqual(status, 504, payload)
        self.assertEqual(self.library(), after_source, "nothing new, nothing lost")
        self.assertEqual((main.OUTPUT_DIR / source).read_bytes(), source_bytes)

    def test_an_encoder_that_writes_nothing_publishes_nothing(self):
        """Exit 0 with a 0-byte file — the animated-WebP defect's shape."""
        with encoder(shell_encoder(": > '{out}'")):
            status, payload = self.combine()
        self.assertEqual(status, 500, payload)
        self.assertIn("no output file", payload.get("detail", ""))
        self.assertEqual(self.published(), [])

    def test_a_truncated_but_nonzero_output_is_not_published(self):
        """Size alone cannot tell a finished file from a truncated one, so the
        staging file is decode-probed before it is given a name."""
        with encoder(shell_encoder("printf '%s' '" + PARTIAL_BYTES.decode() + "' > '{out}'")):
            status, payload = self.combine()
        self.assertEqual(status, 500, payload)
        self.assertIn("unreadable", payload.get("detail", ""))
        self.assertEqual(self.published(), [])

    def test_video_outputs_are_exempt_from_the_readability_probe(self):
        """identify delegates video to ffmpeg, which the policy does not grant,
        so an unreadable probe result there would mean nothing."""
        self.assertTrue(main._output_is_readable(Path("/data/output/absent.mp4")))
        self.assertTrue(main._output_is_readable(Path("/data/output/absent.webm")))
        self.assertFalse(main._output_is_readable(Path("/data/output/absent.gif")))

    def test_the_encoder_is_never_pointed_at_the_library(self):
        """The structural guarantee behind every case above: a job writes only to
        staging, so a crash mid-encode cannot leave a file with a real name."""
        seen = []
        original = main.build_assembly_command

        def recording(frames, output, *args, **kwargs):
            seen.append(Path(output).parent.resolve())
            return original(frames, output, *args, **kwargs)

        with encoder(recording):
            status, payload = self.combine()
        self.assertEqual(status, 200, payload)
        self.assertTrue(seen, "the assembly command was never built")
        for target in seen:
            self.assertEqual(target, main.STAGING_DIR.resolve(), target)

    def test_startup_clears_staging_files_an_interrupted_job_left(self):
        stale = main.STAGING_DIR / f"{uuid.uuid4()}.gif"
        stale.write_bytes(PARTIAL_BYTES)
        kept = main.OUTPUT_DIR / "survives_startup.gif"
        kept.write_bytes(tiny_png())
        try:
            main._clear_stale_staging()
            self.assertFalse(stale.exists())
            self.assertTrue(kept.is_file(), "the library is not touched")
        finally:
            kept.unlink(missing_ok=True)

    # ── A name already taken is never overwritten ─────────────────────────────

    def test_a_taken_name_is_claimed_without_overwriting_it(self):
        """Publication is an atomic exclusive create, so it does not matter when
        the name was taken — including between a check and a write."""
        taken = main.OUTPUT_DIR / "taken.gif"
        taken.write_bytes(tiny_png((1, 2, 3)))
        taken_bytes = taken.read_bytes()

        status, payload = self.combine((200, 10, 10), custom_name="taken")
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(payload["output_filename"], "taken.gif")
        self.assertEqual(taken.read_bytes(), taken_bytes)

    def test_the_same_custom_name_yields_two_distinct_outputs(self):
        first = self.combine((10, 200, 10), custom_name="my_clip")[1]
        second = self.combine((200, 10, 10), custom_name="my_clip")[1]
        names = {first["output_filename"], second["output_filename"]}
        self.assertEqual(len(names), 2, names)
        for name in names:
            self.assertTrue((main.OUTPUT_DIR / name).is_file())

    def test_a_repeated_reconvert_does_not_replace_its_first_result(self):
        source = self.combine((10, 10, 200), custom_name="repeat_source")[1]
        source_name = source["output_filename"]
        first = post(f"/reconvert/{source_name}",
                     {"format": "gif", "keep_output": "true"})[1]
        second = post(f"/reconvert/{source_name}",
                      {"format": "gif", "keep_output": "true"})[1]
        self.assertNotEqual(first["output_filename"], second["output_filename"])
        self.assertTrue((main.OUTPUT_DIR / first["output_filename"]).is_file())

    def test_the_recipe_follows_the_name_the_output_actually_got(self):
        """A sidecar under the requested name would describe someone else's file."""
        taken = main.OUTPUT_DIR / "recipe_taken.gif"
        taken.write_bytes(tiny_png((1, 2, 3)))

        status, payload = self.combine(custom_name="recipe_taken")
        self.assertEqual(status, 200, payload)
        published = payload["output_filename"]
        self.assertNotEqual(published, "recipe_taken.gif")
        self.assertTrue((main.RECIPE_DIR / (published + ".json")).is_file())
        self.assertFalse((main.RECIPE_DIR / "recipe_taken.gif.json").is_file(),
                         "no sidecar for the file this job did not write")

    # ── Neighbouring contracts stay intact ───────────────────────────────────

    def test_a_kept_output_is_listed_with_its_recipe(self):
        status, payload = self.combine(custom_name="normal_path")
        self.assertEqual(status, 200, payload)
        name = payload["output_filename"]
        self.assertEqual(payload["file_size"], (main.OUTPUT_DIR / name).stat().st_size)
        listed = {f["filename"]: f for f in get_json("/files")["files"]}
        self.assertIn(name, listed)
        self.assertEqual(listed[name]["recipe"]["direction"], "convert")

    def test_a_not_kept_output_is_registered_and_hidden(self):
        """FIX-003's ownership rule still holds now that the name is server-owned
        and assigned at publication."""
        status, payload = post("/convert-image",
                               {"out_format": "png", "keep_output": "false"},
                               [("file", "frame.png", tiny_png())])
        self.assertEqual(status, 200, payload)
        name = payload["output_filename"]
        self.assertTrue((main.OUTPUT_DIR / name).is_file())
        self.assertIn(name, main._read_temp_registry())
        self.assertNotIn(name, [f["filename"] for f in get_json("/files")["files"]])

    def test_a_reserved_custom_name_is_still_refused(self):
        status, payload = self.combine(custom_name=main.TMP_OUTPUT_PREFIX + "sneaky")
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.published(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
