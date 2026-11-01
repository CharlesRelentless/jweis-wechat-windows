import json
from pathlib import Path
import tempfile
import unittest
from probe.platform_io import is_private
from probe.runtime_health import assess, transition, save_report, read_json


class RuntimeHealthTests(unittest.TestCase):
    def base(self, **changes):
        value={'gateway_health':'PASS','poll_health':'PASS','subscription_health':'PASS',
               'account_status':'ready','poll_age_seconds':5,'active_subscriptions':{'weixin.message':1},
               'anonymous_mcp_status':401,'public_health':'PASS'}
        value.update(changes); return value

    def report(self, status=None, endpoint=None, **changes):
        options={'expected_origin':'https://fixture.example','lease_expires':5000,'now':1000}
        options.update(changes)
        return assess(status or self.base(),endpoint or {'origin':'https://fixture.example','temporary':True},**options)

    def test_live_temporary_service_is_healthy_but_restart_stays_blocked(self):
        report=self.report()
        self.assertEqual(report['service_status'],'PASS')
        self.assertEqual(report['restart_recovery'],'BLOCKED')

    def test_old_ready_record_and_missing_subscription_are_failures(self):
        result=self.report(self.base(poll_health='FAIL',subscription_health='FAIL',active_subscriptions={}))
        self.assertEqual(result['service_status'],'FAIL')
        self.assertEqual(result['subscription_count'],0)

    def test_changed_endpoint_fails_even_when_new_gateway_is_healthy(self):
        result=self.report(endpoint={'origin':'https://changed.example','temporary':True})
        self.assertIn('endpoint_changed_or_invalid',result['reasons'])

    def test_restart_ready_requires_same_origin_and_persistence(self):
        self.assertEqual(self.report(endpoint={'origin':'https://fixture.example','temporary':False})['restart_recovery'],'BLOCKED')
        self.assertEqual(self.report(endpoint={'origin':'https://fixture.example','temporary':False,'persistent_oauth':True})['restart_recovery'],'READY')

    def test_missing_authentication_and_public_gateway_are_detected(self):
        report=self.report(self.base(anonymous_mcp_status=200,public_health='FAIL'))
        self.assertEqual(set(report['reasons']),{'anonymous_mcp_not_denied','public_ingress_unavailable'})

    def test_unreachable_socket_is_not_misclassified_as_anonymous_access(self):
        report=self.report(self.base(gateway_health='FAIL',anonymous_mcp_status=0))
        self.assertEqual(report['reasons'],['gateway_unavailable'])
        self.assertFalse(transition(report,{})['notify'])

    def test_lease_warning_is_distinct_from_dead_subscription(self):
        report=self.report(lease_expires=1100)
        self.assertEqual(report['service_status'],'WARN')

    def test_two_failed_samples_alert_once_then_one_recovery(self):
        bad=self.report(self.base(gateway_health='FAIL'))
        first=transition(bad,{})
        self.assertFalse(first['notify'])
        second=transition(bad,first)
        self.assertTrue(second['notify'])
        third=transition(bad,second)
        self.assertFalse(third['notify'])
        recovered=transition(self.report(),third)
        self.assertTrue(recovered['notify'])
        self.assertEqual(recovered['notification_kind'],'recovered')
        self.assertFalse(transition(self.report(),recovered)['notify'])

    def test_address_change_is_urgent_without_waiting_for_second_sample(self):
        report=self.report(endpoint={'origin':'https://changed.example'})
        self.assertTrue(transition(report,{})['notify'])

    def test_status_and_transition_files_are_private_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'monitor'
            report=transition(self.report(self.base(gateway_health='FAIL')),{},failure_threshold=1)
            save_report(directory,report)
            self.assertTrue(is_private(directory/'status.json'))
            self.assertTrue(is_private(directory/'transitions.jsonl'))
            self.assertEqual(read_json(directory/'status.json')['service_status'],'FAIL')
            encoded=(directory/'transitions.jsonl').read_text('utf-8')
            self.assertNotIn('fixture.example',encoded)
            save_report(directory,dict(report,notification_delivery='desktop_popup_requested'),record_transition=False)
            self.assertEqual(len((directory/'transitions.jsonl').read_text('utf-8').splitlines()),1)
            (directory/'transitions.jsonl').write_bytes(b'x'*(256*1024+1))
            save_report(directory,report)
            self.assertLess((directory/'transitions.jsonl').stat().st_size,2048)
            self.assertTrue((directory/'transitions.previous.jsonl').exists())


if __name__=='__main__': unittest.main()
