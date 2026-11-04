"""Regression coverage for FIX-002: downloads are read-only and writes are
same-origin only.

Needs the app's runtime (FastAPI/uvicorn), so it runs inside the container with
the source mounted, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_*.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import os
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, "/app")

import main  # noqa: E402  (after sys.path setup)

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_TEST_PORT", "8899"))
BASE = f"http://{HOST}:{PORT}"
FOREIGN_ORIGIN = "https://evil.example"

KEPT_NAME = "kept_output.png"
TEMP_NAME = f"{main.TMP_OUTPUT_PREFIX}job1.png"


def tiny_png(rgb=(10, 200, 10), width=32, height=24):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    chunk = lambda tag, data: (  # noqa: E731
        struct.pack(">I", len(data)) + tag + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def request(path, method="GET", origin=None, host=None, body=None, content_type=None):
    """Return (status, body_bytes). HTTP error responses are results, not raises."""
    req = urllib.request.Request(BASE + path, method=method, data=body)
    if origin is not None:
        req.add_header("Origin", origin)
    if host is not None:
        req.add_header("Host", host)
    if content_type is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class DownloadLifecycleTest(unittest.TestCase):
    """The server runs once for the whole class; each test reseeds the outputs."""

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
        self.kept = main.OUTPUT_DIR / KEPT_NAME
        self.kept_recipe = main.RECIPE_DIR / (KEPT_NAME + ".json")
        self.temp = main.OUTPUT_DIR / TEMP_NAME
        self.kept.write_bytes(tiny_png())
        self.kept_recipe.write_text('{"direction": "convert"}')
        self.temp.write_bytes(tiny_png((200, 10, 10)))
        # Since FIX-003 a file is temporary because the server registered it,
        # not because of its name, so the fixture must claim it the same way a
        # producer does. tests/test_retention.py owns that rule itself.
        main._register_temp_output(TEMP_NAME)

    def tearDown(self):
        for leftover in (self.kept, self.kept_recipe, self.temp):
            leftover.unlink(missing_ok=True)
        main.TEMP_REGISTRY_FILE.unlink(missing_ok=True)

    # ── GET is read-only ──────────────────────────────────────────────────────

    def test_download_does_not_delete_a_kept_output(self):
        status, body = request(f"/download/{KEPT_NAME}")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.kept.read_bytes())
        self.assertTrue(self.kept.exists(), "a download must never remove the file")

    def test_legacy_delete_query_parameter_no_longer_deletes(self):
        """The reproduced defect: ?delete=true unlinked any named output."""
        status, _ = request(f"/download/{KEPT_NAME}?delete=true")
        self.assertEqual(status, 200)
        self.assertTrue(self.kept.exists(), "?delete=true must be inert")
        self.assertTrue(self.kept_recipe.exists(), "recipe must not be orphaned")

    def test_temporary_output_survives_its_download_so_it_can_be_retried(self):
        self.assertEqual(request(f"/download/{TEMP_NAME}")[0], 200)
        self.assertTrue(self.temp.exists())
        status, body = request(f"/download/{TEMP_NAME}")  # retry
        self.assertEqual(status, 200)
        self.assertEqual(body, self.temp.read_bytes())

    def test_download_still_rejects_unsafe_names_and_missing_files(self):
        # Reaches the handler, which rejects anything sanitization would alter.
        self.assertEqual(request("/download/.hidden.png")[0], 400)
        self.assertEqual(request("/download/no_such_file.png")[0], 404)

    def test_download_cannot_escape_the_output_directory(self):
        marker = b"OUTSIDE_OUTPUT_DIR_MUST_NOT_BE_SERVED"
        main.SETTINGS_FILE.write_bytes(marker)
        try:
            # Encoded separators are decoded before routing, so these never
            # match the single-segment route; a leading dot is caught in the
            # handler. Whichever rejects it, the bytes must not come back.
            for attempt in ("/download/..%2Fsettings.json",
                            "/download/..%2F..%2Fconfig%2Fsettings.json",
                            "/download/.hidden.png"):
                with self.subTest(attempt=attempt):
                    status, body = request(attempt)
                    self.assertIn(status, (400, 404))
                    self.assertNotIn(marker, body)
            self.assertEqual(main.SETTINGS_FILE.read_bytes(), marker, "must stay untouched")
        finally:
            main.SETTINGS_FILE.unlink(missing_ok=True)

    # ── Explicit release of temporary outputs ─────────────────────────────────

    def test_release_discards_a_temporary_output(self):
        status, _ = request(f"/files/temp/{TEMP_NAME}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertFalse(self.temp.exists())

    def test_release_refuses_to_touch_a_kept_output(self):
        status, _ = request(f"/files/temp/{KEPT_NAME}", method="DELETE")
        self.assertEqual(status, 403)
        self.assertTrue(self.kept.exists(), "release must not reach kept files")

    def test_release_reports_unsafe_and_missing_names(self):
        self.assertEqual(request("/files/temp/.hidden.png", method="DELETE")[0], 400)
        self.assertIn(request("/files/temp/..%2Fsettings.json", method="DELETE")[0], (400, 404))
        # Registered, but the file is already gone — distinct from "not ours".
        gone = f"{main.TMP_OUTPUT_PREFIX}absent.png"
        main._register_temp_output(gone)
        self.assertEqual(request(f"/files/temp/{gone}", method="DELETE")[0], 404)
        self.assertEqual(request(f"/files/temp/{main.TMP_OUTPUT_PREFIX}never.png",
                                 method="DELETE")[0], 403, "unregistered name is not ours")

    # ── Same-origin writes only ───────────────────────────────────────────────

    def test_foreign_origin_cannot_delete_a_file(self):
        status, _ = request(f"/files/{KEPT_NAME}", method="DELETE", origin=FOREIGN_ORIGIN)
        self.assertEqual(status, 403)
        self.assertTrue(self.kept.exists())

    def test_foreign_origin_cannot_release_a_temporary_output(self):
        status, _ = request(f"/files/temp/{TEMP_NAME}", method="DELETE", origin=FOREIGN_ORIGIN)
        self.assertEqual(status, 403)
        self.assertTrue(self.temp.exists())

    def test_foreign_origin_cannot_write_settings(self):
        status, _ = request("/settings", method="PUT", origin=FOREIGN_ORIGIN,
                            body=b'{"setting-quality": "7"}', content_type="application/json")
        self.assertEqual(status, 403)

    def test_opaque_origin_is_not_trusted(self):
        status, _ = request(f"/files/{KEPT_NAME}", method="DELETE", origin="null")
        self.assertEqual(status, 403)
        self.assertTrue(self.kept.exists())

    def test_same_origin_write_is_allowed(self):
        status, _ = request(f"/files/{KEPT_NAME}", method="DELETE", origin=f"http://{HOST}:{PORT}")
        self.assertEqual(status, 200)
        self.assertFalse(self.kept.exists())

    def test_write_without_an_origin_header_is_allowed(self):
        """Non-browser clients (curl, scripts) send no Origin and keep working."""
        status, _ = request(f"/files/{KEPT_NAME}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertFalse(self.kept.exists())

    def test_reads_are_never_blocked_by_origin(self):
        for path in ("/health", "/files", f"/download/{KEPT_NAME}"):
            with self.subTest(path=path):
                self.assertEqual(request(path, origin=FOREIGN_ORIGIN)[0], 200)

    def test_proxy_host_is_trusted_when_origin_matches_it(self):
        """Behind a reverse proxy, Origin and Host both carry the public name."""
        proxy_host = "magicflow.example.com"
        status, _ = request(f"/files/{KEPT_NAME}", method="DELETE",
                            origin=f"https://{proxy_host}", host=proxy_host)
        self.assertEqual(status, 200)

    def test_allowed_origins_override_covers_host_rewriting_proxies(self):
        configured = "https://magicflow.example.com"
        self.assertFalse(main._origin_is_trusted(configured, "backend:3876"))
        original = main.ALLOWED_ORIGINS
        main.ALLOWED_ORIGINS = frozenset({configured})
        try:
            self.assertTrue(main._origin_is_trusted(configured, "backend:3876"))
            self.assertFalse(main._origin_is_trusted(FOREIGN_ORIGIN, "backend:3876"))
        finally:
            main.ALLOWED_ORIGINS = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
