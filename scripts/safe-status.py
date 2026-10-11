#!/usr/bin/env python3
"""Read safe counts and bounded loopback health, without keys or message bodies."""
import argparse
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from probe.platform_io import configure_stdio, open_private, safe_path

configure_stdio()
parser=argparse.ArgumentParser(); parser.add_argument('--state-dir',required=True); args=parser.parse_args()
root=safe_path(args.state_dir)
database=safe_path(root/'probe.sqlite')
result={'account_status':'not_initialized','active_subscriptions':{},'send_counts':{}}
if database.exists():
    db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
    try:
        account=db.execute('SELECT status,last_poll,last_error FROM weixin_accounts LIMIT 1').fetchone()
        result={'account_status':account[0] if account else 'not_bound','last_error':account[2] if account else None,
            'poll_age_seconds':round(time.time()-account[1],1) if account and account[1] else None,
            'send_counts':dict(db.execute('SELECT status,count(*) FROM weixin_outbox GROUP BY status').fetchall()),
            'active_subscriptions':dict(db.execute('SELECT name,count(*) FROM subscriptions WHERE expires>? GROUP BY name',(time.time(),)).fetchall())}
    finally: db.close()
try:
    with os.fdopen(open_private(root/'service-health.url',os.O_RDONLY),'r',encoding='utf-8') as source:
        p=urlsplit(source.read(4096).strip())
    if p.scheme!='http' or p.hostname!='127.0.0.1' or not p.port: raise ValueError()
    result['health']={}
    for endpoint in ('/healthz','/readyz'):
        conn=http.client.HTTPConnection(p.hostname,p.port,timeout=2)
        try:
            conn.request('GET',endpoint); response=conn.getresponse(); result['health'][endpoint]=response.status
        finally: conn.close()
except Exception: result['health']='unavailable'
try:
    with os.fdopen(open_private(root/'windows-job-summary.json',os.O_RDONLY),'r',encoding='utf-8') as source:
        metrics=json.loads(source.read(4096))
    fields=('memory_limit_bytes','peak_job_memory_used_bytes','process_limit',
            'processor_affinity_mask','active_processes','total_processes','job_terminated_processes','oom_count')
    result['windows_job']={key:metrics[key] for key in fields if key in metrics and (metrics[key] is None or type(metrics[key]) is int)}
except (OSError,ValueError): pass
print(json.dumps(result))
