"""Bound failures for the existing launcher; Windows owns all scheduling."""
import json
import os
import http.client
import datetime
import math
import time
from urllib.parse import urlsplit
from .platform_io import open_private

def read(path):
    try:
        with os.fdopen(open_private(path,os.O_RDONLY),'r',encoding='utf-8') as source:
            value=json.load(source)
        if type(value.get('failures')) is not int or type(value.get('retry_after')) not in (int,float): raise ValueError()
        return value
    except FileNotFoundError:return {'failures':0,'retry_after':0,'last_failure':0}

def save(path,value):
    temp=path.with_suffix('.pending.json')
    with os.fdopen(open_private(temp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY),'w',encoding='utf-8') as target:
        json.dump(value,target);target.flush();os.fsync(target.fileno())
    os.replace(temp,path)

def allowed(path,now):return now>=read(path)['retry_after']

def failed(path,now):
    previous=read(path)
    count=previous['failures']+1 if now-previous.get('last_failure',0)<1800 else 1
    value={'failures':count,'last_failure':now,'retry_after':now+1800 if count>=3 else 0}
    save(path,value)
    return value

def healthy(path):save(path,{'failures':0,'retry_after':0,'last_failure':0})


def local_readiness(path):
    """Bounded liveness/readiness probe; never follow a non-loopback address."""
    try:
        with os.fdopen(open_private(path,os.O_RDONLY),'r',encoding='utf-8') as source:
            raw=source.read(4097)
        if len(raw)>4096:return False
        url=urlsplit(raw.strip())
        if (url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port
                or url.username or url.password or url.path not in ('','/')
                or url.query or url.fragment):return False
        for route in ('/healthz','/readyz'):
            connection=http.client.HTTPConnection('127.0.0.1',url.port,timeout=2)
            try:
                connection.request('GET',route,headers={'Connection':'close'})
                if connection.getresponse().status!=200:return False
            finally:connection.close()
        return True
    except (OSError,ValueError,http.client.HTTPException):return False


class ReadinessRecovery:
    """An existing launcher's recovery gate, with no scheduler or background thread."""
    def __init__(self,started,*,startup_grace=120,failure_window=180):
        self.started=started;self.startup_grace=startup_grace
        self.failure_window=failure_window;self.failed_since=None;self.healthy_since=None

    def observe(self,now,ready):
        if ready:
            if self.healthy_since is None:self.healthy_since=now
        else:self.healthy_since=None
        if ready or now-self.started<self.startup_grace:
            self.failed_since=None
            return False
        if self.failed_since is None:self.failed_since=now
        return now-self.failed_since>=self.failure_window

    def stable(self,now,seconds=300):
        return self.healthy_since is not None and now-self.healthy_since>=seconds


# Fixed diagnostic vocabulary: never retain arbitrary server text, IDs or payloads.
COMPONENTS=('control-plane','response-delivery','queue','dispatcher')
STATES={'starting','polling','idle','backoff','backpressured','stopped','not_observed',
        'uploading','accepted','failed','canceled','available','full','accepting','busy',
        'completed','unknown','benign_not_found'}
CATEGORIES={'network_error','timeout','http_error','canceled','protocol_error',
            'invalid_response','authentication_error','rate_limited','unknown'}
REASONS={'control_plane_ok','control_plane_backpressure','control_plane_failures',
         'control_plane_stalled','control_plane_unobserved','diagnostics_unavailable',
         'local_health_unavailable'}
NUMERIC={'consecutive_failures','current_poll_age_seconds','configured_wait_seconds',
         'effective_wait_seconds','deadline_seconds','http_status','in_progress','attempts',
         'retries','accepted','completed','terminal_failures','depth','capacity','utilization',
         'enqueued','dequeued','backpressure_seconds','active','pool_limit','failures',
         'timeouts','oldest_active_age_seconds','last_success_age_seconds'}
TIMES={'last_attempt','last_success','last_error','next_retry','last_accepted',
       'last_completed','last_failure','last_enqueue','last_dequeue','last_start','last_completion'}


def _number(value):
    return type(value) in (int,float) and 0<=value<=1e15 and math.isfinite(value)


def _enum(value,allowed):
    return value if isinstance(value,str) and value in allowed else 'unknown'


def _timestamp(value):
    if not isinstance(value,str) or len(value)>40:return None
    try:
        parsed=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError,OverflowError,OSError):return None


def project_components(raw,now=None):
    """Keep only bounded enum/numeric metadata from official health components."""
    now=time.time() if now is None else now
    result={}
    if not isinstance(raw,dict):return result
    for name in COMPONENTS:
        item=raw.get(name)
        if not isinstance(item,dict):continue
        safe={'status':_enum(item.get('status'),('ok','degraded','unknown')),
              'state':_enum(item.get('state'),STATES),'details':{}}
        if isinstance(item.get('reason_code'),str) and item['reason_code'] in CATEGORIES:safe['reason_code']=item['reason_code']
        details=item.get('details')
        if isinstance(details,dict):
            for key in NUMERIC|{t+'_unix' for t in TIMES}:
                if _number(details.get(key)):safe['details'][key]=details[key]
            for key in TIMES:
                stamp=_timestamp(details.get(key))
                if stamp is not None and _number(stamp):safe['details'][key+'_unix']=stamp
            for key in ('failure_category','disposition'):
                allowed=CATEGORIES if key=='failure_category' else STATES
                if isinstance(details.get(key),str) and details[key] in allowed:safe['details'][key]=details[key]
            if type(details.get('accepting')) is bool:safe['details']['accepting']=details['accepting']
            stamp=safe['details'].get('last_success_unix')
            if stamp is not None:safe['details']['last_success_age_seconds']=round(max(0,now-stamp),3)
        result[name]=safe
    return result


