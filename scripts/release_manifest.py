"""Build-time artifact inventory; no clock or runtime network access."""
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

root = Path('/app')
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
files = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*'))
         if p.is_file() and '__pycache__' not in p.parts
         and p.name not in ('release-manifest.json', 'python-sbom.json')}
files['/entrypoint.sh'] = digest(Path('/entrypoint.sh'))
source_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
from capabilities import detect
manifest = {
    'schema_version': 1,
    'version': os.environ['MAGICFLOW_VERSION'],
    'revision': os.environ['MAGICFLOW_REVISION'],
    'source_sha256': source_hash,
    'architecture': platform.machine(),
    'python': platform.python_version(),
    'files': files,
    'build_inputs': json.loads((root / 'build-inputs.json').read_text()),
    'system_packages': subprocess.check_output(['dpkg-query', '-W', '-f=${Package}=${Version}\n'], text=True).splitlines(),
    'capabilities': detect(),
}
(root / 'release-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
(root / 'python-sbom.json').write_text(subprocess.check_output(['python3', '-m', 'pip', 'inspect'], text=True))
