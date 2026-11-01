"""Task Scheduler directly owns this interpreter and its bounded Windows Job."""
import os
from pathlib import Path
import runpy
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
# Use the existing installation's packages without launching a venv redirector.
sys.path.insert(0,str(ROOT/'.venv/Lib/site-packages'))
from probe.platform_io import open_private
stream=os.fdopen(open_private(ROOT/'runtime/official-tunnel.log',os.O_CREAT|os.O_APPEND|os.O_WRONLY),
                 'a',encoding='utf-8',buffering=1)
sys.stdout=sys.stderr=stream
os.chdir(ROOT)
script=ROOT/'scripts/run-windows.py'
sys.argv=[str(script)]
runpy.run_path(str(script),run_name='__main__')
