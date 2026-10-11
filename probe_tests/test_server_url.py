import base64
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from probe.server_url import OwnerOAuth, OAuthError, Gateway, StdioBridge, private_json, SCOPE, CHATGPT_REDIRECT
from probe.platform_io import is_private

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://gateway.example.com'
VERIFIER = 'a' * 64


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.oauth = OwnerOAuth(Path(self.temp.name) / 'oauth', clock=lambda: self.now)
        self.oauth.configure_origin(ORIGIN)
        self.client = self.oauth.register({'redirect_uris': [CHATGPT_REDIRECT]})['client_id']

    def request(self, **overrides):
        params = {'client_id': self.client, 'redirect_uri': CHATGPT_REDIRECT,
                  'response_type': 'code', 'code_challenge_method': 'S256',
                  'code_challenge': challenge(VERIFIER), 'state': 'synthetic-state',
                  'resource': ORIGIN, 'scope': SCOPE}
        params.update(overrides)
        return {k: [v] for k, v in params.items()}

    def approved_code(self):
        rid = self.oauth.authorize(self.request())
        record = private_json(self.oauth.directory / (rid + '.request.json'))
        private_json(self.oauth.directory / (rid + '.approval.json'),
                     {'request_id': rid, 'challenge': record['approval_challenge']})
        target = self.oauth.continue_authorization(rid)
        values = parse_qs(urlsplit(target).query)
        self.assertEqual(values['state'], ['synthetic-state'])
        self.assertEqual(values['iss'], [ORIGIN])
        return values['code'][0]

    def exchange(self, code, **overrides):
        values = {'grant_type': 'authorization_code', 'client_id': self.client,
                  'redirect_uri': CHATGPT_REDIRECT, 'code_verifier': VERIFIER,
                  'code': code, 'resource': ORIGIN}
        values.update(overrides)
        return self.oauth.exchange({k: [v] for k, v in values.items()})

    def test_public_request_cannot_approve_itself(self):
        rid = self.oauth.authorize(self.request())
        self.assertIsNone(self.oauth.continue_authorization(rid))
        self.assertTrue(is_private(self.oauth.directory / (rid + '.request.json')))
        private_json(self.oauth.directory / (rid + '.approval.json'),
                     {'request_id': rid, 'challenge': 'forged'})
        with self.assertRaises(OAuthError): self.oauth.continue_authorization(rid)
        self.assertFalse(self.oauth.tokens)

    def test_pkce_redirect_resource_and_client_are_bound(self):
        code = self.approved_code()
        for wrong in ({'code_verifier': 'b' * 64}, {'redirect_uri': 'https://evil.example/cb'},
                      {'resource': 'https://evil.example'}, {'client_id': 'other'}):
            with self.assertRaises(OAuthError): self.exchange(code, **wrong)
        tokens = self.exchange(code)
        self.assertTrue(self.oauth.authenticated('Bearer ' + tokens['access_token']))
        with self.assertRaises(OAuthError): self.exchange(code)

    def test_refresh_is_bound_and_rotates(self):
        tokens = self.exchange(self.approved_code())
        params = {'grant_type': ['refresh_token'], 'client_id': [self.client],
                  'resource': [ORIGIN], 'refresh_token': [tokens['refresh_token']]}
        with self.assertRaises(OAuthError):
            self.oauth.exchange(dict(params, resource=['https://other.example']))
        fresh = self.oauth.exchange(params)
        self.assertNotEqual(tokens['refresh_token'], fresh['refresh_token'])
        with self.assertRaises(OAuthError): self.oauth.exchange(params)

    def test_expiry_and_revocation_on_process_restart(self):
        tokens = self.exchange(self.approved_code())
        self.now += 3601
        self.assertFalse(self.oauth.authenticated('Bearer ' + tokens['access_token']))
        restarted = OwnerOAuth(self.oauth.directory, clock=lambda: self.now)
        restarted.configure_origin(ORIGIN)
        self.assertFalse(restarted.authenticated('Bearer ' + tokens['access_token']))

    def test_authorization_code_expiry(self):
        code = self.approved_code()
        self.now += 91
        with self.assertRaises(OAuthError): self.exchange(code)

    def test_rejects_duplicate_parameters_and_weak_pkce(self):
        values = self.request()
        values['client_id'].append('other')
        with self.assertRaises(OAuthError): self.oauth.authorize(values)
        for wrong in ({'code_challenge_method': 'plain'}, {'scope': 'admin'},
                      {'resource': 'https://other.example'}, {'state': ''}):
            with self.assertRaises(OAuthError): self.oauth.authorize(self.request(**wrong))

    def test_redirect_allowlist_and_bounded_registration(self):
        for redirect in ('http://chatgpt.com/cb', 'https://chatgpt.com.evil.example/cb',
                         CHATGPT_REDIRECT + '?other=1'):
            with self.assertRaises(OAuthError): self.oauth.register({'redirect_uris': [redirect]})
        for _ in range(31): self.oauth.register({'redirect_uris': [CHATGPT_REDIRECT]})
        with self.assertRaises(OAuthError) as error:
            self.oauth.register({'redirect_uris': [CHATGPT_REDIRECT]})
        self.assertEqual(error.exception.status, 429)

    def test_only_canonical_https_issuer(self):
        for url in ('http://example.com', 'https://example.com/', 'https://a:b@example.com',
                    'https://example.com?x=1', 'https://example.com:8443'):
            with self.assertRaises(ValueError): OwnerOAuth(self.oauth.directory).configure_origin(url)
        with self.assertRaises(ValueError): self.oauth.configure_origin('https://changed.example')


