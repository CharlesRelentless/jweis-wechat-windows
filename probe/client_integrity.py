"""Detect accidental changes to the pinned official Windows client bundle."""
import hashlib
import json
from pathlib import Path
from .platform_io import safe_path


def verify_bundle(root):
    directory=safe_path(Path(root)/'bin')
    record=json.loads((directory/'release-verification.json').read_text(encoding='utf-8'))
    if record.get('release')!='v0.0.15' or record.get('sha256')!='3b53133a1e24d43f63088d843860cb1701a4c3ed6390de2e19f69089e43bddc1':
        raise ValueError('Unexpected official release provenance')
    for name in ('tunnel-client.exe','cloudflared.exe'):
        expected=record.get('files',{}).get(name)
        with safe_path(directory/name).open('rb') as source:
            digest=hashlib.sha256()
            for chunk in iter(lambda: source.read(64*1024),b''):
                digest.update(chunk)
            actual=digest.hexdigest()
        if not expected or actual!=expected:
            raise ValueError('Official bundle file integrity mismatch')
    return directory/'tunnel-client.exe'
