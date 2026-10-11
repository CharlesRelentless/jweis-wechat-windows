#!/usr/bin/env python3
"""Human-run hidden TTY entry. No secrets in CLI arguments, stdout or chat."""
import argparse
import getpass
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from probe.platform_io import private_dir, private_file, safe_path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--file',default=str(Path(__file__).resolve().parents[1]/'runtime/secrets/runtime.key'))
    parser.add_argument('--replace',action='store_true')
    args=parser.parse_args()
    target=safe_path(args.file)
    if target.is_symlink(): parser.exit(1,'Key target must be a regular private file, not a symlink.\n')
    if not sys.stdin.isatty(): parser.exit(1,'Run personally in an interactive terminal; stdin automation is disabled.\n')
    if target.exists() and not args.replace: parser.exit(1,'Key already exists; explicit --replace required.\n')
    private_dir(target.parent)
    key=getpass.getpass('Runtime API key (hidden; entered by owner): ').strip()
    if not key or any(c.isspace() for c in key): parser.exit(1,'Invalid key input.\n')
    fd,temp=tempfile.mkstemp(prefix='.pending-key-',dir=target.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            output.write(key+'\n'); output.flush(); os.fsync(output.fileno())
        private_file(temp)
        owner=target.stat() if target.exists() else target.parent.stat()
        if hasattr(os,"geteuid") and os.geteuid()==0: os.chown(temp,owner.st_uid,owner.st_gid)
        os.replace(temp,target)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    print('Stored private key reference. No key value was printed.')

if __name__=='__main__': main()
