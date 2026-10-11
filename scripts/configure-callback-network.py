#!/usr/bin/env python3
"""Configure only this private bridge's callback route, without global changes."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.platform_io import configure_stdio, open_private, private_dir
from probe.probe import callback_proxy_parts


def main():
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'runtime/state')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--proxy-url', help='Existing loopback HTTP CONNECT proxy; no credentials')
    modes.add_argument('--direct', action='store_true', help='Restore the original direct callback route')
    args = parser.parse_args()
    state = private_dir(args.state_dir)
    config = state / 'callback-network.json'
    if args.direct:
        try:
            os.close(open_private(config, os.O_RDONLY))
            config.unlink()
        except FileNotFoundError:
            pass
        print(json.dumps({'callback_route': 'direct', 'global_settings_changed': False}))
    else:
        try:
            host, port = callback_proxy_parts(args.proxy_url)
        except Exception:
            parser.error('Only an explicit loopback HTTP proxy without credentials is permitted')
        with os.fdopen(open_private(config, os.O_CREAT | os.O_TRUNC | os.O_WRONLY),
                       'w', encoding='utf-8') as output:
            json.dump({'proxy_url': args.proxy_url}, output)
        print(json.dumps({'callback_route': 'loopback_CONNECT_and_verified_DoH',
                          'proxy_host': host, 'proxy_port': port, 'global_settings_changed': False}))
    return 0


if __name__ == '__main__': sys.exit(main())