def assess_control_plane(component):
    """An old response-delivery error or an idle dispatcher is not an outage."""
    if not isinstance(component,dict):return False,'control_plane_unobserved'
    state=component.get('state');details=component.get('details',{})
    if not isinstance(details,dict) or not _number(details.get('consecutive_failures')):
        return False,'control_plane_unobserved'
    if state=='backpressured':return True,'control_plane_backpressure'
    if details.get('consecutive_failures',0)>0:return False,'control_plane_failures'
    deadline=details.get('deadline_seconds',35)
    limit=max(120,2*deadline+15)
    if state=='polling':
        if not _number(details.get('current_poll_age_seconds')):return False,'control_plane_unobserved'
        return ((False,'control_plane_stalled') if details.get('current_poll_age_seconds',0)>limit
                else (True,'control_plane_ok'))
    if state=='idle' and details.get('last_success_age_seconds') is not None:
        return ((False,'control_plane_stalled') if details['last_success_age_seconds']>limit
                else (True,'control_plane_ok'))
    return False,'control_plane_unobserved'


def sample_health(path):
    """Only the current private loopback endpoint, with bounded body and timeouts."""
    sample={'ready':False,'healthy':False,'reason':'local_health_unavailable','components':{}}
    if not local_readiness(path):return sample
    sample['ready']=True;sample['reason']='diagnostics_unavailable'
    try:
        with os.fdopen(open_private(path,os.O_RDONLY),'r',encoding='utf-8') as source:raw=source.read(4097)
        if len(raw)>4096:return sample
        url=urlsplit(raw.strip())
        if (url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.username
                or url.password or url.path not in ('','/') or url.query or url.fragment):return sample
        connection=http.client.HTTPConnection('127.0.0.1',url.port,timeout=2)
        try:
            connection.request('GET','/health?details=true',headers={'Connection':'close'})
            response=connection.getresponse()
            if response.status!=200:return sample
            body=response.read(65537)
            if len(body)>65536:return sample
            value=json.loads(body)
            if not isinstance(value,dict):return sample
            sample['components']=project_components(value.get('components'))
            sample['healthy'],sample['reason']=assess_control_plane(sample['components'].get('control-plane'))
        finally:connection.close()
    except (OSError,ValueError,TypeError,http.client.HTTPException):pass
    return sample


def _recent_rpc(state):
    """Read only fixed original RPC metadata, never params/results/callback URLs."""
    records=[]
    try:
        with os.fdopen(open_private(state/'rpc-diagnostics.jsonl',os.O_RDONLY),'rb') as source:
            source.seek(0,os.SEEK_END);source.seek(max(0,source.tell()-32768))
            lines=source.read(32768).decode(errors='replace').splitlines()
    except OSError:return records
    for line in lines:
        try:item=json.loads(line)
        except ValueError:continue
        if not isinstance(item,dict) or item.get('method') not in (
                'server/discover','initialize','ping','events/list','events/subscribe',
                'events/unsubscribe','tools/list','tools/call'):continue
        safe={'method':item['method']}
        stamp=_timestamp(item.get('utc'))
        if stamp is not None:safe['utc_unix']=stamp
        if item.get('stage') in ('received','completed'):safe['stage']=item['stage']
        if type(item.get('success')) is bool:safe['success']=item['success']
        if type(item.get('error_code')) is int:safe['error_code']=item['error_code']
        records.append(safe)
    return records[-12:]


def save_health_sample(state,sample,pid,*,event=None):
    """Current status plus bounded transition/pre-stop evidence, all owner-only."""
    record={'time':time.time(),'client_pid':int(pid),'ready':sample.get('ready') is True,
            'healthy':sample.get('healthy') is True,
            'reason':sample.get('reason') if sample.get('reason') in REASONS else 'diagnostics_unavailable',
            'components':project_components(sample.get('components'))}
    path=state/'control-plane-status.json'
    temp=path.with_suffix('.pending.json')
    with os.fdopen(open_private(temp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY),'w',encoding='utf-8') as output:
        json.dump(record,output,allow_nan=False);output.flush();os.fsync(output.fileno())
    os.replace(temp,path)
    if event is None:return
    if event not in ('observed','changed','before_restart'):raise ValueError('Invalid diagnostic event')
    record.update(event=event,recent_rpc=_recent_rpc(state))
    path=state/'control-plane-diagnostics.jsonl'
    if path.exists() and path.stat().st_size>256*1024:
        os.replace(path,path.with_name('control-plane-diagnostics.previous.jsonl'))
    with os.fdopen(open_private(path,os.O_CREAT|os.O_APPEND|os.O_WRONLY),'a',encoding='utf-8') as output:
        output.write(json.dumps(record,allow_nan=False)+'\n');output.flush();os.fsync(output.fileno())
