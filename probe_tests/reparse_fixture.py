"""Replace a fixture file with a redirect, without Windows symlink privileges."""
import contextlib
import os
import subprocess
import sys


@contextlib.contextmanager
def redirected_file(path, target):
    path.unlink()
    if sys.platform == 'win32':
        # A junction is also a reparse point. Use a test-owned directory only.
        subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(path), str(target.parent)],
                       check=True, capture_output=True, timeout=10)
    else:
        path.symlink_to(target)
    try:
        yield
    finally:
        if sys.platform == 'win32': os.rmdir(path)
        else: path.unlink()
