#!/usr/bin/env python3
"""Project-owned offline checks; never open a real runtime or account."""
import datetime
import json
from pathlib import Path
import platform
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    commands={
        'unit_tests':['-m','unittest','discover','-s','probe_tests'],
        'schema':['scripts/generate-schema.py','--check'],
        'dependencies':['-m','pip','check'],
        'media_sample':['scripts/media-resource-check.py'],
        'outbound_sample':['scripts/outbound-resource-check.py']}
    report={'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'python':platform.python_version(),'system':platform.system(),'machine':platform.machine(),
            'scope':'offline synthetic tests including the official local dev proxy',
            'hosted_tunnel':False,'weixin_end_to_end':False,'checks':{}}
    for name,arguments in commands.items():
        result=subprocess.run([sys.executable,*arguments],cwd=ROOT,text=True,encoding='utf-8',
                              capture_output=True,timeout=90)
        output=(result.stdout+result.stderr).strip()
        item={'status':'PASS' if result.returncode==0 else 'FAIL','exit_code':result.returncode,
              'command':'python '+' '.join(arguments)}
        if name=='unit_tests':
            count=re.search(r'Ran (\d+) tests',output)
            skipped=re.search(r'skipped=(\d+)',output)
            item.update(tests=int(count[1]) if count else None,skipped=int(skipped[1]) if skipped else 0,
                        summary=output[-600:])
        elif name in ('media_sample','outbound_sample') and result.returncode==0:
            item['measurement']=json.loads(result.stdout)
        else: item['summary']=output[-600:]
        report['checks'][name]=item
        print(name,item['status'],flush=True)
        if result.returncode: break
    target=ROOT/'docs/windows-verification.json'
    target.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return 0 if all(x['status']=='PASS' for x in report['checks'].values()) and len(report['checks'])==len(commands) else 1


if __name__=='__main__': sys.exit(main())
