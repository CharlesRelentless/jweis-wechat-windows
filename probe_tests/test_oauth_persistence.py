import json
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from probe.platform_io import is_private
from probe.server_url import OwnerOAuth, OAuthError, CHATGPT_REDIRECT
from probe_tests import test_server_url as fixture

ORIGIN = fixture.ORIGIN


class OAuthPersistenceTests(unittest.TestCase):
    request = fixture.OAuthTests.request
    approved_code = fixture.OAuthTests.approved_code
    exchange = fixture.OAuthTests.exchange

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.directory = Path(self.temp.name) / 'oauth'
        self.oauth = OwnerOAuth(self.directory, clock=lambda: self.now, persistent=True)
        self.addCleanup(lambda: self.oauth.close())
        self.oauth.configure_origin(ORIGIN)
        self.client = self.oauth.register({'redirect_uris': [CHATGPT_REDIRECT]})['client_id']

    def restart(self, origin=ORIGIN):
        self.oauth.close()
        restored = OwnerOAuth(self.directory, clock=lambda: self.now, persistent=True)
        restored.configure_origin(origin)
        self.oauth = restored
        return restored

    def refresh_request(self, token):
        return {'grant_type': ['refresh_token'], 'client_id': [self.client],
                'resource': [ORIGIN], 'refresh_token': [token]}

    def test_same_origin_restart_recovers_existing_access_and_rotating_refresh(self):
        tokens = self.exchange(self.approved_code())
        self.now += 3601
        restarted = self.restart()
        self.assertFalse(restarted.authenticated('Bearer ' + tokens['access_token']))
        fresh = restarted.exchange(self.refresh_request(tokens['refresh_token']))
        again = self.restart()
        self.assertTrue(again.authenticated('Bearer ' + fresh['access_token']))
        with self.assertRaises(OAuthError):
            again.exchange(self.refresh_request(tokens['refresh_token']))
        self.assertNotEqual(tokens['refresh_token'], fresh['refresh_token'])

    def test_store_contains_only_token_hashes_and_has_private_permissions(self):
        tokens = self.exchange(self.approved_code())
        path = self.directory / 'authorization-state.json'
        self.assertTrue(is_private(path))
        content = path.read_text(encoding='utf-8')
        for token in tokens['access_token'], tokens['refresh_token']:
            self.assertNotIn(token, content)
        state = json.loads(content)
        self.assertEqual(set(state), {'version', 'origin', 'clients', 'tokens', 'refresh'})

    def test_changed_origin_is_rejected_without_reusing_authorization(self):
        self.exchange(self.approved_code())
        with self.assertRaisesRegex(ValueError, 'origin'):
            self.restart('https://changed.example.com')

    def test_expired_refresh_stays_expired_after_restart(self):
        tokens = self.exchange(self.approved_code())
        self.now += 86401
        restarted = self.restart()
        with self.assertRaises(OAuthError):
            restarted.exchange(self.refresh_request(tokens['refresh_token']))

    def test_new_client_still_requires_specific_owner_approval(self):
        self.exchange(self.approved_code())
        self.oauth = self.restart()
        self.client = self.oauth.register({'redirect_uris': [CHATGPT_REDIRECT]})['client_id']
        rid = self.oauth.authorize(self.request())
        self.assertIsNone(self.oauth.continue_authorization(rid))

    def test_known_public_client_id_does_not_auto_approve_an_authorization(self):
        self.exchange(self.approved_code())
        self.restart()
        rid = self.oauth.authorize(self.request())
        self.assertIsNone(self.oauth.continue_authorization(rid))

    def test_pending_request_and_one_time_code_are_not_restored(self):
        rid = self.oauth.authorize(self.request())
        code = self.approved_code()
        restarted = self.restart()
        with self.assertRaises(OAuthError): restarted.continue_authorization(rid)
        with self.assertRaises(OAuthError): self.exchange(code)

    def test_second_persistent_writer_is_rejected(self):
        with self.assertRaises(BlockingIOError):
            OwnerOAuth(self.directory, clock=lambda: self.now, persistent=True)

    def test_corrupt_store_fails_closed_and_is_not_silently_overwritten(self):
        path = self.directory / 'authorization-state.json'
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError): self.restart()
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_storage_failure_issues_no_token_and_disables_authorization(self):
        code = self.approved_code()
        with mock.patch.object(self.oauth.store, 'save', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OAuthError) as error:
                self.exchange(code)
        self.assertEqual(error.exception.status, 503)
        self.assertFalse(self.oauth.authenticated('Bearer synthetic'))
        with self.assertRaises(OAuthError):
            self.oauth.register({'redirect_uris': [CHATGPT_REDIRECT]})

    def test_failed_rotation_preserves_previous_complete_state_without_temp_files(self):
        tokens = self.exchange(self.approved_code())
        path = self.directory / 'authorization-state.json'
        before = path.read_bytes()
        with mock.patch('probe.oauth_state.os.replace', side_effect=OSError('synthetic rename failure')):
            with self.assertRaises(OAuthError):
                self.oauth.exchange(self.refresh_request(tokens['refresh_token']))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob('.authorization-*.tmp')))
        recovered = self.restart().exchange(self.refresh_request(tokens['refresh_token']))
        self.assertTrue(self.oauth.authenticated('Bearer ' + recovered['access_token']))


class LocalApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('local_approval', root / 'scripts/approve-server-url.py')
        cls.approval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.approval)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from probe.platform_io import private_dir
        from probe.server_url import private_json, SCOPE
        self.directory = private_dir(self.root / 'runtime/server-url/oauth')
        self.rid = 'a' * 32
        self.record = {'expires': 9999999999, 'redirect_uri': CHATGPT_REDIRECT,
                       'scope': SCOPE, 'resource': ORIGIN, 'approval_challenge': 'synthetic-local-proof'}
        self.json = private_json
        self.json(self.root / 'runtime/server-url/endpoint.json', {'origin': ORIGIN})

    def test_delegated_exact_request_requires_no_input_and_emits_no_local_proof(self):
        self.json(self.directory / (self.rid + '.request.json'), self.record)
        output = io.StringIO()
        with mock.patch.object(self.approval, 'ROOT', self.root), mock.patch('builtins.input', side_effect=AssertionError('Must not prompt')), contextlib.redirect_stdout(output):
            self.approval.main(['--request', self.rid, '--yes'])
        self.assertNotIn(self.record['approval_challenge'], output.getvalue())
        self.assertEqual(self.json(self.directory / (self.rid + '.approval.json'))['request_id'], self.rid)

    def test_delegated_approval_rejects_wrong_scope_origin_redirect_and_expiry(self):
        for wrong in ({'scope': 'admin'}, {'resource': 'https://other.example'},
                      {'redirect_uri': 'https://evil.example/cb'}, {'expires': 0}):
            path = self.directory / (self.rid + '.request.json')
            path.unlink(missing_ok=True)
            self.json(path, dict(self.record, **wrong))
            with self.subTest(wrong=wrong), mock.patch.object(self.approval, 'ROOT', self.root), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.approval.main(['--request', self.rid, '--yes'])
                self.assertEqual(error.exception.code, 1)
                self.assertFalse((self.directory / (self.rid + '.approval.json')).exists())


class FixedIngressArgumentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('fixed_launcher', root / 'scripts/run-server-url.py')
        cls.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.launcher)

    def test_existing_default_remains_temporary_and_memory_only(self):
        args = self.launcher.parse_args([])
        self.assertIsNone(args.public_origin)
        self.assertFalse(args.persistent_oauth)

    def test_persistence_requires_both_fixed_origin_and_explicit_port(self):
        for args in (['--persistent-oauth'], ['--persistent-oauth', '--public-origin', ORIGIN],
                     ['--persistent-oauth', '--public-origin', 'https://temporary.trycloudflare.com', '--port', '8787']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): self.launcher.parse_args(args)

    def test_fixed_configuration_is_opt_in(self):
        args = self.launcher.parse_args(['--persistent-oauth', '--public-origin', ORIGIN, '--port', '8787'])
        self.assertEqual((args.public_origin, args.port, args.persistent_oauth), (ORIGIN, 8787, True))


if __name__ == '__main__': unittest.main()
