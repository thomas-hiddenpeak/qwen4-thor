#!/usr/bin/python3
"""Build corrected pooled-state fixture after all HTTP timing, before tests."""
import os,sys,subprocess,json,hashlib
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor'); out=root/'.q4t-work/b1b-20261019'
assert (out/'http-complete').read_text()=='passed\n'
for name,cmd in [('build-direct-final',['cmake','--build',str(out/'build'),'--target','q4t_tests','--parallel','4']),('build-reference-final',['python3',str(out/'build_reference.py')])]:
    with (out/(name+'.log')).open('w') as log:
        p=subprocess.run(cmd,cwd=root,stdout=log,stderr=subprocess.STDOUT)
    assert p.returncode==0 and 'warning:' not in (out/(name+'.log')).read_text(),name
files=[out/'build/q4t_tests',out/'reference/q4t_tests',root/'tests/mtp_spec_multi_test.cpp',root/'include/q4t/mtp/mtp.h']
(out/'direct-identity.json').write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},indent=2)+'\n')
# The linker has replaced this executable shim with the corrected binary.
os.execv(str(out/'build/q4t_tests'),[str(out/'build/q4t_tests'),*sys.argv[1:]])
