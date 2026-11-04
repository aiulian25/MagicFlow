"""Regression coverage for FIX-016 (server side): every producer returns the
same result contract, so one client path can finish any job.

Runs with the app's runtime inside the container, never against a real /data:

  docker run --rm -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \
      -v "$(mktemp -d):/data" --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_result_contract.py' -v

Stdlib only (unittest + urllib) — the repository has no test dependencies.
"""
import json
import os
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
PORT = int(os.environ.get("MAGICFLOW_CONTRACT_TEST_PORT", "8905"))
BASE = f"http://{HOST}:{PORT}"

# Present on every successful result, whichever producer made it.
CONTRACT_KEYS = {"output_filename", "download_url", "delete_after_download",
                 "file_size", "expires_in"}


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


class ResultContractTest(unittest.TestCase):

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
        self.created = []

    def tearDown(self):
        for name in self.created:
            (main.OUTPUT_DIR / name).unlink(missing_ok=True)
            (main.RECIPE_DIR / (name + ".json")).unlink(missing_ok=True)
        main.TEMP_REGISTRY_FILE.unlink(missing_ok=True)

    def track(self, payload):
        self.created.append(payload["output_filename"])
        return payload

    def combine(self, keep):
        status, payload = post("/convert",
                               {"format": "gif", "delay": "100",
                                "keep_output": "true" if keep else "false"},
                               [("files", "a.png", tiny_png((10, 200, 10))),
                                ("files", "b.png", tiny_png((200, 10, 10)))])
        self.assertEqual(status, 200, payload)
        return self.track(payload)

    def convert_image(self, keep):
        status, payload = post("/convert-image",
                               {"out_format": "png",
                                "keep_output": "true" if keep else "false"},
                               [("file", "a.png", tiny_png())])
        self.assertEqual(status, 200, payload)
        return self.track(payload)

    def reconvert(self, source, keep):
        status, payload = post(f"/reconvert/{source}",
                               {"format": "webp",
                                "keep_output": "true" if keep else "false"})
        self.assertEqual(status, 200, payload)
        return self.track(payload)

    def every_producer(self, keep):
        """One result from each of the three producers, same keep setting."""
        source = self.combine(keep=True)["output_filename"]
        return {
            "convert": self.combine(keep),
            "convert-image": self.convert_image(keep),
            "reconvert": self.reconvert(source, keep),
        }

    # ── One contract, three producers ────────────────────────────────────────

    def test_every_producer_returns_the_same_result_keys(self):
        """The defect this closes: each completion path read a different subset
        of the response, so two of the three lost the lifecycle entirely."""
        for keep in (True, False):
            for producer, payload in self.every_producer(keep).items():
                with self.subTest(producer=producer, keep=keep):
                    self.assertTrue(CONTRACT_KEYS.issubset(payload),
                                    f"missing {CONTRACT_KEYS - set(payload)}")

    def test_a_kept_result_is_not_marked_for_deletion_and_never_expires(self):
        for producer, payload in self.every_producer(keep=True).items():
            with self.subTest(producer=producer):
                self.assertFalse(payload["delete_after_download"])
                self.assertIsNone(payload["expires_in"])

    def test_a_temporary_result_carries_both_halves_of_its_lifecycle(self):
        """Deletion on download AND an expiry, so a client can promise the user
        a retry window instead of implying the work is lost."""
        for producer, payload in self.every_producer(keep=False).items():
            with self.subTest(producer=producer):
                self.assertTrue(payload["delete_after_download"])
                self.assertEqual(payload["expires_in"], main.TEMP_OUTPUT_TTL_S)
                self.assertGreater(payload["expires_in"], 0)

    def test_the_declared_identity_is_the_one_that_can_be_fetched(self):
        for producer, payload in self.every_producer(keep=False).items():
            with self.subTest(producer=producer):
                name = payload["output_filename"]
                self.assertEqual(payload["download_url"], f"/download/{name}")
                with urllib.request.urlopen(BASE + payload["download_url"],
                                            timeout=60) as response:
                    self.assertEqual(len(response.read()), payload["file_size"])

    def test_a_declared_temporary_is_exactly_what_release_accepts(self):
        """The client acts on delete_after_download alone, so the flag and the
        server's own ownership record must never disagree."""
        payload = self.combine(keep=False)
        name = payload["output_filename"]
        self.assertTrue(payload["delete_after_download"])
        self.assertIn(name, main._read_temp_registry())

        request = urllib.request.Request(
            BASE + "/files/temp/" + name, method="DELETE")
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.status, 200)
        self.assertFalse((main.OUTPUT_DIR / name).exists())

    def test_a_kept_result_is_refused_by_release(self):
        payload = self.combine(keep=True)
        name = payload["output_filename"]
        request = urllib.request.Request(
            BASE + "/files/temp/" + name, method="DELETE")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=30)
        self.assertEqual(caught.exception.code, 403)
        self.assertTrue((main.OUTPUT_DIR / name).is_file())

    def test_reconvert_keeps_its_own_extra_fields(self):
        """The shared payload must not have cost Re-convert its specifics."""
        source = self.combine(keep=True)["output_filename"]
        payload = self.reconvert(source, keep=False)
        self.assertIn("frame_count", payload)
        self.assertGreater(payload["frame_count"], 0)
        self.assertIn("timing", payload)

    # ── The batch download path the client now uses ──────────────────────────

    def test_the_archive_endpoint_serves_temporary_results(self):
        """Download All asks for one ZIP; temporaries are hidden from /files, so
        the archive has to accept them by name or the batch path breaks."""
        import io
        import zipfile

        names = [self.convert_image(keep=False)["output_filename"] for _ in range(2)]
        listed = {f["filename"] for f in get_json("/files")["files"]}
        self.assertTrue(set(names).isdisjoint(listed), "temporaries stay hidden")

        request = urllib.request.Request(
            BASE + "/files/archive", method="POST",
            data=json.dumps({"filenames": names}).encode())
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=60) as response:
            self.assertEqual(response.status, 200)
            archive = response.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            self.assertEqual(sorted(zf.namelist()), sorted(names))
        for name in names:
            self.assertTrue((main.OUTPUT_DIR / name).is_file(),
                            "archiving must not consume the results")


if __name__ == "__main__":
    unittest.main(verbosity=2)
