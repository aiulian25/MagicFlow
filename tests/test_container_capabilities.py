"""Regression coverage for FIX-005: the image grants exactly the ImageMagick
rights the app advertises, and nothing more.

This suite tests the IMAGE, not the mounted source, so it must run inside a
freshly built image and will fail against an older one — that is the point. The
app's own PDF routes are exercised too, so the grant is verified through the
code paths users actually hit:

  docker build -t magicflow-magicflow:latest .
  docker run --rm -v "$PWD/tests:/tests:ro" -v "$(mktemp -d):/data" \
      --entrypoint python3 magicflow-magicflow:latest \
      -m unittest discover -s /tests -p 'test_container_capabilities.py' -v

Stdlib only (unittest + subprocess + urllib) — the repository has no test
dependencies.
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

POLICY_FILE = Path("/etc/ImageMagick-6/policy.xml")

# Coders Debian denies for a reason and this app never produces: PS/EPS/XPS all
# hand data to ghostscript, and the remote ones fetch over the network.
DENIED_CODERS = ("PS", "PS2", "PS3", "EPS", "XPS", "URL", "HTTP", "HTTPS")

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAGICFLOW_PDF_TEST_PORT", "8903"))
BASE = f"http://{HOST}:{PORT}"

PAGE_COUNT = 2  # every PDF these tests build has two pages

# The unprivileged account the container runs the app as (Dockerfile: useradd
# -u 1000 appuser). ImageMagick is driven as this user so the rights are
# verified for the identity that will actually use them, however the test
# process itself was started.
APPUSER_UID = 1000


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
    """POST a multipart form. Returns (status, body); JSON bodies are parsed."""
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
            raw = response.read()
            return response.status, raw
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def post_json(path, fields, files=()):
    status, raw = post(path, fields, files)
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {"detail": raw[:200].decode("utf-8", "replace")}


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read())


class PolicyTest(unittest.TestCase):
    """What the deployed policy file grants. No app, no server."""

    @classmethod
    def setUpClass(cls):
        cls.policy = POLICY_FILE.read_text()

    def test_the_pdf_coder_is_granted_read_and_write(self):
        """The reproduced defect: PDF was granted read only, on the incorrect
        assumption that ImageMagick's native PDF writer bypasses coder policy.
        It does not — the encoder is gated the same way the decoder is."""
        self.assertIn('rights="read|write" pattern="PDF"', self.policy)

    def test_ghostscript_and_remote_coders_stay_denied(self):
        for coder in DENIED_CODERS:
            with self.subTest(coder=coder):
                self.assertIn(f'rights="none" pattern="{coder}"', self.policy)

    def test_the_resource_limits_were_applied(self):
        """Same edit, same silent-failure risk: if the sed stopped matching, the
        build would ship Debian's small defaults."""
        for expected in ('name="memory" value="2GiB"',
                         'name="map" value="4GiB"',
                         'name="disk" value="8GiB"'):
            with self.subTest(limit=expected):
                self.assertIn(expected, self.policy)


