import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from probe.platform_io import open_private
from probe.probe import (PinnedHTTPS, RpcError, callback_proxy_parts, load_callback_proxy,
                         post_https, resolve_public_via_proxy)

HOST = 'callbacks.example.com'
URL = 'https://' + HOST + '/private-event-path'
PROXY = ('127.0.0.1', 7890)


def dns_result(ips=('8.8.8.8',)):
    return {'Status': 0, 'TC': False, 'Question': [{'name': HOST + '.', 'type': 1}],
            'Answer': [{'name': HOST, 'type': 1, 'data': ip} for ip in ips]}


class CallbackNetworkTests(unittest.TestCase):
    def config(self, directory, value):
        p = Path(directory) / 'callback-network.json'
        with os.fdopen(open_private(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), 'wb') as f:
            f.write(value)

    def test_missing_config_retains_direct_route_and_live_update_is_owner_private(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(load_callback_proxy(d))
            self.config(d, b'{"proxy_url":"http://127.0.0.1:7890"}')
            self.assertEqual(load_callback_proxy(d), PROXY)
            self.config(d, b'{"proxy_url":"http://127.0.0.1:7891"}')
            self.assertEqual(load_callback_proxy(d), ('127.0.0.1', 7891))

    def test_malformed_or_oversize_config_never_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            for raw in (b'{}', b'null', b'{"proxy_url":false}', b'not json', b'x' * 4097,
                        b'{"proxy_url":"http://127.0.0.1:7890","headers":{}}'):
                self.config(d, raw)
                with self.subTest(raw=raw[:40]), self.assertRaises(RpcError):
                    load_callback_proxy(d)

    def test_proxy_rejects_remote_hosts_credentials_paths_and_other_schemes(self):
        for value in ('http://example.com:7890', 'http://10.0.0.1:7890',
                      'http://localhost:7890', 'http://user:pass@127.0.0.1:7890',
                      'http://127.0.0.1:7890/path', 'http://127.0.0.1:7890?x=1',
                      'http://127.0.0.1:7890#x', 'http://127.0.0.1',
                      'https://127.0.0.1:7890', 'socks5://127.0.0.1:7890'):
            with self.subTest(value=value), self.assertRaises(RpcError):
                callback_proxy_parts(value)
        self.assertEqual(callback_proxy_parts('http://[::1]:7890'), ('::1', 7890))

    def resolver(self, result=None, status=200, raw=None):
        conn = MagicMock()
        conn.getresponse.return_value.status = status
        conn.getresponse.return_value.read.return_value = raw or json.dumps(result or dns_result()).encode()
        return conn

    def test_proxy_doh_uses_fixed_resolver_verified_tls_and_exact_question(self):
        conn = self.resolver()
        with patch('http.client.HTTPSConnection', return_value=conn) as constructor:
            self.assertEqual(resolve_public_via_proxy(HOST, PROXY), ['8.8.8.8'])
        args, kwargs = constructor.call_args
        self.assertEqual(args, PROXY)
        self.assertEqual(kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs['context'].check_hostname)
        conn.set_tunnel.assert_called_once_with('cloudflare-dns.com', 443)
        self.assertEqual(conn.request.call_args.args[0], 'GET')
        self.assertIn('name=' + HOST, conn.request.call_args.args[1])
        conn.close.assert_called_once()

    def test_doh_mixed_private_address_rejected_before_callback_connection(self):
        conn = self.resolver(dns_result(('8.8.8.8', '127.0.0.1')))
        with patch('http.client.HTTPSConnection', return_value=conn), patch('probe.probe.PinnedHTTPS') as target:
            with self.assertRaises(RpcError): post_https(URL, b'{}', {}, {HOST}, PROXY)
            target.assert_not_called()

    def test_dns_error_mismatched_question_and_truncation_fail_closed(self):
        bad = [dict(dns_result(), Status=3), dict(dns_result(), TC=True),
               dict(dns_result(), Question=[{'name': 'evil.example', 'type': 1}]),
               dict(dns_result(), Answer=[]), dns_result(('::1',)), dns_result(('bad',))]
        for value in bad:
            with self.subTest(value=value), patch('http.client.HTTPSConnection', return_value=self.resolver(value)):
                with self.assertRaises(RpcError): resolve_public_via_proxy(HOST, PROXY)

    def test_doh_redirect_or_oversize_not_followed(self):
        for conn in (self.resolver(status=302), self.resolver(raw=b'x' * 16385)):
            with patch('http.client.HTTPSConnection', return_value=conn):
                with self.assertRaises(RpcError): resolve_public_via_proxy(HOST, PROXY)
            conn.request.assert_called_once()
            conn.close.assert_called_once()

    def test_unknown_callback_rejected_before_resolver_or_any_socket(self):
        with patch('probe.probe.resolve_public_via_proxy') as dns, patch('socket.create_connection') as sock:
            with self.assertRaises(RpcError): post_https(URL, b'{}', {}, set(), PROXY)
            dns.assert_not_called(); sock.assert_not_called()

    def test_connect_uses_pinned_public_ip_and_original_hostname_for_tls(self):
        conn = PinnedHTTPS(HOST, '8.8.8.8', PROXY)
        sock, wrapped = MagicMock(), MagicMock()
        context = MagicMock(); context.wrap_socket.return_value = wrapped
        conn._context = context
        self.assertEqual(conn._tunnel_host, '8.8.8.8')
        self.assertEqual(conn._tunnel_port, 443)
        with patch('socket.create_connection', return_value=sock) as connect, patch.object(conn, '_tunnel') as tunnel:
            conn.connect()
        connect.assert_called_once_with(PROXY, timeout=10)
        tunnel.assert_called_once()
        context.wrap_socket.assert_called_once_with(sock, server_hostname=HOST)
        self.assertIs(conn.sock, wrapped)
        conn.close()

    def test_tls_failure_closes_socket_without_fallback(self):
        conn = PinnedHTTPS(HOST, '8.8.8.8', PROXY)
        sock = MagicMock(); context = MagicMock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('verification failed')
        conn._context = context
        with patch('socket.create_connection', return_value=sock) as connect, patch.object(conn, '_tunnel'):
            with self.assertRaises(ssl.SSLCertVerificationError): conn.connect()
        self.assertEqual(connect.call_count, 1)
        sock.close.assert_called_once()

    def test_one_post_keeps_exact_body_headers_and_does_not_follow_redirects(self):
        target = MagicMock(); target.getresponse.return_value.status = 302
        target.getresponse.return_value.read.return_value = b'not a challenge'
        body = b'{"type":"verification","challenge":"synthetic"}'
        hdrs = {'webhook-id': 'synthetic-id', 'webhook-signature': 'synthetic-signature'}
        with patch('probe.probe.resolve_public_via_proxy', return_value=['8.8.8.8']), \
                patch('probe.probe.PinnedHTTPS', return_value=target) as constructor:
            self.assertEqual(post_https(URL, body, hdrs, {HOST}, PROXY), (302, b'not a challenge'))
        constructor.assert_called_once_with(HOST, '8.8.8.8', PROXY)
        target.request.assert_called_once_with('POST', '/private-event-path', body=body,
                                                headers=dict(hdrs, Host=HOST))
        self.assertNotIn('Host', hdrs)
        target.close.assert_called_once()

    def test_supplied_host_cannot_change_https_authority(self):
        target = MagicMock(); target.getresponse.return_value.status = 200
        target.getresponse.return_value.read.return_value = b'{}'
        with patch('probe.probe.resolve_public_via_proxy', return_value=['8.8.8.8']), \
                patch('probe.probe.PinnedHTTPS', return_value=target):
            post_https(URL, b'{}', {'Host': 'evil.example'}, {HOST}, PROXY)
        self.assertEqual(target.request.call_args.kwargs['headers']['Host'], HOST)


if __name__ == '__main__': unittest.main()
