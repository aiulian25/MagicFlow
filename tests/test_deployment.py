"""FIX-025 final-image checks. Run offline in a disposable container, /data fresh.
Host lifecycle/restore verification: scripts/verify-deployment.py IMAGE LOG_DIR.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest


class ReleaseImageTest(unittest.TestCase):
    def test_manifest_matches_installed_files_and_packages(self):
        manifest = json.loads(Path('/app/release-manifest.json').read_text())
        files = manifest['files']
        for name, digest in files.items():
            path = Path(name) if name.startswith('/') else Path('/app') / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, name)
        self.assertEqual(manifest['source_sha256'], hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())
        installed = subprocess.check_output(['dpkg-query', '-W', '-f=${Package}=${Version}\n'], text=True).splitlines()
        self.assertEqual(installed, manifest['system_packages'])
        self.assertTrue(json.loads(Path('/app/python-sbom.json').read_text())['installed'])
        import importlib.metadata
        for line in Path('/app/requirements.txt').read_text().splitlines():
            if line and not line.startswith('#'):
                name, version = line.split()[0].split('==')
                self.assertEqual(importlib.metadata.version(name), version)
        dockerfile = Path('/app/build-locks/Dockerfile').read_text()
        for key in ('node', 'python', 'debian_snapshot'):
            self.assertIn(manifest['build_inputs'][key], dockerfile)
        subprocess.run(['python3', '-m', 'pip', 'check'], check=True, capture_output=True)

    def test_snapshot_transport_accepts_only_the_signed_index_hash(self):
        import ast
        import io
        import tempfile
        import urllib.request
        import urllib.parse
        from unittest.mock import patch
        source = ast.parse(Path('/app/build-locks/fetch-debs.py').read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'fetch')
        with tempfile.TemporaryDirectory() as work:
            cache = Path(work)
            namespace = {'Path': lambda path: cache if path == '/var/cache/apt/archives' else Path(path),
                         'urllib': urllib, 'hashlib': hashlib,
                         'checksums': {'artifact.deb': hashlib.sha256(b'good').hexdigest()}}
            exec(compile(ast.Module(body=[function], type_ignores=[]), '<shipped snapshot transport>', 'exec'), namespace)
            request = ['https://snapshot.debian.org/archive/debian/20261102T000000Z/pool/main/artifact.deb', 'artifact.deb', '4']
            with patch('urllib.request.urlopen', side_effect=lambda *a, **kw: io.BytesIO(b'good')):
                namespace['fetch'](request)
            self.assertEqual((cache / 'artifact.deb').read_bytes(), b'good')
            (cache / 'artifact.deb').unlink()
            with patch('urllib.request.urlopen', side_effect=lambda *a, **kw: io.BytesIO(b'evil')):
                with self.assertRaises(RuntimeError):
                    namespace['fetch'](request)
            self.assertEqual(list(cache.iterdir()), [])

    def test_api_exposes_the_artifact_identity_and_detected_formats(self):
        import sys
        sys.path.insert(0, '/app')
        import main
        manifest = json.loads(Path('/app/release-manifest.json').read_text())
        config = asyncio.run(main.get_config())
        for key in ('version', 'revision', 'source_sha256'):
            self.assertEqual(config['build'][key], manifest[key])
        self.assertTrue(manifest['capabilities']['animation_outputs'])
        self.assertTrue(manifest['capabilities']['still_outputs'])

    def test_entrypoint_runs_as_service_user_and_preserves_correct_file_metadata(self):
        target = Path('/data/output/preserved.txt')
        target.write_text('retained fixture')
        subprocess.run(['chown', 'appuser:appuser', str(target)], check=True)
        before = target.stat()
        result = subprocess.run(['/entrypoint.sh', 'id', '-u'], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), '1000')
        self.assertEqual(target.stat().st_ctime_ns, before.st_ctime_ns)

    def test_explicit_migration_repairs_nested_owner_mismatch_without_following_links(self):
        root = Path('/data/output')
        target = root / 'old-owned.txt'
        target.write_text('existing output')
        target.chmod(0o600)
        outside = Path('/tmp/deployment-outside')
        outside.write_text('outside remains root owned')
        link = root / 'outside-link';link.symlink_to(outside)
        try:
            result = subprocess.run(['/entrypoint.sh', 'cat', str(target)], env={**os.environ, 'MAGICFLOW_FIX_OWNERSHIP': '1'}, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, 'existing output')
            self.assertEqual(target.stat().st_uid, 1000)
            self.assertEqual(outside.stat().st_uid, 0)
        finally:
            link.unlink();outside.unlink()

    def test_unwritable_mount_fails_clearly_and_does_not_start_command(self):
        target = Path('/data/input');before=target.stat().st_mode & 0o777
        target.chmod(0o500)
        try:
            result = subprocess.run(['/entrypoint.sh', 'echo', 'SHOULD_NOT_START'], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('SHOULD_NOT_START', result.stdout)
            self.assertIn('not writable', result.stderr)
        finally:
            target.chmod(before)

    def test_nonroot_entrypoint_and_readonly_source(self):
        result = subprocess.run(['setpriv', '--reuid=appuser', '--regid=appuser', '--init-groups', '--', '/entrypoint.sh', 'id', '-u'], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), '1000')
        result = subprocess.run(['setpriv', '--reuid=appuser', '--regid=appuser', '--init-groups', '--', 'test', '!', '-w', '/app/main.py'])
        self.assertEqual(result.returncode, 0)
