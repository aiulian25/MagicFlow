"""Regression coverage for FIX-003: a temporary output is identified by an
explicit registry entry, never by its filename.

Needs the app's runtime (FastAPI/uvicorn/ImageMagick), so it runs inside the
container with the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_*.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import json
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zlib

sys.path.insert(0, "/app")

import main  # noqa: E402  (after sys.path setup)

HOST = "127.0.0.1"
PORT = 8898
BASE = f"http://{HOST}:{PORT}"

AN_HOUR = 3600
LEGACY_NAME = f"{main.TMP_OUTPUT_PREFIX}legacy_from_old_build.png"
KEPT_RESERVED_NAME = f"{main.TMP_OUTPUT_PREFIX}kept.png"  # the reproduced case


def tiny_png(rgb=(10, 200, 10), width=32, height=24):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    chunk = lambda tag, data: (  # noqa: E731
        struct.pack(">I", len(data)) + tag + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def request(path, method="GET", body=None, content_type=None):
    req = urllib.request.Request(BASE + path, method=method, data=body)
    if content_type is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def multipart(fields, file_field, filename, file_bytes, file_type="image/png"):
    """Minimal multipart/form-data body, so a real producer route can run."""
    boundary = f"----magicflow{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {file_type}\r\n\r\n".encode()
        + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def listed_filenames():
    status, body = request("/files")
    assert status == 200, status
    return [f["filename"] for f in json.loads(body)["files"]]


class RetentionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import uvicorn

        for directory in (main.INPUT_DIR, main.OUTPUT_DIR, main.TMP_DIR,
                          main.CONFIG_DIR, main.THUMB_DIR, main.RECIPE_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        cls._server = uvicorn.Server(uvicorn.Config(
            main.app, host=HOST, port=PORT, log_level="warning"))
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if request("/health")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise RuntimeError("test server did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls._server.should_exit = True
        cls._thread.join(timeout=15)

    def setUp(self):
        main.TEMP_REGISTRY_FILE.unlink(missing_ok=True)
        for leftover in list(main.OUTPUT_DIR.glob("*.png")) + list(main.OUTPUT_DIR.glob("*.jpg")):
            leftover.unlink(missing_ok=True)

    tearDown = setUp

    def age_entry(self, name, seconds_past_expiry=60):
        """Rewrite one registry entry so its recorded expiry is in the past."""
        entries = main._read_temp_registry()
        entries[name] = {"expires": time.time() - seconds_past_expiry}
        main._write_temp_registry(entries)

    # ── The reproduced defect ────────────────────────────────────────────────

    def test_kept_output_named_like_a_temporary_is_never_swept(self):
        """A keep_output file called magicflow_tmp_kept.png used to be deleted
        once its mtime passed an hour and /files was called."""
        kept = main.OUTPUT_DIR / KEPT_RESERVED_NAME
        kept.write_bytes(tiny_png())
        import os
        long_ago = time.time() - (5 * AN_HOUR)
        os.utime(kept, (long_ago, long_ago))

        main._sweep_tmp_outputs()
        self.assertTrue(kept.exists(), "an unregistered file must never be swept")
        self.assertIn(KEPT_RESERVED_NAME, listed_filenames(),
                      "it is not owned as temporary, so it must be visible")
        self.assertTrue(kept.exists(), "listing must not delete it either")

    def test_producers_refuse_a_kept_name_using_the_reserved_prefix(self):
        body, content_type = multipart(
            {"out_format": "png", "keep_output": "true", "custom_name": f"{main.TMP_OUTPUT_PREFIX}evil"},
            "file", "src.png", tiny_png())
        status, payload = request("/convert-image", method="POST", body=body,
                                  content_type=content_type)
        self.assertEqual(status, 400)
        self.assertIn(main.TMP_OUTPUT_PREFIX, json.loads(payload)["detail"])

    def test_an_ordinary_custom_name_is_still_accepted(self):
        body, content_type = multipart(
            {"out_format": "png", "keep_output": "true", "custom_name": "holiday_shot"},
            "file", "src.png", tiny_png())
        status, payload = request("/convert-image", method="POST", body=body,
                                  content_type=content_type)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["output_filename"], "holiday_shot.png")

    # ── Only registered entries expire ───────────────────────────────────────

    def test_registered_temporary_expires_once_its_recorded_expiry_passes(self):
        name = f"{main.TMP_OUTPUT_PREFIX}expired.png"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        main._register_temp_output(name)
        self.age_entry(name)

        main._sweep_tmp_outputs()
        self.assertFalse((main.OUTPUT_DIR / name).exists())
        self.assertNotIn(name, main._read_temp_registry(), "entry must be dropped too")

    def test_registered_temporary_survives_until_it_expires(self):
        name = f"{main.TMP_OUTPUT_PREFIX}fresh.png"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        main._register_temp_output(name)

        main._sweep_tmp_outputs()
        self.assertTrue((main.OUTPUT_DIR / name).exists())
        self.assertIn(name, main._read_temp_registry())

    def test_expiry_uses_recorded_time_not_the_files_mtime(self):
        """Touching a file must not extend or shorten its lifetime."""
        name = f"{main.TMP_OUTPUT_PREFIX}mtime.png"
        path = main.OUTPUT_DIR / name
        path.write_bytes(tiny_png())
        main._register_temp_output(name)
        self.age_entry(name)
        import os
        now = time.time()
        os.utime(path, (now, now))  # brand-new mtime, already-passed expiry

        main._sweep_tmp_outputs()
        self.assertFalse(path.exists(), "recorded expiry decides, not mtime")

    def test_legacy_prefixed_file_is_left_alone_and_listed(self):
        legacy = main.OUTPUT_DIR / LEGACY_NAME
        legacy.write_bytes(tiny_png())
        import os
        long_ago = time.time() - (5 * AN_HOUR)
        os.utime(legacy, (long_ago, long_ago))

        main._sweep_tmp_outputs()
        self.assertTrue(legacy.exists())
        self.assertIn(LEGACY_NAME, listed_filenames())

    def test_a_derived_name_may_carry_the_prefix_and_is_still_safe(self):
        """Re-converting a prefix-named source derives e.g. '..._re.gif', which
        the custom-name guard cannot reach. The registry is what makes it safe:
        unregistered means never swept and always listed."""
        derived = f"{main.TMP_OUTPUT_PREFIX}legacy_re.gif"
        path = main.OUTPUT_DIR / derived
        path.write_bytes(tiny_png())
        import os
        long_ago = time.time() - (10 * AN_HOUR)
        os.utime(path, (long_ago, long_ago))

        main._sweep_tmp_outputs()
        self.assertTrue(path.exists())
        self.assertIn(derived, listed_filenames())
        path.unlink(missing_ok=True)

    def test_a_corrupt_registry_deletes_nothing(self):
        name = f"{main.TMP_OUTPUT_PREFIX}safe.png"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        main.TEMP_REGISTRY_FILE.write_text("{ not json")

        main._sweep_tmp_outputs()
        self.assertTrue((main.OUTPUT_DIR / name).exists(),
                        "an unreadable registry owns nothing, so it sweeps nothing")

    # ── Deletion takes the artifacts with it ─────────────────────────────────

    def test_expiry_also_removes_the_recipe_and_cached_thumbnail(self):
        name = f"{main.TMP_OUTPUT_PREFIX}artifacts.png"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        recipe = main.RECIPE_DIR / (name + ".json")
        recipe.write_text('{"direction": "convert"}')
        thumb = main.THUMB_DIR / f"{name}.12345.jpg"
        thumb.write_bytes(b"not-a-real-jpeg")
        main._register_temp_output(name)
        self.age_entry(name)

        main._sweep_tmp_outputs()
        self.assertFalse((main.OUTPUT_DIR / name).exists())
        self.assertFalse(recipe.exists(), "sidecar recipe must follow the file")
        self.assertFalse(thumb.exists(), "cached thumbnail must follow the file")

    # ── Listing and release follow registry ownership ────────────────────────

    def test_registered_temporary_is_hidden_from_the_library(self):
        name = f"{main.TMP_OUTPUT_PREFIX}hidden.png"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        main._register_temp_output(name)
        self.assertNotIn(name, listed_filenames())

    def test_release_accepts_a_registered_temporary_and_forgets_it(self):
        name = f"{main.TMP_OUTPUT_PREFIX}release.png"
        (main.OUTPUT_DIR / name).write_bytes(tiny_png())
        main._register_temp_output(name)

        status, _ = request(f"/files/temp/{name}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertFalse((main.OUTPUT_DIR / name).exists())
        self.assertNotIn(name, main._read_temp_registry())

    def test_release_refuses_an_unregistered_file_even_with_the_prefix(self):
        legacy = main.OUTPUT_DIR / LEGACY_NAME
        legacy.write_bytes(tiny_png())
        status, _ = request(f"/files/temp/{LEGACY_NAME}", method="DELETE")
        self.assertEqual(status, 403)
        self.assertTrue(legacy.exists(), "a name alone must not authorize deletion")

    # ── End to end through a real producer ───────────────────────────────────

    def test_not_kept_conversion_registers_hides_downloads_and_releases(self):
        body, content_type = multipart(
            {"out_format": "png", "keep_output": "false"}, "file", "src.png", tiny_png())
        status, payload = request("/convert-image", method="POST", body=body,
                                  content_type=content_type)
        self.assertEqual(status, 200)
        result = json.loads(payload)
        name = result["output_filename"]
        self.assertTrue(result["delete_after_download"])

        self.assertIn(name, main._read_temp_registry(), "producer must register it")
        self.assertNotIn(name, listed_filenames())
        self.assertEqual(request(f"/download/{name}")[0], 200)
        self.assertTrue((main.OUTPUT_DIR / name).exists(), "download is read-only")

        self.assertEqual(request(f"/files/temp/{name}", method="DELETE")[0], 200)
        self.assertFalse((main.OUTPUT_DIR / name).exists())

    def test_kept_conversion_is_never_registered(self):
        body, content_type = multipart(
            {"out_format": "png", "keep_output": "true", "custom_name": "keeper"},
            "file", "src.png", tiny_png())
        status, payload = request("/convert-image", method="POST", body=body,
                                  content_type=content_type)
        self.assertEqual(status, 200)
        name = json.loads(payload)["output_filename"]
        self.assertNotIn(name, main._read_temp_registry())
        self.assertIn(name, listed_filenames())
        main._sweep_tmp_outputs()
        self.assertTrue((main.OUTPUT_DIR / name).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
