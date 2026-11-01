#!/usr/bin/env python3
"""Run the official tunnel and descendants in a bounded Windows Job Object."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.platform_io import configure_stdio, private_dir, is_private, open_private, safe_path
from probe.windows_job import WindowsJob
from probe.client_integrity import verify_bundle
from probe.tunnel_runtime_env import official_environment, activation_policy
from probe import native_recovery


def lifecycle(state, event, **values):
    # Only launcher metadata, never configuration, credentials or client output.
    path = state/'tunnel-lifecycle.jsonl'
    if path.exists() and path.stat().st_size > 256*1024:
        os.replace(path, path.with_suffix('.previous.jsonl'))
    with os.fdopen(open_private(path, os.O_CREAT|os.O_APPEND|os.O_WRONLY),
                   'a', encoding='utf-8') as output:
        output.write(json.dumps({'time':time.time(),'event':event,**values})+'\n')


def start_client(command, *, cwd, env, log_path):
    # pythonw has no OS console handles. Assigning sys.stderr alone does not
    # redirect a child's inherited Win32 handles.
    with os.fdopen(open_private(log_path, os.O_CREAT|os.O_APPEND|os.O_WRONLY),
                   'a', encoding='utf-8') as output:
        return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def main():
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile-file', default=str(ROOT/'runtime/profile.yaml'))
    args = parser.parse_args()
    state = private_dir(ROOT/'runtime/state')
    lifecycle(state,'launcher_started',pid=os.getpid())
    recovery = state/'native-recovery.json'
    try:
        if not native_recovery.allowed(recovery,time.time()): return 0
    except (OSError,ValueError,TypeError,KeyError):
        parser.exit(1,'Native recovery metadata needs inspection.\n')
    profile = safe_path(args.profile_file)
    key = ROOT/'runtime/secrets/runtime.key'
    if not profile.is_file() or not is_private(profile):
        parser.exit(1, 'Private configuration is required.\n')
    try:
        child_env = official_environment(ROOT, profile)
    except (OSError, ValueError):
        parser.exit(1, 'Official activation blocked; check owner authorization, charge latch and private credential file.\n')
    if child_env is None and (not key.is_file() or not is_private(key)):
        parser.exit(1, 'Private owner-entered runtime key is required.\n')
    lifecycle(state,'activation_checked')
    try: client = verify_bundle(ROOT)
    except (OSError,ValueError): parser.exit(1, 'Verified official bundle integrity is required; client was not started.\n')
    lifecycle(state,'bundle_verified')
    try:
        job = WindowsJob()
    except OSError:
        parser.exit(1, 'Cannot apply Windows process-tree resource limits; client was not started.\n')
    # Job assignment precedes child creation: no unbounded startup interval.
    command = [str(client), 'run', '--profile-file', str(profile)]
    if child_env is not None:
        proxy = activation_policy(ROOT, profile).get('proxy_url')
        if proxy: command.extend(['--http-proxy', proxy])
    process = start_client(command,cwd=ROOT,env=child_env,
                           log_path=ROOT/'runtime/official-client.log')
    lifecycle(state,'client_started',pid=process.pid)
    started=time.monotonic(); recovery_reset=False
    readiness=native_recovery.ReadinessRecovery(started)
    next_health=started+15; previous_health=None
    try:
        while True:
            try:
                return_code = process.wait(timeout=5)
                lifecycle(state,'client_exited',return_code=return_code,
                          duration_seconds=round(time.monotonic()-started,1))
                break
            except subprocess.TimeoutExpired:
                if child_env is not None:
                    try: activation_policy(ROOT, profile)
                    except (OSError, ValueError):
                        process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                        return_code = 0  # A latched owner stop must not cause restart loops.
                        break
                    now=time.monotonic()
                    if now>=next_health:
                        sample=native_recovery.sample_health(state/'service-health.url')
                        ready=sample['healthy']
                        next_health=now+15
                        signature=(ready,sample['reason'])
                        try:
                            event='observed' if previous_health is None else 'changed' if signature!=previous_health else None
                            native_recovery.save_health_sample(state,sample,process.pid,event=event)
                            previous_health=signature
                        except (OSError,ValueError):
                            # Keep the current child if evidence cannot be persisted.
                            # Raising here would tear down its Windows Job without evidence.
                            continue
                        if readiness.observe(now,ready):
                            try:
                                native_recovery.save_health_sample(state,sample,process.pid,event='before_restart')
                                lifecycle(state,'readiness_recovery_requested',reason=sample['reason'])
                                # An owner/charge stop always wins, including one set
                                # while the diagnostic was being captured.
                                activation_policy(ROOT,profile)
                            except (OSError,ValueError):continue
                            process.terminate()
                            try:stopped_code=process.wait(timeout=5)
                            except subprocess.TimeoutExpired:process.kill();stopped_code=process.wait(timeout=5)
                            lifecycle(state,'recovery_client_stopped',return_code=stopped_code,
                                      reason=sample['reason'])
                            return_code=1
                            break
                        if ready and not recovery_reset and readiness.stable(now):
                            native_recovery.healthy(recovery);recovery_reset=True
    except KeyboardInterrupt:
        # All console processes receive Ctrl-C. Give the official client time to
        # stop and close its worker, then terminate this owned process if needed.
        try: return_code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate(); return_code = process.wait(timeout=5)
    finally:
        summary = job.metrics()
        with os.fdopen(open_private(state/'windows-job-summary.json',
                                    os.O_CREAT | os.O_WRONLY | os.O_TRUNC), 'w', encoding='utf-8') as output:
            json.dump(summary, output, indent=2); output.write('\n')
    # Task Scheduler interprets Win32 termination values (for example -1) as
    # scheduler status codes. Report a normal process failure for native retries.
    if return_code != 0: native_recovery.failed(recovery,time.time())
    return 0 if return_code == 0 else 1


if __name__ == '__main__': sys.exit(main())