class HTTPTests(unittest.TestCase):
    request = OAuthTests.request
    approved_code = OAuthTests.approved_code
    exchange = OAuthTests.exchange

    def setUp(self):
        OAuthTests.setUp(self)
        self.bridge = StdioBridge(ROOT, Path(self.temp.name) / 'bridge')
        self.addCleanup(self.bridge.close)
        self.server = Gateway(self.oauth, self.bridge)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        def close():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(close)

    def send(self, method, path, value=None, headers=None, form=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        body = None if value is None else (urlencode(value) if form else json.dumps(value))
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault('Content-Type', 'application/x-www-form-urlencoded' if form else 'application/json')
        try:
            connection.request(method, path, body, hdrs)
            response = connection.getresponse()
            result = response.read()
            return response.status, dict(response.getheaders()), result
        finally: connection.close()

    def rpc(self, name, token=None, params=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        status, _, body = self.send('POST', '/mcp',
                                   {'jsonrpc': '2.0', 'id': 1, 'method': name, 'params': params or {}}, headers)
        return status, json.loads(body)

    def test_every_rpc_requires_authentication(self):
        for method in ('server/discover', 'initialize', 'tools/list', 'tools/call',
                       'events/list', 'events/subscribe', 'events/unsubscribe'):
            self.assertEqual(self.rpc(method)[0], 401)
        status, headers, _ = self.send('GET', '/mcp')
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata=', headers['WWW-Authenticate'])

    def test_real_stdio_tool_and_event_catalog_survive_adapter(self):
        token = self.exchange(self.approved_code())['access_token']
        status, discovery = self.rpc('server/discover', token)
        self.assertEqual(status, 200)
        self.assertEqual(discovery['result']['supportedVersions'], ['2026-07-28'])
        self.assertIn('events', discovery['result']['capabilities'])
        status, tools = self.rpc('tools/list', token)
        self.assertIn('get_weixin_status', [x['name'] for x in tools['result']['tools']])
        status, result = self.rpc('tools/call', token, {'name': 'get_weixin_status', 'arguments': {}})
        self.assertEqual(status, 200)
        self.assertFalse(result['result']['structuredContent']['bound'])
        status, events = self.rpc('events/list', token)
        self.assertIn('weixin.message', [x['name'] for x in events['result']['events']])

    def test_dns_rebinding_origin_and_forwarded_host_do_not_bypass(self):
        for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                        {'Host': 'evil.example', 'X-Forwarded-Host': 'gateway.example.com'}):
            self.assertEqual(self.send('GET', '/healthz', headers=headers)[0], 403)

    def test_public_metadata_has_pkce_and_exact_issuer(self):
        status, _, body = self.send('GET', '/.well-known/oauth-authorization-server')
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data['issuer'], ORIGIN)
        self.assertEqual(data['code_challenge_methods_supported'], ['S256'])
        self.assertTrue(data['authorization_response_iss_parameter_supported'])

    def test_notification_accepted_without_body_and_no_sse(self):
        token = self.exchange(self.approved_code())['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        status, _, body = self.send('POST', '/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers)
        self.assertEqual((status, body), (202, b''))
        self.assertEqual(self.send('GET', '/mcp', headers=headers)[0], 405)

    def test_public_consent_does_not_expose_local_approval_secret(self):
        status, headers, body = self.send('GET', '/oauth/authorize?' + urlencode({k: v[0] for k, v in self.request().items()}))
        self.assertEqual(status, 200)
        record = next(iter(self.oauth.pending.values()))
        self.assertNotIn(record['approval_challenge'].encode(), body)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn('frame-ancestors', headers['Content-Security-Policy'])
        self.assertFalse(self.oauth.tokens)

    def test_oversize_and_non_json_rejected_after_auth(self):
        from probe.outbound import MAX_RPC_BYTES
        token = self.exchange(self.approved_code())['access_token']
        headers = {'Authorization': 'Bearer ' + token, 'Content-Length': str(MAX_RPC_BYTES + 1)}
        self.assertEqual(self.send('POST', '/mcp', {}, headers)[0], 413)
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'text/plain'}
        self.assertEqual(self.send('POST', '/mcp', {}, headers)[0], 415)

    def test_invalid_notification_does_not_desynchronize_worker(self):
        token = self.exchange(self.approved_code())['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.send('POST', '/mcp', {'jsonrpc': '2.0', 'method': 'unknown'}, headers)[0], 400)
        self.assertEqual(self.rpc('ping', token), (200, {'jsonrpc': '2.0', 'id': 1, 'result': {}}))


if __name__ == '__main__': unittest.main()
