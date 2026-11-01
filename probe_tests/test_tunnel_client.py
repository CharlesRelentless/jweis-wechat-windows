import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from probe.client_integrity import verify_bundle
from probe.platform_io import private_dir
from probe.probe import Probe

ROOT=Path(__file__).resolve().parents[1]


class ConnectionPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('offline_check',ROOT/'scripts/tunnel-offline-check.py')
        cls.check=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.check)

    def test_partial_publication_waits_for_complete_loopback_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'connection.json'
            path.write_text('',encoding='utf-8')
            expected={'mcp_url':'http://127.0.0.1:12345/mcp','health_url':'http://127.0.0.1:12346'}
            def publish():
                time.sleep(.03)
                path.write_text('{"mcp_url":',encoding='utf-8')
                time.sleep(.03)
                path.write_text(json.dumps(expected),encoding='utf-8')
            writer=threading.Thread(target=publish)
            writer.start()
            try:
                self.assertEqual(self.check.wait_connection_info(path,mock.Mock(poll=lambda:None),timeout=2),expected)
            finally: writer.join(timeout=2)

    def test_permanently_incomplete_publication_fails_with_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'connection.json'
            path.write_text('{',encoding='utf-8')
            with self.assertRaisesRegex(TimeoutError,'publication timeout'):
                self.check.wait_connection_info(path,mock.Mock(poll=lambda:None),timeout=.05)

    def test_valid_non_loopback_publication_is_rejected_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'connection.json'
            path.write_text(json.dumps({'mcp_url':'https://example.com/mcp','health_url':'http://127.0.0.1:12346'}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'loopback'):
                self.check.wait_connection_info(path,mock.Mock(poll=lambda:None),timeout=2)

    def test_fixture_does_not_use_owner_codex_home_or_native_control_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root=private_dir(Path(directory)/'fixture')
            env=self.check.fixture_environment(root,{'CODEX_HOME':'owner-home',
                'CODEX_APP_TOOLS_PIPE_PATH':'owner-context','OPENAI_API_KEY':'synthetic-key'})
            self.assertEqual(env['CODEX_HOME'],str(root/'codex-home'))
            self.assertNotIn('CODEX_APP_TOOLS_PIPE_PATH',env)
            self.assertNotIn('OPENAI_API_KEY',env)
            self.assertEqual(env['TUNNEL_CLIENT_CODEX_APP_SERVER_CMD'],sys.executable)


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
            'release':'v0.0.19',
            'sha256':'1d0ecc3a54f2a51e27a635ac155f40fbb95df58c52cdeddf6c75532cc0bbeb25',
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

    def test_mixed_release_and_archive_records_fail_before_binary_use(self):
        for changes in ({'release':'v0.0.15'},
                        {'sha256':'3b53133a1e24d43f63088d843860cb1701a4c3ed6390de2e19f69089e43bddc1'}):
            with self.subTest(changes=changes):
                record=dict(self.record,**changes)
                (self.binary/'release-verification.json').write_text(json.dumps(record),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'release provenance'):
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
    def test_official_client_reads_synthetic_authorized_local_file(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/tunnel-offline-check.py'),'--local-files'],
                              text=True,encoding='utf-8',capture_output=True,timeout=35)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['tool_count'],16)
        self.assertTrue(report['authorized_local_read'])

    def test_official_client_accepts_confirmed_env_without_another_key_file(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/tunnel-offline-check.py'),'--confirmed-env'],
                              text=True,encoding='utf-8',capture_output=True,timeout=35)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['doctor_result'],'ok')
        self.assertTrue(report['confirmed_env_mode'])
        self.assertTrue(report['second_key_file_absent'])
        self.assertEqual(report['health'],{'/healthz':200,'/readyz':200})
        self.assertTrue(report['actual_status_tool_call'])

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
