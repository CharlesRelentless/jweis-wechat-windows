#!/usr/bin/env python3
"""Foreground OAuth gateway with Quick Tunnel or a configured fixed ingress."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.server_url import Gateway, OwnerOAuth, StdioBridge
from probe.platform_io import configure_stdio, private_dir, open_private
from probe.client_integrity import verify_bundle
from probe.windows_job import WindowsJob


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-callback-host', action='append', default=[])
    parser.add_argument('--public-origin', help='Existing fixed HTTPS ingress origin; skips Quick Tunnel creation')
    parser.add_argument('--port', type=int, default=0, help='Gateway loopback port for an existing ingress')
    parser.add_argument('--persistent-oauth', action='store_true', help='Retain token hashes across same-origin restarts')
    args = parser.parse_args(argv)
    if len(args.allow_callback_host) > 3 or any(not re.fullmatch(r'[a-zA-Z0-9.-]+', x) for x in args.allow_callback_host):
        parser.error('At most three verified exact callback hostnames are allowed')
    if not 0 <= args.port <= 65535: parser.error('Loopback port must be in 0..65535')
    if args.persistent_oauth and (not args.public_origin or not args.port):
        parser.error('Persistent OAuth requires a fixed --public-origin and explicit loopback --port')
    if args.persistent_oauth:
        host = urlsplit(args.public_origin).hostname or ''
        if host == 'trycloudflare.com' or host.endswith('.trycloudflare.com'):
            parser.error('A temporary Quick Tunnel origin cannot provide persistent connection recovery')
    return args


def main():
    configure_stdio()
    args = parse_args()
    if not args.public_origin: verify_bundle(ROOT)
    job = WindowsJob()  # Assign this launcher BEFORE all children are created.
    runtime = private_dir(ROOT / 'runtime/server-url')
    oauth = OwnerOAuth(runtime / 'oauth', persistent=args.persistent_oauth)
    bridge = gateway = edge = server_thread = None
    ready = threading.Event()
    def publish_endpoint(origin, temporary):
        oauth.configure_origin(origin)
        endpoint = {'origin': oauth.origin, 'mcp_url': oauth.origin + '/mcp',
                    'local_port': gateway.server_port, 'temporary': temporary,
                    'persistent_oauth': args.persistent_oauth,
                    'authentication': 'OAuth 2.1 PKCE; owner-approved grant required'}
        with os.fdopen(open_private(runtime / 'endpoint.json', os.O_CREAT | os.O_TRUNC | os.O_WRONLY),
                       'w', encoding='utf-8') as f:
            json.dump(endpoint, f, indent=2)
        print('SERVER_URL=' + endpoint['mcp_url'], flush=True)
        print('No public MCP tool can be called without owner-approved OAuth.', flush=True)
        ready.set()
    try:
        if args.public_origin: oauth.configure_origin(args.public_origin)
        bridge = StdioBridge(ROOT, ROOT / 'runtime/state', args.allow_callback_host)
        gateway = Gateway(oauth, bridge, port=args.port)
        server_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        server_thread.start()
        if args.public_origin:
            publish_endpoint(args.public_origin, False)
        else:
            config = runtime / 'cloudflared-empty.yaml'
            with os.fdopen(open_private(config, os.O_CREAT | os.O_TRUNC | os.O_WRONLY), 'w', encoding='utf-8') as f:
                f.write('{}\n')
            edge = subprocess.Popen([str(ROOT / 'bin/cloudflared.exe'), '--no-autoupdate', '--config', str(config),
                                     'tunnel', '--protocol', 'http2', '--url',
                                     'http://127.0.0.1:' + str(gateway.server_port)],
                                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            def read_edge():
                for raw in edge.stderr:
                    match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', raw.decode('utf-8', 'replace'))
                    if match and not ready.is_set(): publish_endpoint(match[0], True)
            threading.Thread(target=read_edge, daemon=True).start()
        deadline = time.monotonic() + 90
        while not ready.wait(.5):
            if (edge and edge.poll() is not None) or time.monotonic() >= deadline:
                print('Cloudflare endpoint not available; no plugin URL registered.', file=sys.stderr)
                return 1
        while (edge is None or edge.poll() is None) and bridge.process.poll() is None:
            time.sleep(.5)
        print('An owned process stopped; shutting down the foreground connection.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if edge and edge.poll() is None:
            edge.terminate()
            edge.wait(timeout=10)
        if gateway:
            if server_thread: gateway.shutdown()
            gateway.server_close()
        if bridge: bridge.close()
        oauth.close()
        with os.fdopen(open_private(runtime / 'windows-job-summary.json', os.O_CREAT | os.O_TRUNC | os.O_WRONLY),
                       'w', encoding='utf-8') as f:
            json.dump(job.metrics(), f, indent=2)


if __name__ == '__main__':
    sys.exit(main())
