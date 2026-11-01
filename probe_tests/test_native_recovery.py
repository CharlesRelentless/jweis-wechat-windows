from pathlib import Path
import tempfile
import unittest
import threading
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from probe.platform_io import private_dir, private_file, is_private
from probe import native_recovery as recovery

class NativeRecoveryTests(unittest.TestCase):
    def test_failure_budget_resets_only_after_five_continuously_healthy_minutes(self):
        guard=recovery.ReadinessRecovery(0)
        guard.observe(10,True)
        self.assertFalse(guard.stable(309))
        guard.observe(309,False)
        guard.observe(310,True)
        self.assertFalse(guard.stable(609))
        self.assertTrue(guard.stable(610))

    def test_control_plane_fault_is_visible_when_both_legacy_probes_are_200(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=json.dumps({'components':{'control-plane':{'status':'degraded',
                    'state':'backoff','details':{'consecutive_failures':2,
                    'failure_category':'network_error'}}}}).encode() if '?' in self.path else b'ready'
                self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                path=private_dir(Path(temp)/'state')/'service-health.url'
                path.write_text('http://127.0.0.1:'+str(server.server_port),encoding='utf-8');private_file(path)
                sample=recovery.sample_health(path)
                self.assertTrue(sample['ready'])
                self.assertFalse(sample['healthy'])
                self.assertEqual(sample['reason'],'control_plane_failures')
        finally:server.shutdown();server.server_close();worker.join(timeout=2)

    def test_long_poll_backpressure_and_historical_error_do_not_trigger_recovery(self):
        for state,details in [('polling',{'current_poll_age_seconds':34,'deadline_seconds':35,'consecutive_failures':0}),
                              ('backpressured',{'consecutive_failures':4,'last_success_age_seconds':5000}),
                              ('idle',{'consecutive_failures':0,'last_success_age_seconds':1,'last_error_unix':100})]:
            with self.subTest(state=state):
                self.assertTrue(recovery.assess_control_plane({'state':state,'details':details})[0])
        self.assertFalse(recovery.assess_control_plane({'state':'polling','details':{
            'current_poll_age_seconds':121,'deadline_seconds':35,'consecutive_failures':0}})[0])
        self.assertTrue(recovery.assess_control_plane({'state':'polling','details':{
            'current_poll_age_seconds':180,'deadline_seconds':600,'consecutive_failures':0}})[0])

    def test_diagnostic_projection_excludes_untrusted_strings_payloads_and_nonfinite_values(self):
        raw={'control-plane':{'state':'backoff','reason_code':'secret-canary','details':{
            'consecutive_failures':2,'failure_category':'network_error','text':'secret-canary',
            'deadline_seconds':float('nan'),'request_id':'secret-canary'}},
             'mcp':{'state':'secret-canary','tools':['secret-canary']}}
        projected=recovery.project_components(raw,now=100)
        text=json.dumps(projected,allow_nan=False)
        self.assertNotIn('secret-canary',text)
        self.assertNotIn('NaN',text)
        self.assertEqual(projected['control-plane']['details']['consecutive_failures'],2)

    def test_failure_evidence_is_private_bounded_and_retains_only_rpc_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            state=private_dir(Path(temp)/'state')
            rpc=state/'rpc-diagnostics.jsonl'
            rpc.write_text(json.dumps({'utc':'2026-10-23T05:11:00Z','method':'tools/call',
                'stage':'received','arguments':'secret-canary'})+'\n',encoding='utf-8');private_file(rpc)
            sample={'ready':True,'healthy':False,'reason':'control_plane_failures',
                    'components':{'control-plane':{'state':'backoff','details':{'consecutive_failures':2}}}}
            recovery.save_health_sample(state,sample,123,event='before_restart')
            saved=state/'control-plane-diagnostics.jsonl'
            self.assertTrue(is_private(saved))
            data=saved.read_text(encoding='utf-8')
            self.assertNotIn('secret-canary',data)
            self.assertIn('tools/call',data)
            self.assertEqual(json.loads(data)['event'],'before_restart')
            with saved.open('a',encoding='utf-8') as f:f.write(' '*(256*1024))
            recovery.save_health_sample(state,sample,123,event='before_restart')
            self.assertLess(saved.stat().st_size,16384)
            self.assertTrue(saved.with_name('control-plane-diagnostics.previous.jsonl').exists())

    def test_live_but_unresponsive_client_is_recovered_only_after_grace_and_failure_window(self):
        guard=recovery.ReadinessRecovery(started=0,startup_grace=120,failure_window=180)
        self.assertFalse(guard.observe(110,False))
        self.assertFalse(guard.observe(120,False))
        self.assertFalse(guard.observe(299,False))
        self.assertTrue(guard.observe(300,False))

    def test_success_resets_failure_window_and_requires_continuous_failure(self):
        guard=recovery.ReadinessRecovery(started=0,startup_grace=120,failure_window=180)
        self.assertFalse(guard.observe(120,False))
        self.assertFalse(guard.observe(290,True))
        self.assertFalse(guard.observe(300,False))
        self.assertFalse(guard.observe(479,False))
        self.assertTrue(guard.observe(480,False))

    def test_readiness_probe_requires_real_local_200_and_rejects_remote_url(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'ready')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                path=private_dir(Path(temp)/'state')/'service-health.url'
                path.write_text('http://127.0.0.1:'+str(server.server_port),encoding='utf-8')
                from probe.platform_io import private_file
                private_file(path)
                self.assertTrue(recovery.local_readiness(path))
                path.write_text('https://example.com/health',encoding='utf-8')
                self.assertFalse(recovery.local_readiness(path))
                path.unlink()
                self.assertFalse(recovery.local_readiness(path))
        finally:server.shutdown();server.server_close();worker.join(timeout=2)

    def test_three_crashes_back_off_without_a_second_scheduler_and_stable_run_resets(self):
        with tempfile.TemporaryDirectory() as temp:
            path=private_dir(Path(temp)/'state')/'native-recovery.json'
            self.assertTrue(recovery.allowed(path,10000))
            recovery.failed(path,10000);recovery.failed(path,10060);recovery.failed(path,10120)
            self.assertFalse(recovery.allowed(path,10180))
            self.assertTrue(recovery.allowed(path,11920))
            recovery.healthy(path)
            self.assertTrue(recovery.allowed(path,10200))
    def test_long_gap_resets_the_consecutive_failure_count(self):
        with tempfile.TemporaryDirectory() as temp:
            path=private_dir(Path(temp)/'state')/'native-recovery.json'
            recovery.failed(path,10000)
            self.assertEqual(recovery.failed(path,12000)['failures'],1)
