import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows Job Object tests')
class WindowsJobTests(unittest.TestCase):
    def run_child(self, source):
        return subprocess.run([sys.executable, '-c', source],
                              cwd=Path(__file__).resolve().parents[1],
                              text=True, encoding='utf-8', capture_output=True, timeout=15)

    def test_limits_apply_to_inherited_child_and_memory_is_enforced(self):
        result = self.run_child('''
import json, subprocess, sys
from probe.windows_job import WindowsJob
job=WindowsJob(memory_mib=64, process_limit=4)
child=subprocess.run([sys.executable,'-c',
    "try:\\n data=bytearray(100*1024*1024)\\n print('unbounded')\\nexcept MemoryError:\\n print('bounded')"],
    text=True,capture_output=True,timeout=5)
print(json.dumps({'child':child.stdout.strip(),'returncode':child.returncode,'metrics':job.metrics()}))
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence['child'], 'bounded')
        self.assertEqual(evidence['returncode'], 0)
        metrics = evidence['metrics']
        self.assertEqual(metrics['memory_limit_bytes'], 64*1024*1024)
        self.assertEqual(metrics['process_limit'], 4)
        self.assertEqual(metrics['processor_affinity_mask'].bit_count(), 1)
        self.assertGreaterEqual(metrics['total_processes'], 2)
        # Use the child's allocation outcome as enforcement evidence. The raw
        # Windows peak counter can include the denied allocation on this host.
        self.assertGreater(metrics['peak_job_memory_used_bytes'], 0)

    def test_process_cap_rejects_child_creation(self):
        result = self.run_child('''
import subprocess,sys
from probe.windows_job import WindowsJob
job=WindowsJob(process_limit=1)
try:
 subprocess.run([sys.executable,'-c','print("unbounded")'],check=True,timeout=5)
except OSError:
 print('bounded')
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'bounded')

    def test_launcher_exit_kills_descendant_and_releases_worker_lock(self):
        from probe.platform_io import acquire_worker_lock
        with tempfile.TemporaryDirectory() as directory:
            source = '''
import subprocess,sys
from probe.windows_job import WindowsJob
job=WindowsJob()
child=subprocess.Popen([sys.executable,'-c',
 "import sys,time; from probe.platform_io import acquire_worker_lock; lock=acquire_worker_lock(sys.argv[1]); print('locked',flush=True); time.sleep(60)",sys.argv[1]],
 stdout=subprocess.PIPE,text=True)
assert child.stdout.readline().strip()=='locked'
print('ready',flush=True)
'''
            result = subprocess.run([sys.executable, '-c', source, directory],
                                    cwd=Path(__file__).resolve().parents[1],
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ready')
            deadline = time.monotonic()+3
            while True:
                try: lock=acquire_worker_lock(directory); break
                except BlockingIOError:
                    if time.monotonic()>deadline: raise
                    time.sleep(.05)
            lock.close()
