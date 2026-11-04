"""Fetch snapshot packages through urllib; verify SHA256 from signed APT indexes.
APT still resolves/install-checks the packages. Bounded downloads avoid stalled
snapshot redirects in some APT HTTPS transports. No unsigned metadata is used.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys
import urllib.request
import urllib.parse

checksums = {}
for stanza in subprocess.check_output(['apt-cache', 'dumpavail'], text=True).split('\n\n'):
    fields = dict(line.split(': ', 1) for line in stanza.splitlines() if ': ' in line and not line.startswith(' '))
    if 'Filename' in fields and 'SHA256' in fields:
        checksums[Path(fields['Filename']).name] = fields['SHA256']
uris = subprocess.check_output(['apt-get', '--print-uris', '--yes', '--download-only', '--no-install-recommends', 'install', *sys.argv[1:]], text=True)
requests = [shlex.split(line) for line in uris.splitlines() if line.startswith("'")]
def fetch(request):
    url, name, size, *_ = request
    url = urllib.parse.unquote(url)
    expected = checksums[Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name]  # fail closed if the signed index lacks SHA256
    destination = Path('/var/cache/apt/archives') / name
    partial = destination.with_suffix('.partial')
    archive, _, artifact = urllib.parse.urlparse(url).path.removeprefix('/archive/').split('/', 2)
    mirror = 'https://deb.debian.org/' + archive + '/' + artifact
    candidates = [mirror, url, mirror, url]
    for attempt, candidate in enumerate(candidates):
        try:
            digest = hashlib.sha256();count = 0
            with urllib.request.urlopen(candidate, timeout=30) as response, partial.open('wb') as out:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk);count += len(chunk);out.write(chunk)
            if count != int(size) or digest.hexdigest() != expected:
                raise ValueError('Snapshot artifact verification failed: ' + name)
            partial.replace(destination)
            return
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == len(candidates) - 1:
                raise RuntimeError('Could not fetch verified snapshot package: ' + name)

with ThreadPoolExecutor(max_workers=4) as workers:
    for count, _ in enumerate(workers.map(fetch, requests), 1):
        if count % 20 == 0:print('Verified', count, 'snapshot packages', flush=True)
print('Verified', len(requests), 'snapshot packages against signed-index SHA256 values.')
