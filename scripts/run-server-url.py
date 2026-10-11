#!/usr/bin/env python3
"""Foreground OAuth + JSON HTTP bridge and an owned Cloudflare Quick Tunnel."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.server_url import Gateway, OwnerOAuth, StdioBridge
from probe.platform_io import configure_stdio, private_dir, open_private
from probe.client_integrity import verify_bundle
from probe.windows_job import WindowsJob


def main():
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-callback-host', action='append', default=[])
    args = parser.parse_args()
    if len(args.allow_callback_host) > 3 or any(not re.fullmatch(r'[a-zA-Z0-9.-]+', x) for x in args.allow_callback_host):
        parser.error('At most three verified exact callback hostnames are allowed')
    verify_bundle(ROOT)
    job = WindowsJob()  # Assign this launcher BEFORE all children are created.
    runtime = private_dir(ROOT / 'runtime/server-url')
    oauth = OwnerOAuth(runtime / 'oauth')
    bridge = StdioBridge(ROOT, ROOT / 'runtime/state', args.allow_callback_host)
    gateway = Gateway(oauth, bridge)
    server_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    server_thread.start()
    config = runtime / 'cloudflared-empty.yaml'
    with os.fdopen(open_private(config, os.O_CREAT | os.O_TRUNC | os.O_WRONLY), 'w', encoding='utf-8') as f:
        f.write('{}\n')
    edge = subprocess.Popen([str(ROOT / 'bin/cloudflared.exe'), '--no-autoupdate', '--config', str(config),
                             'tunnel', '--protocol', 'http2', '--url',
                             'http://127.0.0.1:' + str(gateway.server_port)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    ready = threading.Event()
    def read_edge():
        for raw in edge.stderr:
            line = raw.decode('utf-8', 'replace')
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
            if match and not ready.is_set():
                oauth.configure_origin(match[0])
                endpoint = {'origin': oauth.origin, 'mcp_url': oauth.origin + '/mcp',
                            'local_port': gateway.server_port, 'temporary': True,
                            'authentication': 'OAuth 2.1 PKCE; owner local approval required'}
                with os.fdopen(open_private(runtime / 'endpoint.json', os.O_CREAT | os.O_TRUNC | os.O_WRONLY),
                               'w', encoding='utf-8') as f:
                    json.dump(endpoint, f, indent=2)
                print('SERVER_URL=' + endpoint['mcp_url'], flush=True)
                print('No public MCP tool can be called without owner-approved OAuth.', flush=True)
                ready.set()
    edge_reader = threading.Thread(target=read_edge, daemon=True)
    edge_reader.start()
    try:
        deadline = time.monotonic() + 90
        while not ready.wait(.5):
            if edge.poll() is not None or time.monotonic() >= deadline:
                print('Cloudflare endpoint not available; no plugin URL registered.', file=sys.stderr)
                return 1
        while edge.poll() is None and bridge.process.poll() is None:
            time.sleep(.5)
        print('An owned process stopped; shutting down the foreground connection.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if edge.poll() is None:
            edge.terminate()
            edge.wait(timeout=10)
        gateway.shutdown()
        gateway.server_close()
        bridge.close()
        with os.fdopen(open_private(runtime / 'windows-job-summary.json', os.O_CREAT | os.O_TRUNC | os.O_WRONLY),
                       'w', encoding='utf-8') as f:
            json.dump(job.metrics(), f, indent=2)


if __name__ == '__main__':
    sys.exit(main())
