#!/usr/bin/env python3
"""One bounded runtime check. Scheduling belongs to Windows Task Scheduler."""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from probe.platform_io import configure_stdio, acquire_worker_lock, private_dir, open_private
from probe.runtime_health import assess, transition, read_json, save_report
from probe.tunnel_runtime_env import activation_policy


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def http_status(url):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(url,timeout=4) as response: return response.status
    except urllib.error.HTTPError as error: return error.code
    except (OSError,ValueError): return 0


def collect(root, expected_origin, *, public_check=False, official_tunnel=None):
    spec=importlib.util.spec_from_file_location('live_safe_status',root/'scripts/safe-status.py')
    reader=importlib.util.module_from_spec(spec); spec.loader.exec_module(reader)
    status=reader.build_status(root/'runtime/state')
    if official_tunnel is not None:
        transport=read_json(root/'runtime/transport.json')
        policy=activation_policy(root,root/'runtime/profile.yaml')
        if not re.fullmatch('tunnel_[a-f0-9]{32}',official_tunnel): raise ValueError('Invalid tunnel pin')
        db=sqlite3.connect((root/'runtime/state/probe.sqlite').as_uri()+'?mode=ro',uri=True,timeout=3)
        try: lease=db.execute("select min(expires) from subscriptions where name='weixin.message' and expires>?",(time.time(),)).fetchone()[0]
        finally: db.close()
        reasons=[reason for field,reason in (('gateway_health','gateway_unavailable'),('poll_health','poll_stale'),
                  ('subscription_health','subscription_count_invalid'),
                  ('control_plane_health','control_plane_unhealthy')) if status.get(field)!='PASS']
        if transport.get('tunnel_id')!=official_tunnel or policy.get('tunnel_id')!=official_tunnel:
            reasons.append('endpoint_changed_or_invalid')
        if status.get('account_status')=='auth_required': reasons.append('wechat_auth_required')
        now=time.time()
        if lease is not None and 0<lease-now<900: reasons.append('subscription_expires_within_15_minutes')
        warning=reasons==['subscription_expires_within_15_minutes']
        return {'service_status':'WARN' if warning else 'FAIL' if reasons else 'PASS',
                'reasons':sorted(set(reasons)),'restart_recovery':'READY',
                'limitations':['owner_login_and_existing_proxy_required','billing_dashboard_may_be_delayed'],
                'transport':'official_tunnel','poll_age_seconds':status.get('poll_age_seconds'),
                'control_plane_health':status.get('control_plane_health'),
                'control_plane_reason':status.get('control_plane_reason'),
                'subscription_count':status.get('active_subscriptions',{}).get('weixin.message',0),
                'lease_remaining_seconds':round(lease-now) if lease else None,'checked_at_unix':now,
                'authenticated_mcp_probe':'not_performed_no_credentials_used'}
    endpoint=read_json(root/'runtime/server-url/endpoint.json')
    port=endpoint.get('local_port')
    if type(port) is int and 0<port<65536:
        status['anonymous_mcp_status']=http_status('http://127.0.0.1:'+str(port)+'/mcp')
    db=sqlite3.connect((root/'runtime/state/probe.sqlite').as_uri()+'?mode=ro',uri=True,timeout=3)
    try:
        lease=db.execute("select min(expires) from subscriptions where name='weixin.message' and expires>?",(time.time(),)).fetchone()[0]
    finally: db.close()
    parsed=urlsplit(expected_origin)
    if public_check:
        if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port or
                parsed.path or parsed.query or parsed.fragment or not parsed.hostname):
            raise ValueError('Invalid pinned origin')
        codes={path:http_status(expected_origin+path) for path in ('/healthz','/readyz')}
        status['public_http_status']=codes
        status['public_health']='PASS' if all(code==200 for code in codes.values()) else 'FAIL'
    return assess(status,endpoint,expected_origin=expected_origin,lease_expires=lease,now=time.time())


def main():
    configure_stdio()
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--public-check',action='store_true')
    parser.add_argument('--notify',action='store_true')
    parser.add_argument('--initialize',action='store_true',help='Pin the currently approved endpoint without replacing an existing pin')
    args=parser.parse_args()
    root=args.root.resolve(); directory=private_dir(root/'runtime/monitor')
    with acquire_worker_lock(directory):
        if args.initialize:
            try: transport=read_json(root/'runtime/transport.json')
            except FileNotFoundError: transport={}
            if transport:
                if transport.get('kind')!='official_tunnel':raise ValueError('Unknown runtime transport')
                activation=activation_policy(root,root/'runtime/profile.yaml')
                tunnel=transport.get('tunnel_id')
                if (not isinstance(tunnel,str) or not re.fullmatch('tunnel_[a-f0-9]{32}',tunnel)
                        or not activation or activation.get('tunnel_id')!=tunnel):
                    raise ValueError('Authorized official tunnel identity required')
                desired={'version':1,'transport':'official_tunnel','expected_tunnel_id':tunnel}
                fields=('transport','expected_tunnel_id')
            else:
                endpoint=read_json(root/'runtime/server-url/endpoint.json')
                origin=endpoint['origin']; parsed=urlsplit(origin)
                if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                    raise ValueError('Canonical endpoint required')
                desired={'version':1,'expected_origin':origin}
                fields=('expected_origin',)
            policy=directory/'policy.json'
            if policy.exists():
                existing=read_json(policy)
                if any(existing.get(key)!=desired[key] for key in fields):
                    raise ValueError('Existing endpoint pin differs; complete an explicit ingress migration first')
            else:
                with os.fdopen(open_private(policy,os.O_CREAT|os.O_EXCL|os.O_WRONLY),'w',encoding='utf-8') as target:
                    json.dump(desired,target)
            print(json.dumps({'policy_initialized':True})); return 0
        try:
            pinned=read_json(directory/'policy.json')
            if pinned.get('transport')=='official_tunnel':
                report=collect(root,None,official_tunnel=pinned['expected_tunnel_id'])
            else:
                report=collect(root,pinned['expected_origin'],public_check=args.public_check)
        except (OSError,ValueError,TypeError,AttributeError,KeyError,sqlite3.Error):
            report={'service_status':'FAIL','reasons':['status_collection_failed'],'restart_recovery':'BLOCKED',
                    'limitations':['no_automatic_restart'],'checked_at_unix':time.time()}
        try: previous=read_json(directory/'status.json')
        except (OSError,ValueError): previous={}
        report=transition(report,previous)
        save_report(directory,report)
        if args.notify and report['notify'] and sys.platform=='win32':
            try:
                shown=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden',
                                      '-File',str(root/'scripts/show-runtime-alert.ps1'),'-ReportPath',str(directory/'status.json')],
                                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
                if shown.returncode: raise OSError('Desktop notification failed')
                report['notification_delivery']='desktop_popup_requested'
            except (OSError,subprocess.TimeoutExpired):
                report['notification_delivery']='failed'
                report['alert_key']=previous.get('alert_key')
            save_report(directory,report,record_transition=False)
        print(json.dumps(report,ensure_ascii=False))
        return 1 if report['service_status']=='FAIL' else 0


if __name__=='__main__': sys.exit(main())
