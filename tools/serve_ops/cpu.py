"""Run grouped CPU checks once, preserving every command and original failure."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
BASE=ROOT/'.q4t-work/serve-ops-20261103'


def run():
    group=sys.argv[1] if len(sys.argv)==2 else 'group-02'
    if not group.startswith('group-') or '/' in group:
        raise ValueError('new group name required')
    out=BASE/'cpu'/group;out.mkdir(parents=True)
    tool=BASE/'deps/promtool'
    assert hashlib.sha256(tool.read_bytes()).hexdigest()=='d4ff433304144fae3d4490bd381be983e99064c601bc35e8dfe29f52300a9bc4'
    from tools.serve_ops.rule_tests import build
    fixture=out/'rule-tests.json'
    fixture.write_text(json.dumps(build(ROOT/'deploy/serve-ops/q4t.rules.yml'),indent=2)+'\n')
    unit=out/'q4t-ops-fixture.service'
    unit.write_text('[Unit]\nDescription=CPU syntax fixture only\n'+
        (ROOT/'deploy/serve-ops/q4t-logging.conf.example').read_text()+'\nExecStart=/usr/bin/true\n')
    commands=[('python',[sys.executable,'-B','-m','unittest','tools.serve_ops.test_ops','-v']),
        ('policy',[sys.executable,'-B','tools/serve_ops/rules.py','--check']),
        ('promtool-version',[str(tool),'--version']),
        ('rules',[str(tool),'check','rules',str(ROOT/'deploy/serve-ops/q4t.rules.yml')]),
        ('config',[str(tool),'check','config',str(ROOT/'deploy/serve-ops/prometheus.example.yml')]),
        ('rule-timeseries',[str(tool),'test','rules',str(fixture)]),
        ('systemd-unit',['systemd-analyze','verify',str(unit)])]
    results={}
    for name,argv in commands:
        started=time.monotonic()
        with (out/(name+'.stdout')).open('xb') as stdout, (out/(name+'.stderr')).open('xb') as stderr:
            process=subprocess.run(argv,cwd=ROOT,stdout=stdout,stderr=stderr,timeout=180)
        results[name]=dict(argv=argv,returncode=process.returncode,seconds=time.monotonic()-started)
        print(name,process.returncode,flush=True)
    result=dict(passed=all(r['returncode']==0 for r in results.values()),commands=results,
                http_or_model_started=False,original_failures_preserved=True)
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return 0 if result['passed'] else 1


if __name__=='__main__':
    sys.exit(run())
