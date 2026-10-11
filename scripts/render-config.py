#!/usr/bin/env python3
"""Render private configuration; values are identifiers/references, never secret bytes."""
import argparse
import os
import json
import sys
from pathlib import Path
import re
import shlex
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from probe.platform_io import WINDOWS, private_dir, open_private, safe_path


def render(root, tunnel_id, callback_hosts, replace=False, discover_callback=False):
    root=safe_path(root)
    if not WINDOWS and not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(root)):
        raise ValueError('Use an absolute installation path without spaces or shell metacharacters')
    if not re.fullmatch(r'tunnel_[a-f0-9]{32}',tunnel_id):
        raise ValueError('Use the actual tunnel ID returned by Platform')
    if discover_callback and callback_hosts:
        raise ValueError('Discovery must deny all callbacks; do not combine it with callback hosts')
    if (not discover_callback and not 1<=len(callback_hosts)<=3) or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?',h) or '.' not in h or '..' in h for h in callback_hosts):
        raise ValueError('Supply 1..3 exact callback hostnames observed from the authorized platform subscription')
    runtime=private_dir(root/'runtime')
    for name in ('state','secrets'): private_dir(runtime/name)
    python=root/('.venv/Scripts/python.exe' if WINDOWS else '.venv/bin/python')
    args=[python.as_posix(),(root/'probe/probe.py').as_posix(),'--state-dir',(runtime/'state').as_posix()]
    for host in callback_hosts: args.extend(['--allow-callback-host',host])
    if WINDOWS:
        # tunnel-client parses its own argv string; this is not cmd.exe quoting.
        def quote_arg(value):
            if not any(c.isspace() or c in '\\"\'' for c in value): return value
            return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'
        command=' '.join(quote_arg(value) for value in args)
        q=lambda value: json.dumps(value,ensure_ascii=False)
        profile='\n'.join([
            'config_version: 1','control_plane:','  tunnel_id: '+tunnel_id,
            '  api_key: '+q('file:'+(runtime/'secrets/runtime.key').as_posix()),
            'mcp:','  commands:','    - channel: main','      command: '+q(command),
            'health:','  listen_addr: 127.0.0.1:0','  url_file: '+q((runtime/'state/service-health.url').as_posix()),
            'log:','  level: warn','  format: json',''])
        files=[('profile.yaml',profile)]
    else:
        profile=(root/'config/tunnel-profile.yaml.example').read_text(encoding='utf-8').replace('__TUNNEL_ID__',tunnel_id).replace('__ROOT__',str(root)).replace('__MCP_COMMAND__',shlex.join(args))
        service=(root/'config/dots-wechat-bridge.service.example').read_text(encoding='utf-8').replace('__ROOT__',str(root))
        files=[('profile.yaml',profile),('dots-wechat-bridge.service',service)]
    for name,text in files:
        flags=os.O_CREAT|os.O_WRONLY|(os.O_TRUNC if replace else os.O_EXCL)
        with os.fdopen(open_private(runtime/name,flags),'w',encoding='utf-8') as output: output.write(text)
    return runtime


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--tunnel-id',required=True)
    destination=parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--callback-host',action='append')
    destination.add_argument('--discover-callback',action='store_true',help='Deny all callbacks while discovering the authorized subscription hostname')
    parser.add_argument('--replace',action='store_true',help='Update only rendered config; preserves key and database')
    args=parser.parse_args()
    try:
        runtime=render(args.root,args.tunnel_id,args.callback_host or [],args.replace,args.discover_callback)
        print('Rendered private configuration in '+str(runtime))
    except (ValueError,OSError) as exc:
        parser.exit(1,type(exc).__name__+': config not generated; check inputs/file ownership\n')

if __name__=='__main__': main()
