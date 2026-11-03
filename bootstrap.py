"""Create a local venv and install dependencies once, then start the app."""
from pathlib import Path
import hashlib
import os
import subprocess
import sys
import venv

BASE=Path(__file__).resolve().parent
if sys.version_info < (3,11):
    raise SystemExit('Python 3.11 or newer is required.')
environment=BASE/'.venv'
python=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
if not python.exists():
    venv.EnvBuilder(with_pip=True).create(environment)
lock=BASE/'workbench/requirements.lock.txt'
stamp=environment/'.dependencies.sha256'
signature=hashlib.sha256(lock.read_bytes()).hexdigest()
if not stamp.exists() or stamp.read_text().strip()!=signature:
    subprocess.check_call([str(python),'-B',str(BASE/'workbench/setup_runtime.py')],cwd=BASE)
    stamp.write_text(signature,encoding='ascii')
raise SystemExit(subprocess.call([str(python),'-B',str(BASE/'launch.py'),*sys.argv[1:]],cwd=BASE))
