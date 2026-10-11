"""Bounded, private, atomic storage of OAuth client metadata and token hashes.

No bearer token, refresh token, pending consent or authorization code is saved.
The existing private-file boundary excludes processes running as the same owner.
"""
import json
import math
import os
from pathlib import Path
import re
import secrets

from .platform_io import is_private, open_private, safe_path, acquire_worker_lock

MAX_STATE_BYTES = 1024 * 1024


class OAuthState:
    def __init__(self, directory, redirects, scope, limit):
        self.path = Path(directory) / 'authorization-state.json'
        self.redirects, self.scope, self.limit = frozenset(redirects), scope, limit
        self.owner_lock = acquire_worker_lock(directory)

    def close(self):
        self.owner_lock.close()

    @staticmethod
    def finite_number(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    def validate(self, state, origin):
        def invalid(): raise ValueError('Invalid private authorization state')
        if (not isinstance(state, dict) or set(state) != {'version', 'origin', 'clients', 'tokens', 'refresh'}
                or type(state['version']) is not int or state['version'] != 1):
            invalid()
        if state['origin'] != origin:
            raise ValueError('Stored OAuth origin differs from the configured fixed origin')
        for name in ('clients', 'tokens', 'refresh'):
            if not isinstance(state[name], dict) or len(state[name]) > self.limit: invalid()
        for key, client in state['clients'].items():
            if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', key)
                    or not isinstance(client, dict)
                    or set(client) != {'client_id', 'redirect_uris', 'token_endpoint_auth_method',
                                       'grant_types', 'response_types', 'client_id_issued_at'}
                    or client['client_id'] != key
                    or not isinstance(client['redirect_uris'], list) or not 1 <= len(client['redirect_uris']) <= 3
                    or any(not isinstance(x, str) or x not in self.redirects for x in client['redirect_uris'])
                    or client['token_endpoint_auth_method'] != 'none'
                    or client['grant_types'] != ['authorization_code', 'refresh_token']
                    or client['response_types'] != ['code']
                    or not self.finite_number(client['client_id_issued_at'])):
                invalid()
        for name in ('tokens', 'refresh'):
            for key, record in state[name].items():
                if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key)
                        or not isinstance(record, dict)
                        or set(record) != {'client_id', 'resource', 'scope', 'expires'}
                        or not isinstance(record['client_id'], str) or record['client_id'] not in state['clients']
                        or record['resource'] != origin or record['scope'] != self.scope
                        or not self.finite_number(record['expires'])):
                    invalid()

    def load(self, origin):
        path = safe_path(self.path)
        if not path.exists(): return None
        if not is_private(path): raise ValueError('Authorization state permissions are not private')
        with os.fdopen(open_private(path, os.O_RDONLY), 'rb') as source:
            data = source.read(MAX_STATE_BYTES + 1)
        if len(data) > MAX_STATE_BYTES: raise ValueError('Private authorization state exceeds its bound')
        try: state = json.loads(data)
        except (ValueError, UnicodeError):
            raise ValueError('Invalid private authorization state') from None
        self.validate(state, origin)
        return state

    def save(self, state):
        self.validate(state, state['origin'])
        data = json.dumps(state, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(data) > MAX_STATE_BYTES: raise ValueError('Private authorization state exceeds its bound')
        path = safe_path(self.path)
        if path.exists() and not is_private(path):
            raise ValueError('Authorization state permissions are not private')
        temporary = path.with_name('.authorization-' + secrets.token_hex(16) + '.tmp')
        try:
            with os.fdopen(open_private(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY), 'wb') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
