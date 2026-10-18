import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from probe.platform_io import private_dir, open_private
from probe.probe import Probe

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('safe_status_reader',ROOT/'scripts/safe-status.py')
status=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)
HEALTH={'/healthz':200,'/readyz':200}


class LiveStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime=private_dir(Path(self.temp.name)/'runtime')
        self.state=private_dir(self.runtime/'state')
        self.endpoint=private_dir(self.runtime/'server-url')/'endpoint.json'
        self.probe=Probe(self.state)
        self.addCleanup(self.probe.close)
        self.now=1000
        self.probe.db.execute(
            'INSERT INTO weixin_accounts(id,owner,token,base_url,last_poll) VALUES(?,?,?,?,?)',
            ('fixture-account','fixture-owner','fixture-token','https://example.com',self.now-10))
        self.add_subscription('fixture-sub',self.now+100)
        self.write(self.endpoint,json.dumps({'local_port':4242,'origin':'https://private-origin.invalid'}))

    def write(self,path,value):
        with os.fdopen(open_private(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC),'w',encoding='utf-8') as target:
            target.write(value)

    def add_subscription(self,identifier,expires):
        self.probe.db.execute(
            'INSERT INTO subscriptions(id,url,secret,rotate_until,expires,verified_at,name) VALUES(?,?,?,?,?,?,?)',
            (identifier,'https://private-callback.invalid','fixture-secret',0,expires,self.now,'weixin.message'))
        self.probe.db.commit()

    def report(self,probe=None):
        return status.build_status(self.state,now=self.now,health_probe=probe or (lambda port:HEALTH))

    def test_current_gateway_port_wins_over_healthy_legacy_port(self):
        self.write(self.state/'service-health.url','http://127.0.0.1:1111')
        health=mock.Mock(return_value=HEALTH)
        result=self.report(health)
        health.assert_called_once_with(4242)
        self.assertEqual(result['health_source'],'server_url_endpoint')
        self.assertEqual(result['runtime_status'],'PASS')

    def test_invalid_current_endpoint_never_falls_back(self):
        self.write(self.state/'service-health.url','http://127.0.0.1:1111')
        for value in ['{','null','[]',json.dumps({'local_port':True}),json.dumps({'local_port':0}),
                      json.dumps({'local_port':65536}),json.dumps({'local_port':'4242'})]:
            with self.subTest(value=value):
                self.write(self.endpoint,value)
                health=mock.Mock(return_value=HEALTH)
                result=self.report(health)
                health.assert_not_called()
                self.assertEqual(result['health_source'],'invalid_server_url_endpoint')
                self.assertEqual(result['runtime_status'],'FAIL')

    def test_unreadable_current_endpoint_does_not_probe_old_port(self):
        health=mock.Mock(return_value=HEALTH)
        with mock.patch.object(status,'read_private_text',side_effect=PermissionError()):
            result=self.report(health)
        health.assert_not_called()
        self.assertEqual(result['health_source'],'invalid_server_url_endpoint')

    def test_legacy_tunnel_health_still_works_when_endpoint_absent(self):
        self.endpoint.unlink()
        self.write(self.state/'service-health.url','http://127.0.0.1:1111')
        health=mock.Mock(return_value=HEALTH)
        self.assertEqual(self.report(health)['runtime_status'],'PASS')
        health.assert_called_once_with(1111)

    def test_non_loopback_or_credential_bearing_legacy_urls_are_rejected(self):
        self.endpoint.unlink()
        for url in ['https://127.0.0.1:1111','http://example.com:1111',
                    'http://fixture:fixture@127.0.0.1:1111','http://127.0.0.1:1111?fixture=1']:
            with self.subTest(url=url):
                self.write(self.state/'service-health.url',url)
                health=mock.Mock(return_value=HEALTH)
                result=self.report(health)
                health.assert_not_called()
                self.assertEqual(result['runtime_status'],'FAIL')

    def test_stale_missing_or_future_poll_cannot_report_live_ready(self):
        for last_poll in [self.now-600,None,self.now+60]:
            with self.subTest(last_poll=last_poll):
                self.probe.db.execute('UPDATE weixin_accounts SET last_poll=?',(last_poll,))
                self.probe.db.commit()
                result=self.report()
                self.assertEqual(result['account_status'],'ready')
                self.assertEqual(result['poll_health'],'FAIL')
                self.assertEqual(result['runtime_status'],'FAIL')

    def test_expired_or_duplicate_message_lease_is_not_healthy(self):
        self.probe.db.execute('UPDATE subscriptions SET expires=?',(self.now-1,))
        self.probe.db.commit()
        self.assertEqual(self.report()['subscription_health'],'FAIL')
        self.add_subscription('fixture-sub2',self.now+100)
        self.add_subscription('fixture-sub3',self.now+100)
        result=self.report()
        self.assertEqual(result['active_subscriptions']['weixin.message'],2)
        self.assertEqual(result['runtime_status'],'FAIL')

    def test_bound_but_unreachable_gateway_is_failure(self):
        health=mock.Mock(side_effect=OSError('fixture network failure'))
        result=self.report(health)
        self.assertEqual(result['health'],'unavailable')
        self.assertEqual(result['gateway_health'],'FAIL')
        self.assertEqual(result['runtime_status'],'FAIL')

    def test_unbound_setup_is_warning_and_not_connected(self):
        self.probe.db.execute('DELETE FROM weixin_accounts')
        self.probe.db.execute('DELETE FROM subscriptions')
        self.probe.db.commit()
        result=self.report()
        self.assertEqual(result['account_status'],'not_bound')
        self.assertEqual(result['runtime_status'],'WARN')

    def test_status_is_read_only_and_excludes_identifiers_secrets_and_body(self):
        self.probe.db.execute('UPDATE weixin_accounts SET last_error=?',('fixture-private-diagnostic',))
        self.probe.db.execute(
            'INSERT INTO weixin_inbox(id,account,peer,source_id,text,created_at,kind,unsupported) VALUES(?,?,?,?,?,?,?,?)',
            ('fixture-inbound','fixture-account','fixture-owner','fixture-source','fixture-private-message',self.now,'text','[]'))
        self.probe.db.commit()
        before=self.probe.db.total_changes
        digest=hashlib.sha256((self.state/'probe.sqlite').read_bytes()).hexdigest()
        result=self.report()
        self.assertEqual(result['last_error'],'recorded_error')
        encoded=json.dumps(result)
        for private in ['fixture-account','fixture-owner','fixture-token','fixture-secret',
                        'fixture-private-message','fixture-private-diagnostic','private-origin','private-callback']:
            self.assertNotIn(private,encoded)
        self.assertEqual(self.probe.db.total_changes,before)
        self.assertEqual(hashlib.sha256((self.state/'probe.sqlite').read_bytes()).hexdigest(),digest)


if __name__=='__main__': unittest.main()
