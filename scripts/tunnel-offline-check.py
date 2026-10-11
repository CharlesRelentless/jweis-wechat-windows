#!/usr/bin/env python3
"""Real official Windows client -> real bridge; local synthetic control plane only."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from probe.platform_io import private_dir, open_private, is_private, acquire_worker_lock
from probe.windows_job import WindowsJob
from probe.client_integrity import verify_bundle
SESSION_HEADERS={}


def read_json(body, stage):
    try: return json.loads(body)
    except ValueError:
        raise RuntimeError(f'Offline {stage} returned invalid JSON') from None


def wait_connection_info(path, process, timeout=17):
    # A URL file can be observed during publication. Existence alone is not
    # readiness; wait for a complete JSON document without retrying MCP calls.
    deadline=time.monotonic()+timeout
    while True:
        if process.poll() is not None:
            raise RuntimeError('Official local proxy exited before readiness')
        try:
            info=json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            if time.monotonic()>=deadline:
                raise TimeoutError('Official local proxy connection-info publication timeout') from None
            time.sleep(.05)
            continue
        for field in ('mcp_url','health_url'):
            value=info.get(field) if isinstance(info,dict) else None
            if not isinstance(value,str) or not value.startswith('http://127.0.0.1:'):
                raise ValueError('Offline connection info requires loopback URLs')
        return info


@contextlib.contextmanager
def temporary_fixture():
    temp=tempfile.TemporaryDirectory(prefix='dots-native-client-')
    try: yield temp.name
    finally:
        # Windows can release a child's image mapping just after it releases
        # the worker lock. Bound the wait before deleting this test-owned copy.
        deadline=time.monotonic()+8
        while True:
            try: temp.cleanup(); break
            except PermissionError:
                if time.monotonic()>deadline: raise
                time.sleep(.1)


def local_request(url, payload=None):
    if not url.startswith('http://127.0.0.1:'): raise ValueError('Offline check requires loopback')
    headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream',
             'MCP-Protocol-Version':'2026-07-28'}
    if payload: headers.update(SESSION_HEADERS)
    request=urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None, headers=headers)
    # Explicitly bypass proxy variables for this loopback-only check.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try: response=opener.open(request,timeout=8)
    except TimeoutError:
        method=payload.get('method') if payload else urlsplit(url).path
        raise TimeoutError(f'Offline {method} request timed out') from None
    except urllib.error.HTTPError as error:
        # This process uses only synthetic fixtures and an official loopback
        # proxy. Project just the JSON error message, never request headers.
        body=error.read(2048)
        try:
            detail=json.loads(body)
            message=str(detail.get('error',detail))[:180]
        except ValueError: message='non-JSON local error'
        method=payload.get('method') if payload else urlsplit(url).path
        raise RuntimeError(f'Offline {method} HTTP {error.code}: {message}') from None
    with response:
        if payload and response.headers.get('MCP-Session-Id'):
            SESSION_HEADERS['MCP-Session-Id']=response.headers['MCP-Session-Id']
        body=response.read(2*1024*1024)
        if payload:
            if not body: return response.status,None
            if body.startswith(b'data:'): body=body.split(b'data:',1)[1].strip()
            return response.status,read_json(body,payload.get('method','notification'))
        return response.status,None


def run_check():
    client=verify_bundle(ROOT)
    spec=importlib.util.spec_from_file_location('render_config',ROOT/'scripts/render-config.py')
    renderer=importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)
    summary={'scope':'official local dev proxy; no hosted tunnel or WeChat account',
             'client_version':subprocess.check_output([str(client),'--version'],text=True).strip()}
    with temporary_fixture() as temp:
        root=private_dir(Path(temp)/"测试 空格 & $ '目录")
        # Build a real disposable environment from the invoking interpreter.
        # CI and clean source exports need not have ROOT/.venv installed.
        subprocess.run([sys.executable,'-m','venv','--without-pip',str(root/'.venv')],
                       check=True,capture_output=True,timeout=30)
        deps=private_dir(root/'.venv/Lib/site-packages')
        (deps/'offline-dependencies.pth').write_text(sysconfig.get_path('purelib')+'\n',encoding='utf-8')
        shutil.copytree(ROOT/'probe',root/'probe',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        runtime=renderer.render(root,'tunnel_'+'2'*32,[],discover_callback=True)
        with os.fdopen(open_private(runtime/'secrets/runtime.key',os.O_WRONLY|os.O_CREAT|os.O_EXCL),
                       'w',encoding='utf-8') as output:
            output.write('offline-synthetic-key-not-an-account-credential\n')
        # Limit only this check's descendants. This script itself must run as a
        # separate process because a kill-on-close Job includes the launcher.
        job=WindowsJob()
        env=dict(os.environ)
        for name in list(env):
            if name.startswith(('CONTROL_PLANE_','MCP_','TUNNEL_','TUNNEL_CLIENT_','HARPOON_','CLOUDFLARED_','HEALTH_','LOG_','ADMIN_UI_','PROXY_')) or name in ('OPENAI_API_KEY','OPENAI_ADMIN_KEY'):
                del env[name]
        doctor=subprocess.run([str(client),'doctor','--profile-file',str(runtime/'profile.yaml'),'--json'],
                              env=env,cwd=root,text=True,capture_output=True,timeout=15)
        report=read_json(doctor.stdout,'doctor')
        summary['doctor_result']=report['result']
        summary['doctor_checks']={x['id']:x['status'] for x in report['checks']}
        if doctor.returncode or report['result']!='ok': raise RuntimeError('Offline doctor failed')
        info_file=runtime/'state/local-test-connection.json'
        log=runtime/'state/local-test.log'
        with os.fdopen(open_private(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL),'wb') as output:
            process=subprocess.Popen([str(client),'dev','proxy','--backend','go',
                '--profile-file',str(runtime/'profile.yaml'),'--url-file',str(info_file),
                '--health-url-file',str(runtime/'state/service-health.url'),
                '--readiness-timeout','12s','--duration','25s'],
                env=env,cwd=root,stdout=output,stderr=output)
            try:
                info=wait_connection_info(info_file,process)
                parts=urlsplit(info['health_url'])
                health_origin=parts.scheme+'://'+parts.netloc
                summary['health']={route:local_request(health_origin+route)[0]
                                   for route in ('/healthz','/readyz')}
                def rpc(method,params=None):
                    status,value=local_request(info['mcp_url'],{'jsonrpc':'2.0','id':1,
                        'method':method,'params':params or {}})
                    if status!=200 or 'error' in value: raise RuntimeError('Local MCP forwarding failed: '+method)
                    return value['result']
                discovery=rpc('server/discover')
                summary['protocol_versions']=discovery['supportedVersions']
                summary['events_capability']='events' in discovery['capabilities']
                initialized=rpc('initialize',{'protocolVersion':'2026-07-28',
                    'clientInfo':{'name':'native-offline-check','version':'1'},'capabilities':{}})
                summary['initialized_protocol']=initialized['protocolVersion']
                notify_code,_=local_request(info['mcp_url'],{'jsonrpc':'2.0','method':'notifications/initialized'})
                summary['initialized_notification_status']=notify_code
                tools=rpc('tools/list')['tools']
                summary['tool_count']=len(tools)
                summary['message_event']=any(x['name']=='weixin.message' for x in rpc('events/list')['events'])
                status=rpc('tools/call',{'name':'get_weixin_status','arguments':{}})
                summary['actual_status_tool_call']='structuredContent' in status and not status.get('isError',False)
                summary['private_profile']=is_private(runtime/'profile.yaml')
                summary['private_database']=is_private(runtime/'state/probe.sqlite')
                summary['windows_job']=job.metrics()
                if summary['health']!={'/healthz':200,'/readyz':200}: raise RuntimeError('Local health not ready')
                if not summary['actual_status_tool_call'] or not summary['message_event']: raise RuntimeError('Missing real MCP capability')
            finally:
                if process.poll() is None: process.terminate()
                process.wait(timeout=8)
                deadline=time.monotonic()+8
                while True:
                    try: lock=acquire_worker_lock(runtime/'state'); lock.close(); break
                    except BlockingIOError:
                        if time.monotonic()>deadline: raise RuntimeError('Offline worker did not exit')
                        time.sleep(.1)
    return summary


if __name__=='__main__':
    try:
        summary=run_check()
        (ROOT/'docs/tunnel-offline-verification.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False))
    except Exception as error:
        # Controlled categories only: never emit complete runtime logs.
        print(json.dumps({'result':'FAIL','error_class':type(error).__name__,'reason':str(error)[:180]}))
        sys.exit(1)
