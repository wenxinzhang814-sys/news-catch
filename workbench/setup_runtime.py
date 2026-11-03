"""Install pinned direct dependencies; use the Windows trust store when present.

Run with .venv\Scripts\python.exe setup_runtime.py. Certificate checks stay on.
"""
from pathlib import Path
import ssl
import subprocess
import sys

root=Path(__file__).resolve().parent
command=[sys.executable,'-m','pip','install','-r',str(root/'requirements.lock.txt'),'--disable-pip-version-check']
if sys.platform=='win32':
    certificates=[]
    for store in ('ROOT','CA'):
        for certificate,encoding,trust in ssl.enum_certificates(store):
            if encoding=='x509_asn':certificates.append(ssl.DER_cert_to_PEM_cert(certificate))
    if certificates:
        bundle=Path(sys.prefix)/'windows-trust.pem'
        bundle.write_text('\n'.join(certificates),encoding='ascii')
        command.extend(['--cert',str(bundle)])
raise SystemExit(subprocess.call(command,cwd=root))
