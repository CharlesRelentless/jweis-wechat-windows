"""Single-owner OAuth gateway for the unchanged stdio bridge.

Public OAuth requests never grant access. The Windows owner must approve each
link using a private local file, bound to one request, redirect and S256 PKCE.
This foreground validation gateway intentionally keeps tokens in memory.
"""
import base64
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .platform_io import private_dir, open_private
from .outbound import MAX_RPC_BYTES
from .probe import VERSION

SCOPE = 'weixin:owner'
CHATGPT_REDIRECT = 'https://chatgpt.com/connector_platform_oauth_redirect'
MAX_RECORDS = 32


def private_json(path, value=None):
    flags = os.O_RDONLY if value is None else os.O_CREAT | os.O_EXCL | os.O_WRONLY
    with os.fdopen(open_private(path, flags), 'r' if value is None else 'w', encoding='utf-8') as f:
        if value is None:
            return json.load(f)
        json.dump(value, f, ensure_ascii=False)


class OAuthError(Exception):
    def __init__(self, reason, status=400):
        self.reason, self.status = reason, status


def one(values, key, default=None):
    value = values.get(key, [default])
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        raise OAuthError('invalid_request')
    return value[0]


class OwnerOAuth:
    def __init__(self, directory, clock=time.time, redirects=(CHATGPT_REDIRECT,)):
        self.directory = private_dir(directory)
        self.clock, self.redirects = clock, frozenset(redirects)
        self.origin = None
        self.clients, self.pending, self.codes, self.tokens, self.refresh = {}, {}, {}, {}, {}
        self.lock = threading.RLock()

    def configure_origin(self, origin):
        parts = urlsplit(origin)
        if (parts.scheme != 'https' or not parts.hostname or parts.port is not None
                or parts.username or parts.password or parts.path or parts.query or parts.fragment
                or not re.fullmatch(r'[a-z0-9][a-z0-9.-]+', parts.hostname)):
            raise ValueError('Canonical HTTPS origin required')
        with self.lock:
            if self.origin and self.origin != origin:
                raise ValueError('Origin cannot change within a live authorization session')
            self.origin = origin

    def prune(self):
        now = self.clock()
        for collection in (self.pending, self.codes, self.tokens, self.refresh):
            for key, record in list(collection.items()):
                if record['expires'] <= now:
                    del collection[key]
                    if collection is self.pending:
                        for suffix in ('request', 'approval'):
                            (self.directory / (key + '.' + suffix + '.json')).unlink(missing_ok=True)

    def metadata(self):
        return {'issuer': self.origin, 'authorization_endpoint': self.origin + '/oauth/authorize',
                'token_endpoint': self.origin + '/oauth/token',
                'registration_endpoint': self.origin + '/oauth/register',
                'response_types_supported': ['code'],
                'grant_types_supported': ['authorization_code', 'refresh_token'],
                'token_endpoint_auth_methods_supported': ['none'],
                'code_challenge_methods_supported': ['S256'],
                'scopes_supported': [SCOPE],
                'authorization_response_iss_parameter_supported': True}

    def register(self, data):
        with self.lock:
            if len(self.clients) >= MAX_RECORDS:
                raise OAuthError('registration_limit', 429)
            redirects = data.get('redirect_uris')
            if (not isinstance(redirects, list) or not redirects or len(redirects) > 3
                    or any(not isinstance(x, str) or x not in self.redirects for x in redirects)):
                raise OAuthError('invalid_redirect_uri')
            if (data.get('token_endpoint_auth_method', 'none') != 'none'
                    or data.get('response_types', ['code']) != ['code']
                    or set(data.get('grant_types', ['authorization_code', 'refresh_token']))
                    - {'authorization_code', 'refresh_token'}):
                raise OAuthError('invalid_client_metadata')
            client_id = secrets.token_urlsafe(24)
            result = {'client_id': client_id, 'redirect_uris': redirects,
                      'token_endpoint_auth_method': 'none',
                      'grant_types': ['authorization_code', 'refresh_token'],
                      'response_types': ['code'], 'client_id_issued_at': int(self.clock())}
            self.clients[client_id] = result
            return result

    def authorize(self, values):
        with self.lock:
            self.prune()
            client_id = one(values, 'client_id')
            redirect = one(values, 'redirect_uri')
            if client_id not in self.clients or redirect not in self.clients[client_id]['redirect_uris']:
                raise OAuthError('invalid_client')
            if one(values, 'response_type') != 'code' or one(values, 'code_challenge_method') != 'S256':
                raise OAuthError('invalid_request')
            challenge = one(values, 'code_challenge')
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge):
                raise OAuthError('invalid_request')
            resource = one(values, 'resource')
            if resource != self.origin:
                raise OAuthError('invalid_target')
            scope = one(values, 'scope', SCOPE)
            if scope != SCOPE:
                raise OAuthError('invalid_scope')
            state = one(values, 'state')
            if not state or len(state) > 1024:
                raise OAuthError('invalid_request')
            if len(self.pending) >= MAX_RECORDS:
                raise OAuthError('authorization_limit', 429)
            request_id = secrets.token_hex(16)
            record = {'client_id': client_id, 'redirect_uri': redirect,
                      'challenge': challenge, 'resource': resource, 'scope': scope,
                      'state': state, 'expires': self.clock() + 600,
                      'approval_challenge': secrets.token_urlsafe(32)}
            # The random local approval challenge never appears in public HTML.
            private_json(self.directory / (request_id + '.request.json'), record)
            self.pending[request_id] = record
            return request_id

    def continue_authorization(self, request_id):
        with self.lock:
            self.prune()
            record = self.pending.get(request_id)
            if record is None:
                raise OAuthError('expired_request', 410)
            marker = self.directory / (request_id + '.approval.json')
            if not marker.exists():
                return None
            approval = private_json(marker)
            if not hmac.compare_digest(str(approval.get('challenge', '')), record['approval_challenge']):
                raise OAuthError('invalid_approval', 403)
            if approval.get('request_id') != request_id:
                raise OAuthError('invalid_approval', 403)
            if len(self.codes) >= MAX_RECORDS:
                raise OAuthError('authorization_limit', 429)
            code = secrets.token_urlsafe(32)
            self.codes[hashlib.sha256(code.encode()).hexdigest()] = dict(record, expires=self.clock() + 90)
            del self.pending[request_id]
            marker.unlink()
            (self.directory / (request_id + '.request.json')).unlink()
            return record['redirect_uri'] + '?' + urlencode({'code': code, 'state': record['state'], 'iss': self.origin})

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode('ascii')).hexdigest()

    def exchange(self, values):
        with self.lock:
            self.prune()
            if len(self.tokens) >= MAX_RECORDS or len(self.refresh) >= MAX_RECORDS:
                raise OAuthError('token_limit', 429)
            client_id = one(values, 'client_id')
            resource = one(values, 'resource')
            grant = one(values, 'grant_type')
            if grant == 'authorization_code':
                code = one(values, 'code')
                if len(code) > 256 or not code.isascii():
                    raise OAuthError('invalid_grant')
                record = self.codes.get(self.digest(code))
                verifier = one(values, 'code_verifier')
                if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
                    raise OAuthError('invalid_grant')
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
                if (not record or record['client_id'] != client_id or record['resource'] != resource
                        or record['redirect_uri'] != one(values, 'redirect_uri')
                        or not hmac.compare_digest(record['challenge'], challenge)):
                    raise OAuthError('invalid_grant')
                del self.codes[self.digest(code)]
            elif grant == 'refresh_token':
                token = one(values, 'refresh_token')
                if len(token) > 256 or not token.isascii():
                    raise OAuthError('invalid_grant')
                record = self.refresh.get(self.digest(token))
                if not record or record['client_id'] != client_id or record['resource'] != resource:
                    raise OAuthError('invalid_grant')
                if 'scope' in values and one(values, 'scope') != record['scope']:
                    raise OAuthError('invalid_scope')
                del self.refresh[self.digest(token)]
            else:
                raise OAuthError('unsupported_grant_type')
            access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            common = {'client_id': client_id, 'resource': resource, 'scope': record['scope']}
            self.tokens[self.digest(access)] = dict(common, expires=self.clock() + 3600)
            self.refresh[self.digest(refresh)] = dict(common, expires=self.clock() + 86400)
            return {'access_token': access, 'refresh_token': refresh,
                    'token_type': 'Bearer', 'expires_in': 3600, 'scope': SCOPE}

    def authenticated(self, authorization):
        with self.lock:
            self.prune()
            if not isinstance(authorization, str) or not authorization.startswith('Bearer '):
                return False
            token = authorization[7:]
            if not token.isascii() or len(token) > 256:
                return False
            record = self.tokens.get(self.digest(token))
            return bool(record and record['resource'] == self.origin and record['scope'] == SCOPE)


