"""Exercise the shipped CLI against synthetic SQLite state and a real local socket."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from probe.platform_io import open_private,private_dir
from probe.probe import Probe

ROOT=Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        failures=getattr(self.server,'control_plane_failures',0)
        body=json.dumps({'components':{'control-plane':{
            'state':'backoff' if failures else 'polling','status':'degraded' if failures else 'ok',
            'details':{'consecutive_failures':failures,'current_poll_age_seconds':0,'deadline_seconds':35}}}}).encode() if self.path=='/health?details=true' else b''
        self.send_response(401 if self.path=='/mcp' else 200)
        self.send_header('Content-Length',str(len(body))); self.end_headers();self.wfile.write(body)
    def log_message(self,*args): pass


class RuntimeCheckIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        scripts=self.root/'scripts'; scripts.mkdir()
        (scripts/'safe-status.py').write_bytes((ROOT/'scripts/safe-status.py').read_bytes())
        self.state=private_dir(self.root/'runtime/state')
        self.probe=Probe(self.state); self.addCleanup(self.probe.close)
        now=time.time()
        self.probe.db.execute('INSERT INTO weixin_accounts(id,owner,token,base_url,last_poll) VALUES(?,?,?,?,?)',('fixture','fixture-owner','fixture-token','https://example.com',now))
        self.probe.db.execute('INSERT INTO subscriptions(id,url,secret,rotate_until,expires,verified_at,name) VALUES(?,?,?,?,?,?,?)',('fixture-sub','https://callback.example','fixture-secret',0,now+3600,now,'weixin.message'))
        self.probe.db.commit()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.stop_server)
        self.write(self.root/'runtime/server-url/endpoint.json',{'origin':'https://fixture.example','local_port':self.server.server_port,'temporary':True})
        self.write(self.root/'runtime/monitor/policy.json',{'version':1,'expected_origin':'https://fixture.example'})

    def write(self,path,value):
        private_dir(path.parent)
        with os.fdopen(open_private(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY),'w',encoding='utf-8') as target: json.dump(value,target)

    def stop_server(self):
        if self.thread.is_alive(): self.server.shutdown(); self.thread.join(timeout=3)
        self.server.server_close()

    def run_check(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/check-runtime.py'),'--root',str(self.root)],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertTrue(result.stdout.strip(),result.stderr)
        report=json.loads(result.stdout)
        for secret in ['fixture-token','fixture-owner','fixture-secret','https://fixture.example']:
            self.assertNotIn(secret,result.stdout)
        return result.returncode,report

    def test_real_gateway_failure_notifies_once_without_restart_or_message_changes(self):
        code,healthy=self.run_check()
        self.assertEqual((code,healthy['service_status'],healthy['restart_recovery']),(0,'PASS','BLOCKED'))
        self.stop_server()
        code,first=self.run_check(); self.assertEqual(code,1); self.assertFalse(first['notify'])
        code,second=self.run_check(); self.assertEqual(code,1); self.assertTrue(second['notify'])
        code,third=self.run_check(); self.assertFalse(third['notify'])
        self.assertEqual(self.probe.db.execute('select count(*) from weixin_outbox').fetchone()[0],0)
        self.assertEqual(self.probe.db.execute('select count(*) from subscriptions').fetchone()[0],1)

    def test_cli_fails_closed_when_current_endpoint_changes(self):
        self.run_check()
        self.write(self.root/'runtime/server-url/endpoint.json',{'origin':'https://changed.example','local_port':self.server.server_port,'temporary':True})
        code,report=self.run_check()
        self.assertEqual(code,1)
        self.assertTrue(report['notify'])
        self.assertIn('endpoint_changed_or_invalid',report['reasons'])

    def configure_official_transport(self):
        tunnel='tunnel_'+'3'*32
        self.write(self.root/'runtime/transport.json',{'kind':'official_tunnel','tunnel_id':tunnel})
        self.write(self.root/'runtime/monitor/policy.json',{'transport':'official_tunnel','expected_tunnel_id':tunnel})
        self.write(self.root/'runtime/official-tunnel-activation.json',{
            'tunnel_id':tunnel,'cost_status':'no_charge_observed','continue_until_charge':True,
            'online_start_authorized':True})
        for path,value in [(self.root/'runtime/profile.yaml','control_plane:\n  tunnel_id: '+tunnel+'\n  api_key: "env:JWEIS_TUNNEL_API_KEY"\n'),
                           (self.root/'runtime/state/service-health.url','http://127.0.0.1:'+str(self.server.server_port))]:
            with os.fdopen(open_private(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY),'w',encoding='utf-8') as target:target.write(value)

    def test_official_transport_uses_pinned_identity_and_current_health(self):
        self.configure_official_transport()
        code,report=self.run_check()
        self.assertEqual((code,report['service_status'],report['restart_recovery']),(0,'PASS','READY'))
        self.write(self.root/'runtime/transport.json',{'kind':'official_tunnel','tunnel_id':'tunnel_'+'4'*32})
        code,report=self.run_check()
        self.assertEqual(code,1)
        self.assertIn('endpoint_changed_or_invalid',report['reasons'])

    def test_ready_and_fresh_poll_do_not_hide_control_plane_failure(self):
        self.configure_official_transport()
        self.server.control_plane_failures=3
        code,report=self.run_check()
        self.assertEqual((code,report['service_status']),(1,'FAIL'))
        self.assertIn('control_plane_unhealthy',report['reasons'])
        self.assertEqual(self.probe.db.execute('select count(*) from weixin_outbox').fetchone()[0],0)
        self.assertEqual(self.probe.db.execute('select count(*) from subscriptions').fetchone()[0],1)

    def test_official_initialization_pins_current_tunnel_and_rejects_silent_migration(self):
        self.configure_official_transport()
        policy=self.root/'runtime/monitor/policy.json';policy.unlink()
        command=[sys.executable,str(ROOT/'scripts/check-runtime.py'),'--root',str(self.root),'--initialize']
        initialized=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(initialized.returncode,0,initialized.stderr)
        self.assertEqual(json.loads(policy.read_text())['expected_tunnel_id'],'tunnel_'+'3'*32)
        self.write(policy,{'transport':'official_tunnel','expected_tunnel_id':'tunnel_'+'4'*32})
        original=policy.read_bytes()
        rejected=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertNotEqual(rejected.returncode,0)
        self.assertEqual(policy.read_bytes(),original)


if __name__=='__main__': unittest.main()
