#!/usr/bin/env python3
"""Emit an exact hash lock from a fresh CPython 3.12/Linux amd64 wheel download."""
import email
import hashlib
from pathlib import Path
import sys
import zipfile

wheels = sorted(Path(sys.argv[1]).glob('*.whl'))
if not wheels:
    raise SystemExit('No wheels found; download into a fresh directory first.')
entries = {}
for wheel in wheels:
    with zipfile.ZipFile(wheel) as archive:
        metadata = email.message_from_bytes(archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))))
    name = metadata['Name']
    if name.lower() in entries:
        raise SystemExit('Duplicate package; use a fresh wheel directory: ' + name)
    entries[name.lower()] = f'{name}=={metadata["Version"]} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}'
print('# CPython 3.12 / Linux amd64 wheels. Regenerate deliberately; see README release process.')
print('\n'.join(entries[name] for name in sorted(entries)))
