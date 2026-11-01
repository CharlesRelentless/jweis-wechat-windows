#!/usr/bin/env python3
"""Read safe counts and bounded loopback health, without keys or message bodies."""
import argparse
import http.client
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from probe.platform_io import configure_stdio, open_private, safe_path
from probe.native_recovery import sample_health

POLL_STALE_AFTER_SECONDS = 180
POLL_ERRORS = frozenset({
    'auth_required', 'bot_auth_expired', 'poll_rejected', 'invalid_update_batch',
    'invalid_api_destination', 'invalid_api_json', 'invalid_api_response',
    'invalid_api_endpoint', 'non_public_api_address', 'api_response_too_large',
    'api_http_error', 'api_timeout', 'api_network_error', 'not_bound',
})


def read_private_text(path, limit=4096):
    with os.fdopen(open_private(path,os.O_RDONLY),'r',encoding='utf-8') as source:
        text=source.read(limit+1)
    if len(text)>limit: raise ValueError('Status metadata is too large')
    return text


def resolve_health_port(root):
    official = False
    try:
        if not (root.parent/'transport.json').exists(): raise FileNotFoundError()
        transport=json.loads(read_private_text(root.parent/'transport.json'))
        if transport.get('kind')!='official_tunnel': raise ValueError()
        official=True
    except FileNotFoundError:
        pass
    except (OSError,ValueError,AttributeError):
        return None,'invalid_transport_metadata'
    if official:
        try:
            p=urlsplit(read_private_text(root/'service-health.url').strip())
            if (p.scheme!='http' or p.hostname!='127.0.0.1' or not p.port or p.path not in ('','/')
                    or p.username or p.password or p.query or p.fragment): raise ValueError()
            return p.port,'official_tunnel'
        except (OSError,ValueError): return None,'invalid_official_health_file'
    # A malformed current endpoint must not fall back to a healthy old port.
    try:
        metadata=json.loads(read_private_text(root.parent/'server-url/endpoint.json'))
        port=metadata.get('local_port')
        if type(port) is not int or not 1<=port<=65535: raise ValueError()
        return port,'server_url_endpoint'
    except FileNotFoundError:
        pass
    except (OSError,ValueError,AttributeError):
        return None,'invalid_server_url_endpoint'
    try:
        p=urlsplit(read_private_text(root/'service-health.url').strip())
        if (p.scheme!='http' or p.hostname!='127.0.0.1' or not p.port or
                p.username or p.password or p.query or p.fragment): raise ValueError()
        return p.port,'legacy_health_file'
    except FileNotFoundError:
        return None,'not_configured'
    except (OSError,ValueError):
        return None,'invalid_legacy_health_file'


def probe_health(port):
    result={}
    for endpoint in ('/healthz','/readyz'):
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
        try:
            conn.request('GET',endpoint)
            result[endpoint]=conn.getresponse().status
        finally: conn.close()
    return result


def build_status(state_dir, *, now=None, health_probe=probe_health, control_probe=sample_health):
    root=safe_path(state_dir)
    now=time.time() if now is None else now
    database=safe_path(root/'probe.sqlite')
    result={'account_status':'not_initialized','active_subscriptions':{},'send_counts':{}}
    last_poll=None
    if database.exists():
        db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=3)
        try:
            account=db.execute('SELECT status,last_poll,last_error FROM weixin_accounts LIMIT 1').fetchone()
            last_poll=account[1] if account else None
            error=account[2] if account else None
            result.update({
                'account_status':account[0] if account else 'not_bound',
                'last_error':error if error is None or error in POLL_ERRORS else 'recorded_error',
                'send_counts':dict(db.execute('SELECT status,count(*) FROM weixin_outbox GROUP BY status').fetchall()),
                'active_subscriptions':dict(db.execute('SELECT name,count(*) FROM subscriptions WHERE expires>? GROUP BY name',(now,)).fetchall()),
            })
        finally: db.close()
    valid_poll=type(last_poll) in (int,float) and math.isfinite(last_poll)
    age=now-last_poll if valid_poll else None
    result['poll_age_seconds']=round(age,1) if age is not None else None
    ready=result['account_status']=='ready'
    result['poll_health']=('PASS' if age is not None and -5<=age<=POLL_STALE_AFTER_SECONDS
                           else 'FAIL' if ready or result['account_status']=='auth_required' else 'WARN')
    result['poll_stale_after_seconds']=POLL_STALE_AFTER_SECONDS
    result['subscription_health']=('PASS' if result['active_subscriptions'].get('weixin.message',0)==1
                                   else 'FAIL' if ready or result['account_status']=='auth_required' else 'WARN')
    port,source=resolve_health_port(root)
    result['health_source']=source
    result['health']='unavailable'
    if port is not None:
        try: result['health']=health_probe(port)
        except (OSError,http.client.HTTPException,ValueError): pass
    result['gateway_health']=('PASS' if result['health']=={'/healthz':200,'/readyz':200}
                              else 'WARN' if source=='not_configured' else 'FAIL')
    checks=[result[x] for x in ('gateway_health','poll_health','subscription_health')]
    if source=='official_tunnel':
        sample=control_probe(root/'service-health.url')
        result['control_plane_health']='PASS' if sample['healthy'] else 'FAIL'
        result['control_plane_reason']=sample['reason']
        checks.append(result['control_plane_health'])
    result['runtime_status']=('FAIL' if 'FAIL' in checks or result['account_status']=='auth_required'
                              else 'WARN' if 'WARN' in checks or not ready else 'PASS')
    try:
        metrics=json.loads(read_private_text(root/'windows-job-summary.json'))
        fields=('memory_limit_bytes','peak_job_memory_used_bytes','process_limit',
                'processor_affinity_mask','active_processes','total_processes','job_terminated_processes','oom_count')
        result['windows_job']={key:metrics[key] for key in fields if key in metrics and (metrics[key] is None or type(metrics[key]) is int)}
    except (OSError,ValueError,TypeError): pass
    return result


def main():
    configure_stdio()
    parser=argparse.ArgumentParser()
    parser.add_argument('--state-dir',required=True)
    args=parser.parse_args()
    print(json.dumps(build_status(args.state_dir)))


if __name__=='__main__': main()