class StdioBridge:
    def __init__(self, root, state, callback_hosts=()):
        command = [sys.executable, str(root / 'probe/probe.py'), '--state-dir', str(state)]
        for host in callback_hosts:
            command.extend(['--allow-callback-host', host])
        self.process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lock = threading.Lock()

    def rpc(self, request):
        with self.lock:
            if self.process.poll() is not None:
                raise RuntimeError('Bridge worker stopped')
            self.process.stdin.write(json.dumps(request, ensure_ascii=False).encode() + b'\n')
            self.process.stdin.flush()
            if 'id' not in request:
                return None
            data = self.process.stdout.readline(2 * 1024 * 1024)
            if not data or not data.endswith(b'\n'):
                raise RuntimeError('Invalid bridge response')
            result = json.loads(data)
            if not isinstance(result, dict) or result.get('id') != request.get('id'):
                raise RuntimeError('Bridge response did not match the request')
            return result

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, oauth, bridge, port=0):
        self.oauth, self.bridge = oauth, bridge
        self.request_slots = threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1', port), Handler)

    def process_request(self, request, address):
        if not self.request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.request_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'JweisBridge'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass  # Never log OAuth codes, bearer tokens, callback URLs or messages.

    def reply(self, status, data=None, headers=None, content_type='application/json'):
        body = b'' if data is None else (json.dumps(data, ensure_ascii=False).encode()
                                         if content_type == 'application/json' else data.encode())
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def checks(self):
        oauth = self.server.oauth
        if not oauth.origin:
            raise OAuthError('initializing', 503)
        # Both the externally forwarded Host and direct loopback validation are
        # checked. X-Forwarded-* never controls trust or the issuer.
        allowed = {urlsplit(oauth.origin).netloc, '127.0.0.1:' + str(self.server.server_port)}
        if self.headers.get('Host') not in allowed:
            raise OAuthError('invalid_host', 403)
        origin = self.headers.get('Origin')
        if origin and origin not in {oauth.origin, 'https://chatgpt.com'}:
            raise OAuthError('invalid_origin', 403)
        if self.headers.get('Transfer-Encoding'):
            raise OAuthError('unsupported_encoding', 400)

    def body(self, form=False):
        if len(self.headers.get_all('Content-Length') or []) != 1:
            raise OAuthError('invalid_request')
        try:
            size = int(self.headers['Content-Length'])
        except (ValueError, TypeError):
            raise OAuthError('invalid_request')
        if size < 0 or size > MAX_RPC_BYTES:
            raise OAuthError('request_too_large', 413)
        expected = 'application/x-www-form-urlencoded' if form else 'application/json'
        if self.headers.get('Content-Type', '').split(';')[0].strip() != expected:
            raise OAuthError('unsupported_media_type', 415)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise OAuthError('invalid_request')
        try:
            result = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=32) if form else json.loads(raw)
        except (ValueError, UnicodeError):
            raise OAuthError('invalid_request')
        if not isinstance(result, dict):
            raise OAuthError('invalid_request')
        return result

    def challenge(self):
        self.reply(401, {'error': 'authorization_required'}, {
            'WWW-Authenticate': 'Bearer resource_metadata="' + self.server.oauth.origin
            + '/.well-known/oauth-protected-resource", scope="' + SCOPE + '"'})

    def waiting(self, request_id):
        content = ('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3;url=/oauth/continue?request_id='
                   + request_id + '"><title>Jweis 微信桥接授权</title><h1>等待电脑主人批准</h1>'
                   '<p>此连接能够读取本人微信消息、收发图片和文件，并订阅来信事件。</p>'
                   '<p>在本机运行 scripts/approve-server-url.py，核对并批准此请求：</p><pre>'
                   + html.escape(request_id) + '</pre><p>批准后本页会返回 ChatGPT。不要把密钥发到聊天。</p>')
        self.reply(200, content, content_type='text/html; charset=utf-8')

    def do_GET(self):
        try:
            self.checks()
            path = urlsplit(self.path).path
            oauth = self.server.oauth
            if path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
                self.reply(200, {'resource': oauth.origin, 'authorization_servers': [oauth.origin],
                                 'scopes_supported': [SCOPE], 'bearer_methods_supported': ['header']})
            elif path == '/.well-known/oauth-authorization-server':
                self.reply(200, oauth.metadata())
            elif path in ('/oauth/authorize', '/oauth/continue'):
                values = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=32)
                request_id = oauth.authorize(values) if path.endswith('authorize') else one(values, 'request_id')
                if not re.fullmatch(r'[0-9a-f]{32}', request_id):
                    raise OAuthError('invalid_request')
                target = oauth.continue_authorization(request_id)
                if target:
                    self.reply(302, headers={'Location': target})
                else:
                    self.waiting(request_id)
            elif path == '/mcp':
                if not oauth.authenticated(self.headers.get('Authorization')):
                    self.challenge()
                else:
                    self.reply(405, {'error': 'json_post_only'}, {'Allow': 'POST'})
            elif path in ('/healthz', '/readyz'):
                # No public account status or private diagnostic data.
                result = self.server.bridge.rpc({'jsonrpc': '2.0', 'id': 'health', 'method': 'ping'})
                if 'error' in result:
                    raise OAuthError('worker_unavailable', 503)
                self.reply(200, {'status': 'ready'})
            else:
                self.reply(404, {'error': 'not_found'})
        except OAuthError as error:
            self.reply(error.status, {'error': error.reason})
        except Exception:
            self.reply(503, {'error': 'request_unavailable'})

    def do_POST(self):
        try:
            self.checks()
            path = urlsplit(self.path).path
            oauth = self.server.oauth
            if path == '/oauth/register':
                self.reply(201, oauth.register(self.body()))
            elif path == '/oauth/token':
                self.reply(200, oauth.exchange(self.body(form=True)))
            elif path == '/mcp':
                if not oauth.authenticated(self.headers.get('Authorization')):
                    self.close_connection = True
                    self.challenge()
                    return
                version = self.headers.get('MCP-Protocol-Version')
                if version and version not in (VERSION, '2025-11-25', '2025-06-18', '2025-03-26'):
                    raise OAuthError('unsupported_protocol')
                request = self.body()
                if request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                    raise OAuthError('invalid_request')
                if 'id' not in request and request['method'] not in ('notifications/initialized', 'notifications/cancelled'):
                    raise OAuthError('unsupported_notification')
                if 'id' in request and (isinstance(request['id'], bool) or
                        not isinstance(request['id'], (int, str, type(None)))):
                    raise OAuthError('invalid_request')
                result = self.server.bridge.rpc(request)
                self.reply(202 if result is None else 200, result,
                           {'MCP-Protocol-Version': VERSION})
            else:
                self.reply(404, {'error': 'not_found'})
        except OAuthError as error:
            self.close_connection = True  # A rejected body is not another request.
            self.reply(error.status, {'error': error.reason})
        except Exception:
            self.close_connection = True
            self.reply(503, {'error': 'request_unavailable'})

    def do_OPTIONS(self):
        try:
            self.checks()
            self.reply(204, headers={'Access-Control-Allow-Origin': self.headers.get('Origin', self.server.oauth.origin),
                                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                                    'Access-Control-Allow-Headers': 'Authorization, Content-Type, MCP-Protocol-Version'})
        except OAuthError as error:
            self.reply(error.status, {'error': error.reason})
