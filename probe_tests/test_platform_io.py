import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from probe.platform_io import WINDOWS, acquire_worker_lock, is_private, open_private, private_dir, safe_path

ROOT = Path(__file__).resolve().parents[1]


class PlatformIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = private_dir(Path(self.temp.name) / "private")

    def test_utf8_space_path_binary_roundtrip_and_private_access(self):
        directory = private_dir(self.root / "中文 path")
        path = directory / "message.txt"
        payload = "微信：你好，jweis\n".encode("utf-8")
        with os.fdopen(open_private(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY), "wb") as f:
            f.write(payload)
        with os.fdopen(open_private(path, os.O_RDONLY), "rb") as f:
            self.assertEqual(f.read(), payload)
        self.assertTrue(is_private(directory))
        self.assertTrue(is_private(path))
        with self.assertRaises(FileExistsError): open_private(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    def test_worker_exclusion_across_processes_and_release(self):
        command = [sys.executable, "-c",
                   "import sys; from probe.platform_io import acquire_worker_lock; "
                   "h=acquire_worker_lock(sys.argv[1]); print('acquired'); h.close()", str(self.root)]
        with acquire_worker_lock(self.root):
            denied = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(denied.returncode, 0)
            self.assertNotIn("acquired", denied.stdout)
        allowed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout.strip(), "acquired")

    @unittest.skipUnless(WINDOWS, "Windows junction and alternate stream test")
    def test_windows_directory_junction_is_rejected_before_file_write(self):
        target = private_dir(Path(self.temp.name) / "outside")
        junction = self.root / "redirect"
        # Creation only: cleanup removes the junction itself with os.rmdir.
        created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                                 capture_output=True, timeout=10)
        self.assertEqual(created.returncode, 0, "Could not create synthetic test junction")
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        with self.assertRaises(OSError): open_private(junction / "new.txt", os.O_CREAT | os.O_WRONLY)
        with self.assertRaises(OSError): private_dir(junction)
        self.assertFalse((target / "new.txt").exists())
        with self.assertRaises(OSError): open_private(self.root / "file.txt:secret", os.O_CREAT | os.O_WRONLY)

    def test_existing_directory_cannot_be_opened_as_file(self):
        with self.assertRaises(OSError): open_private(self.root, os.O_WRONLY)


if __name__ == "__main__": unittest.main()
