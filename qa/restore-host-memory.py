#!/usr/bin/env python3
"""Restore stock Android memory producer after pausing QA and stopping devices.

No device is stopped, recreated or started by this script. It refuses active
workers/runtimes. The administrator's configured host watermark is retained;
the contaminated pre-fix factor is never restored automatically.
"""
import os
from pathlib import Path
import subprocess
import sys

if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Run as root after pausing QA and stopping native devices.')
    raise SystemExit(subprocess.call([sys.executable,str(Path(__file__).with_name('qa_host_memory.py')),
                                     '--all','--rollback-producers',*sys.argv[1:]]))