class ImageMagickRightsTest(unittest.TestCase):
    """What ImageMagick actually permits, as the unprivileged runtime user."""

    @classmethod
    def setUpClass(cls):
        cls.work = Path("/data/tmp") / f"fix005-{uuid.uuid4().hex}"
        cls.work.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(cls.work, APPUSER_UID, APPUSER_UID)  # run_convert writes as appuser
        cls.red = cls.work / "red.png"
        cls.blue = cls.work / "blue.png"
        cls.red.write_bytes(tiny_png((200, 10, 10)))
        cls.blue.write_bytes(tiny_png((10, 10, 200)))

    @classmethod
    def tearDownClass(cls):
        for leftover in cls.work.iterdir():
            leftover.unlink(missing_ok=True)
        cls.work.rmdir()

    def run_convert(self, *args):
        """Drive ImageMagick as appuser. Dropping privileges here rather than
        relying on how the container was started keeps the result meaningful
        either way; the work directory lives on /data, which appuser owns."""
        drop = {"user": APPUSER_UID, "group": APPUSER_UID} if os.geteuid() == 0 else {}
        return subprocess.run(["convert", *args], capture_output=True, text=True,
                              timeout=120, cwd=str(self.work), **drop)

    def test_two_stills_write_a_two_page_pdf(self):
        target = self.work / "pages.pdf"
        result = self.run_convert(str(self.red), str(self.blue), str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(target.stat().st_size, 0)
        self.assertEqual(main._count_frames(str(target)), PAGE_COUNT)

    def test_a_written_pdf_rasterizes_back_into_its_pages(self):
        target = self.work / "roundtrip.pdf"
        self.assertEqual(self.run_convert(str(self.red), str(self.blue),
                                          str(target)).returncode, 0)
        result = self.run_convert("-density", "72", str(target), "-coalesce",
                                  str(self.work / "page_%02d.png"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(sorted(self.work.glob("page_*.png"))), PAGE_COUNT)

    def test_writing_a_ghostscript_coder_is_still_refused(self):
        for suffix in (".eps", ".ps"):
            with self.subTest(suffix=suffix):
                target = self.work / f"denied{suffix}"
                result = self.run_convert(str(self.red), str(target))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("security policy", result.stderr)
                # ImageMagick creates the destination before checking rights and
                # leaves the empty file behind, so "refused" means no content
                # was encoded, not that nothing was touched.
                self.assertEqual(target.stat().st_size if target.exists() else 0, 0)

    def test_the_grant_applies_to_the_unprivileged_runtime_user(self):
        """Not an incidental root privilege: the PDF above was written by the
        account the container drops to, which is who the app runs as."""
        target = self.work / "ownership.pdf"
        self.assertEqual(self.run_convert(str(self.red), str(self.blue),
                                          str(target)).returncode, 0)
        self.assertEqual(target.stat().st_uid, APPUSER_UID)


class PdfWorkflowTest(unittest.TestCase):
    """The app's own PDF routes — the workflows the README advertises."""

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

    def combine_pdf(self, **extra):
        fields = {"format": "pdf", "keep_output": "true", "delay": "100"}
        fields.update(extra)
        status, payload = post_json(
            "/convert", fields,
            [("files", "red.png", tiny_png((200, 10, 10))),
             ("files", "blue.png", tiny_png((10, 10, 200)))])
        if status == 200:
            self.created.append(payload["output_filename"])
        return status, payload

    def test_combining_two_stills_produces_a_readable_pdf(self):
        """The reproduced defect: this returned 500 with a policy error after
        the frames had already been prepared."""
        status, payload = self.combine_pdf()
        self.assertEqual(status, 200, payload)
        produced = main.OUTPUT_DIR / payload["output_filename"]
        self.assertTrue(produced.name.endswith(".pdf"))
        self.assertGreater(payload["file_size"], 0)
        self.assertEqual(main._count_frames(str(produced)), PAGE_COUNT)
        self.assertEqual(produced.read_bytes()[:5], b"%PDF-")

    def test_a_produced_pdf_lists_with_a_thumbnail(self):
        status, payload = self.combine_pdf(custom_name="pdf_card")
        self.assertEqual(status, 200, payload)
        name = payload["output_filename"]
        listed = {f["filename"] for f in get_json("/files")["files"]}
        self.assertIn(name, listed)
        with urllib.request.urlopen(BASE + f"/thumb/{name}", timeout=120) as thumb:
            self.assertEqual(thumb.status, 200)

    def test_extracting_a_produced_pdf_returns_its_pages(self):
        """The round trip the README promises: assemble a PDF, split it back."""
        status, payload = self.combine_pdf(custom_name="pdf_source")
        self.assertEqual(status, 200, payload)
        pdf_bytes = (main.OUTPUT_DIR / payload["output_filename"]).read_bytes()

        status, raw = post("/explode", {"out_format": "png"},
                           [("file", "pages.pdf", pdf_bytes)])
        self.assertEqual(status, 200, raw[:200])
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(len(archive.namelist()), PAGE_COUNT, archive.namelist())

    def test_reconverting_a_pdf_into_an_animation_works(self):
        status, payload = self.combine_pdf(custom_name="pdf_reconvert_source")
        self.assertEqual(status, 200, payload)
        source = payload["output_filename"]
        status, result = post_json(f"/reconvert/{source}",
                                   {"format": "gif", "keep_output": "true"})
        self.assertEqual(status, 200, result)
        self.created.append(result["output_filename"])
        self.assertEqual(result["frame_count"], PAGE_COUNT)

    def test_a_failed_pdf_job_still_publishes_nothing(self):
        """FIX-004's guarantee, checked on the format that used to fail: the
        grant must not become the only thing standing between a policy error
        and a zero-byte file in the library."""
        before = {f.name for f in main.OUTPUT_DIR.iterdir() if f.is_file()}
        original = main.build_assembly_command
        main.build_assembly_command = lambda frames, output, *a, **k: [
            "sh", "-c", f"printf PARTIAL > '{output}'; exit 1"]
        try:
            status, payload = post_json(
                "/convert", {"format": "pdf", "keep_output": "true"},
                [("files", "red.png", tiny_png())])
        finally:
            main.build_assembly_command = original
        self.assertEqual(status, 500, payload)
        self.assertEqual({f.name for f in main.OUTPUT_DIR.iterdir() if f.is_file()},
                         before)
        self.assertEqual(list(main.STAGING_DIR.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
