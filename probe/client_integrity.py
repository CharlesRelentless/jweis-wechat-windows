"""Detect accidental changes to the pinned official Windows client bundle."""
import hashlib
import json
from pathlib import Path
from .platform_io import safe_path


def verify_bundle(root):
    directory=safe_path(Path(root)/'bin')
    record=json.loads((directory/'release-verification.json').read_text(encoding='utf-8'))
    if record.get('release')!='v0.0.19' or record.get('sha256')!='1d0ecc3a54f2a51e27a635ac155f40fbb95df58c52cdeddf6c75532cc0bbeb25':
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
