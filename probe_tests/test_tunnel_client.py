import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from probe.client_integrity import verify_bundle
from probe.platform_io import private_dir
from probe.probe import Probe

ROOT=Path(__file__).resolve().parents[1]


class ClientIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.binary=self.root/'bin'
        self.binary.mkdir()
        files={}
        for name in ('tunnel-client.exe','cloudflared.exe'):
            data=(name.encode()+b'\x00synthetic-binary')*4096
            (self.binary/name).write_bytes(data)
            files[name]=hashlib.sha256(data).hexdigest()
        self.record={
            'release':'v0.0.15',
            'sha256':'3b53133a1e24d43f63088d843860cb1701a4c3ed6390de2e19f69089e43bddc1',
            'files':files}
        (self.binary/'release-verification.json').write_text(json.dumps(self.record),encoding='utf-8')

    def test_pinned_bundle_verifies_without_python311_file_digest(self):
        # Python 3.10 has no callable hashlib.file_digest. Exercise the public
        # bundle check with that API unavailable, including multi-block input.
        with mock.patch.object(hashlib,'file_digest',None,create=True):
            self.assertEqual(verify_bundle(self.root),self.binary/'tunnel-client.exe')

    def test_modified_companion_is_rejected(self):
        with (self.binary/'cloudflared.exe').open('ab') as target:
            target.write(b'changed')
        with self.assertRaisesRegex(ValueError,'integrity mismatch'):
            verify_bundle(self.root)


class SafeStatusTests(unittest.TestCase):
    def test_unicode_read_only_status_closes_database_and_excludes_private_content(self):
        with tempfile.TemporaryDirectory() as directory:
            state=private_dir(Path(directory)/'状态 含空格')
            probe=Probe(state); probe.close()
            result=subprocess.run([sys.executable,str(ROOT/'scripts/safe-status.py'),'--state-dir',str(state)],
                                  text=True,encoding='utf-8',capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(result.stdout)
            self.assertEqual(report['account_status'],'not_bound')
            self.assertEqual(report['active_subscriptions'],{})
            self.assertNotIn('owner',report)
            self.assertNotIn('message',report)


@unittest.skipUnless(sys.platform=='win32' and (ROOT/'bin/tunnel-client.exe').is_file(),
                     'Optional verified official Windows client is not installed')
class OfficialTunnelTests(unittest.TestCase):
    def test_official_client_discovers_events_and_forwards_actual_tool_call(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/tunnel-offline-check.py')],
                              text=True,encoding='utf-8',capture_output=True,timeout=35)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['doctor_result'],'ok')
        self.assertEqual(report['health'],{'/healthz':200,'/readyz':200})
        self.assertEqual(report['protocol_versions'],['2026-07-28'])
        self.assertTrue(report['events_capability'])
        self.assertTrue(report['message_event'])
        self.assertTrue(report['actual_status_tool_call'])
        self.assertEqual(report['tool_count'],15)
        self.assertTrue(report['private_database'])
