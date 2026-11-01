import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest import mock

from probe.platform_io import private_dir

ROOT=Path(__file__).resolve().parents[1]


class ConsolelessClientTests(unittest.TestCase):
    def test_real_child_stops_after_loopback_control_plane_fault_and_evidence_is_retained(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=json.dumps({'components':{'control-plane':{'state':'backoff','status':'degraded',
                    'details':{'consecutive_failures':3,'failure_category':'timeout'}}}}).encode() if '?' in self.path else b'ready'
                self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        spec=importlib.util.spec_from_file_location('run_windows',ROOT/'scripts/run-windows.py')
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);runtime=private_dir(root/'runtime');state=private_dir(runtime/'state')
                profile=runtime/'profile.yaml';profile.write_text('synthetic',encoding='utf-8')
                endpoint=state/'service-health.url';endpoint.write_text('http://127.0.0.1:'+str(server.server_port))
                from probe.platform_io import private_file
                private_file(endpoint)
                child=launcher.start_client([sys.executable,'-c','import time; time.sleep(30)'],
                    cwd=ROOT,env=os.environ.copy(),log_path=runtime/'synthetic.log')
                guard=launcher.native_recovery.ReadinessRecovery(0,startup_grace=0,failure_window=0)
                try:
                    with mock.patch.object(launcher,'ROOT',root),mock.patch.object(launcher,'is_private',return_value=True), \
                         mock.patch.object(launcher,'official_environment',return_value={}), \
                         mock.patch.object(launcher,'activation_policy',return_value={}), \
                         mock.patch.object(launcher,'verify_bundle',return_value=root/'client.exe'), \
                         mock.patch.object(launcher,'WindowsJob') as job, \
                         mock.patch.object(launcher,'start_client',return_value=child), \
                         mock.patch.object(launcher.native_recovery,'ReadinessRecovery',return_value=guard), \
                         mock.patch.object(launcher.time,'monotonic',side_effect=[0,20]), \
                         mock.patch.object(sys,'argv',['run-windows.py']):
                        job.return_value.metrics.return_value={}
                        self.assertEqual(launcher.main(),1)
                    self.assertIsNotNone(child.poll())
                    records=[json.loads(x) for x in (state/'control-plane-diagnostics.jsonl').read_text().splitlines()]
                    self.assertEqual(records[-1]['event'],'before_restart')
                    self.assertTrue(records[-1]['ready'])
                    self.assertFalse(records[-1]['healthy'])
                    self.assertEqual(records[-1]['components']['control-plane']['details']['consecutive_failures'],3)
                    self.assertEqual(launcher.native_recovery.read(state/'native-recovery.json')['failures'],1)
                finally:
                    if child.poll() is None:child.terminate();child.wait(timeout=5)
        finally:server.shutdown();server.server_close();worker.join(timeout=2)

    def test_alive_client_with_expired_readiness_window_is_stopped_and_counts_as_failure(self):
        spec=importlib.util.spec_from_file_location('run_windows',ROOT/'scripts/run-windows.py')
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runtime=private_dir(root/'runtime')
            profile=runtime/'profile.yaml';profile.write_text('synthetic',encoding='utf-8')
            state=private_dir(runtime/'state')
            child=mock.Mock(pid=123)
            child.wait.side_effect=[subprocess.TimeoutExpired('synthetic-client',5),0]
            guard=launcher.native_recovery.ReadinessRecovery(0,startup_grace=0,failure_window=0)
            def terminate_after_evidence():
                self.assertIn('before_restart',(state/'control-plane-diagnostics.jsonl').read_text())
            child.terminate.side_effect=terminate_after_evidence
            with mock.patch.object(launcher,'ROOT',root),mock.patch.object(launcher,'is_private',return_value=True), \
                 mock.patch.object(launcher,'official_environment',return_value={}), \
                 mock.patch.object(launcher,'activation_policy',return_value={}), \
                 mock.patch.object(launcher,'verify_bundle',return_value=root/'client.exe'), \
                 mock.patch.object(launcher,'WindowsJob') as job, \
                 mock.patch.object(launcher,'start_client',return_value=child), \
                 mock.patch.object(launcher.native_recovery,'ReadinessRecovery',return_value=guard), \
                 mock.patch.object(launcher.native_recovery,'sample_health',return_value={
                     'ready':True,'healthy':False,'reason':'control_plane_failures','components':{}}), \
                 mock.patch.object(launcher.time,'monotonic',side_effect=[0,20]), \
                 mock.patch.object(sys,'argv',['run-windows.py']):
                job.return_value.metrics.return_value={}
                self.assertEqual(launcher.main(),1)
            child.terminate.assert_called_once()
            self.assertEqual(launcher.native_recovery.read(state/'native-recovery.json')['failures'],1)
            self.assertIn('readiness_recovery_requested',(state/'tunnel-lifecycle.jsonl').read_text())
            evidence=(state/'control-plane-diagnostics.jsonl').read_text()
            self.assertIn('before_restart',evidence)
            self.assertIn('control_plane_failures',evidence)

    def test_evidence_failure_does_not_destroy_child_or_override_owner_charge_stop(self):
        spec=importlib.util.spec_from_file_location('run_windows',ROOT/'scripts/run-windows.py')
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runtime=private_dir(root/'runtime')
            profile=runtime/'profile.yaml';profile.write_text('synthetic',encoding='utf-8')
            state=private_dir(runtime/'state');child=mock.Mock(pid=123)
            child.wait.side_effect=[subprocess.TimeoutExpired('synthetic',5),
                                    subprocess.TimeoutExpired('synthetic',5),0]
            with mock.patch.object(launcher,'ROOT',root),mock.patch.object(launcher,'is_private',return_value=True), \
                 mock.patch.object(launcher,'official_environment',return_value={}), \
                 mock.patch.object(launcher,'activation_policy',side_effect=[{}, {}, ValueError('owner-stop')]), \
                 mock.patch.object(launcher,'verify_bundle',return_value=root/'client.exe'), \
                 mock.patch.object(launcher,'WindowsJob') as job, \
                 mock.patch.object(launcher,'start_client',return_value=child), \
                 mock.patch.object(launcher.native_recovery,'sample_health',return_value={
                     'ready':True,'healthy':False,'reason':'control_plane_failures','components':{}}), \
                 mock.patch.object(launcher.native_recovery,'save_health_sample',side_effect=OSError('disk-full')), \
                 mock.patch.object(launcher.time,'monotonic',side_effect=[0,20]), \
                 mock.patch.object(sys,'argv',['run-windows.py']):
                job.return_value.metrics.return_value={}
                self.assertEqual(launcher.main(),0)
            # One terminate belongs to the owner stop, not the failed diagnostic.
            child.terminate.assert_called_once()
            self.assertFalse((state/'native-recovery.json').exists())

    def test_child_output_is_captured_even_without_host_console_handles(self):
        spec=importlib.util.spec_from_file_location('run_windows',ROOT/'scripts/run-windows.py')
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as directory:
            root=private_dir(Path(directory)/'runtime')
            child=launcher.start_client([sys.executable,'-c',
                'import sys; print("client-output"); print("client-error",file=sys.stderr)'],
                cwd=ROOT,env=os.environ.copy(),log_path=root/'client.log')
            self.assertEqual(child.wait(timeout=10),0)
            output=(root/'client.log').read_text(encoding='utf-8')
            self.assertIn('client-output',output)
            self.assertIn('client-error',output)
