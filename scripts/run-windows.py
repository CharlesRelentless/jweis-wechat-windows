#!/usr/bin/env python3
"""Run the official tunnel and descendants in a bounded Windows Job Object."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.platform_io import configure_stdio, private_dir, is_private, open_private, safe_path
from probe.windows_job import WindowsJob
from probe.client_integrity import verify_bundle


def main():
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile-file', default=str(ROOT/'runtime/profile.yaml'))
    args = parser.parse_args()
    profile = safe_path(args.profile_file)
    key = ROOT/'runtime/secrets/runtime.key'
    for path in (profile, key):
        if not path.is_file() or not is_private(path):
            parser.exit(1, 'Private configuration and owner-entered runtime key are required.\n')
    try: client = verify_bundle(ROOT)
    except (OSError,ValueError): parser.exit(1, 'Verified official bundle integrity is required; client was not started.\n')
    state = private_dir(ROOT/'runtime/state')
    try:
        job = WindowsJob()
    except OSError:
        parser.exit(1, 'Cannot apply Windows process-tree resource limits; client was not started.\n')
    # Job assignment precedes child creation: no unbounded startup interval.
    process = subprocess.Popen([str(client), 'run', '--profile-file', str(profile)], cwd=ROOT)
    try:
        return_code = process.wait()
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
    return return_code


if __name__ == '__main__': sys.exit(main())
