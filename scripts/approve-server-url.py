#!/usr/bin/env python3
"""Owner-run or explicitly delegated single-request local approval.

No secret is printed or supplied on the command line. Public requests cannot
select noninteractive approval; --yes is available only to a local operator.
"""
import argparse
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.server_url import private_json, CHATGPT_REDIRECT, SCOPE
from probe.platform_io import configure_stdio, private_dir


def main(argv=None):
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--yes', action='store_true', help='Explicit local authorization for this exact request; requires owner or delegated authority')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9a-f]{32}', args.request):
        parser.error('Invalid request ID')
    directory = private_dir(ROOT / 'runtime/server-url/oauth')
    try:
        record = private_json(directory / (args.request + '.request.json'))
        endpoint = private_json(ROOT / 'runtime/server-url/endpoint.json')
        if (record['expires'] <= time.time() or record['redirect_uri'] != CHATGPT_REDIRECT
                or record['scope'] != SCOPE or record['resource'] != endpoint['origin']):
            raise ValueError('Expired or unapproved redirect')
    except (OSError, ValueError, KeyError):
        parser.exit(1, 'No current authorized ChatGPT request found.\n')
    print('Request:', args.request)
    print('Return to:', record['redirect_uri'])
    print('Permission: owner WeChat messages, replies, attachments and events.')
    print('Check that the same request ID is visible in your ChatGPT authorization browser tab.')
    if not args.yes and input('Type APPROVE to authorize this one connection: ').strip() != 'APPROVE':
        parser.exit(1, 'No authorization granted.\n')
    private_json(directory / (args.request + '.approval.json'),
                 {'request_id': args.request, 'challenge': record['approval_challenge']})
    print('Approved. Return to the browser; it will redirect to ChatGPT.')


if __name__ == '__main__':
    main()
