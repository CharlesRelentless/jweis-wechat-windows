"""Read the confirmed private env file for the existing official launcher.

No global environment mutation, second plaintext key file, or network request.
The activation policy is a local start gate, not a financial spending limit.
"""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from probe.platform_io import is_private, safe_path

REFERENCE = 'env:JWEIS_TUNNEL_API_KEY'


def activation_policy(root, profile):
    root = safe_path(root)
    profile = safe_path(profile)
    text = profile.read_text(encoding='utf-8')
    if not re.search(r'^  api_key: [\"\']?' + re.escape(REFERENCE) + r'[\"\']?\s*$',
                     text, re.MULTILINE):
        return None  # Preserve the existing owner-entered raw-key branch.
    tunnel = re.search(r'^  tunnel_id: (tunnel_[a-f0-9]{32})\s*$', text, re.MULTILINE)
    policy_path = safe_path(root / 'runtime/official-tunnel-activation.json')
    if not tunnel or not policy_path.is_file() or not is_private(policy_path):
        raise ValueError('Private official tunnel activation policy is required.')
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    continued = (policy.get('cost_status') == 'no_charge_observed'
                 and policy.get('continue_until_charge') is True)
    if (policy.get('tunnel_id') != tunnel.group(1)
            or not (policy.get('cost_status') == 'confirmed_free' or continued)
            or policy.get('charge_detected') is True
            or policy.get('online_start_authorized') is not True):
        raise ValueError('Official tunnel activation is blocked until free transport is confirmed.')
    return policy


def official_environment(root, profile):
    root = safe_path(root)
    policy = activation_policy(root, profile)
    if policy is None:
        return None
    target = safe_path(root / 'runtime/secrets/openai-tunnel.env')
    if not target.is_file() or not is_private(target) or target.stat().st_size > 16384:
        raise ValueError('The confirmed private runtime env file is required.')
    # This exact format is emitted by the official secure key helper. Do not
    # execute shell syntax or import additional variables from an env file.
    value = target.read_text(encoding='utf-8')
    match = re.fullmatch(r'OPENAI_API_KEY=(sk-[A-Za-z0-9_-]{16,})\r?\n?', value)
    if not match:
        raise ValueError('The runtime env file has an unsupported format.')
    child_env = dict(os.environ)
    child_env.pop('OPENAI_API_KEY', None)
    child_env['JWEIS_TUNNEL_API_KEY'] = match.group(1)
    if policy.get('proxy_url'):
        proxy = urlsplit(policy['proxy_url'])
        if (proxy.scheme != 'http' or proxy.hostname not in ('127.0.0.1', '::1')
                or not proxy.port or proxy.username or proxy.password
                or proxy.path not in ('', '/') or proxy.query or proxy.fragment):
            raise ValueError('Only the approved loopback proxy is allowed.')
        child_env['HTTPS_PROXY'] = child_env['HTTP_PROXY'] = policy['proxy_url']
    return child_env
