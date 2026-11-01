"""Read-only runtime health decisions and bounded, private change tracking.

This module never restarts a gateway, approves OAuth, sends messages or changes
subscriptions. In particular, an ephemeral endpoint must not be auto-restarted.
"""
import json
import os
from pathlib import Path
from .platform_io import open_private, private_dir


def assess(status, endpoint, *, expected_origin, lease_expires=None, now):
    failures = []
    for field, reason in [('gateway_health', 'gateway_unavailable'),
                          ('poll_health', 'poll_stale'),
                          ('subscription_health', 'subscription_count_invalid')]:
        if status.get(field) != 'PASS': failures.append(reason)
    if status.get('account_status') == 'auth_required': failures.append('wechat_auth_required')
    origin = endpoint.get('origin') if isinstance(endpoint, dict) else None
    if not origin or origin != expected_origin: failures.append('endpoint_changed_or_invalid')
    # A disconnected socket is an availability failure, not proof of anonymous access.
    if status.get('anonymous_mcp_status') not in (None, 0, 401): failures.append('anonymous_mcp_not_denied')
    if status.get('public_health') not in (None, 'PASS'): failures.append('public_ingress_unavailable')
    warnings = []
    if lease_expires is not None and 0 < lease_expires-now < 900:
        warnings.append('subscription_expires_within_15_minutes')
    durable = bool(isinstance(endpoint, dict) and endpoint.get('temporary') is False
                   and endpoint.get('persistent_oauth') is True and origin == expected_origin)
    limitations = [] if durable else ['automatic_restart_disabled_until_fixed_origin_and_persistent_oauth']
    return {'service_status': 'FAIL' if failures else 'WARN' if warnings else 'PASS',
            'reasons': sorted(set(failures+warnings)), 'restart_recovery': 'READY' if durable else 'BLOCKED',
            'limitations': limitations, 'poll_age_seconds': status.get('poll_age_seconds'),
            'subscription_count': status.get('active_subscriptions', {}).get('weixin.message', 0),
            'lease_remaining_seconds': round(lease_expires-now) if lease_expires is not None else None,
            'public_http_status': status.get('public_http_status'),
            'checked_at_unix': now, 'authenticated_mcp_probe': 'not_performed_no_credentials_used'}


def transition(report, previous, *, failure_threshold=2):
    """Notify after two failed samples, promptly on auth/address changes; once on recovery."""
    previous = previous if isinstance(previous, dict) else {}
    signature = [report['service_status'], report['reasons']]
    same = signature == previous.get('signature')
    failures = (previous.get('consecutive_failures', 0)+1 if same else 1) if report['service_status']=='FAIL' else 0
    urgent = bool(set(report['reasons']) & {'wechat_auth_required', 'endpoint_changed_or_invalid',
                                           'anonymous_mcp_not_denied'})
    actionable = report['service_status']=='WARN' or (report['service_status']=='FAIL' and (urgent or failures>=failure_threshold))
    alert_key = signature if actionable else previous.get('alert_key')
    recovered = report['service_status']=='PASS' and previous.get('alert_key') is not None
    notify = (actionable and signature != previous.get('alert_key')) or recovered
    if recovered: alert_key = None
    return dict(report, signature=signature, consecutive_failures=failures,
                alert_key=alert_key, notify=notify,
                notification_kind='recovered' if recovered else 'failure' if notify else 'none')


def read_json(path, limit=65536):
    with os.fdopen(open_private(path, os.O_RDONLY), 'r', encoding='utf-8') as source:
        data=source.read(limit+1)
    if len(data)>limit: raise ValueError('Bounded metadata exceeded')
    value=json.loads(data)
    if not isinstance(value, dict): raise ValueError('Object metadata required')
    return value


def save_report(directory, report, *, record_transition=True):
    directory=private_dir(directory)
    path=directory/'status.json'
    temp=directory/'status.pending.json'
    with os.fdopen(open_private(temp, os.O_CREAT|os.O_TRUNC|os.O_WRONLY), 'w', encoding='utf-8') as target:
        json.dump(report,target,ensure_ascii=False,allow_nan=False,indent=2)
        target.flush(); os.fsync(target.fileno())
    os.replace(temp,path)
    if report['notify'] and record_transition:
        history=directory/'transitions.jsonl'
        # Keep only bounded, non-sensitive state changes. No messages, URLs or IDs.
        if history.exists() and history.stat().st_size>256*1024:
            rotated=directory/'transitions.previous.jsonl'
            os.replace(history,rotated)
        event={k:report[k] for k in ('checked_at_unix','service_status','reasons','notification_kind')}
        with os.fdopen(open_private(history,os.O_CREAT|os.O_APPEND|os.O_WRONLY),'a',encoding='utf-8') as target:
            target.write(json.dumps(event,ensure_ascii=False)+'\n')
