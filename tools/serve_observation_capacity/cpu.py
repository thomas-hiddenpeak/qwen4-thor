"""Grouped SRV-12b build/CPU gates; all listener tests run in the later lease."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_memory import stage as previous

h,a=previous.h,previous.a
BASE=ROOT/'.q4t-work/serve-observation-capacity-20261103'
CPU=BASE/'cpu'


def sources():
    release=h.read(ROOT/'build/q4t.release.json')
    paths=set(h.read(release['tested_source_manifest']))|{'include/q4t/server/http_connection.h'}
    return {name:h.sha(ROOT/name) for name in sorted(paths)}


def command(folder,name,argv,*,timeout=900):
    h.save(folder/(name+'-command.json'),dict(argv=argv,started=time.time()))
    with (folder/(name+'.stdout')).open('xb') as out,(folder/(name+'.stderr')).open('xb') as err:
        result=subprocess.run(argv,cwd=ROOT,stdout=out,stderr=err,timeout=timeout)
    h.save(folder/(name+'-exit.json'),dict(returncode=result.returncode,ended=time.time()))
    print(name,result.returncode,flush=True)
    return result.returncode==0


def build():
    CPU.mkdir(parents=True)
    commands=[(name,[arg.replace(str(previous.BASE),str(BASE)) for arg in argv])
              for name,argv in h.read(previous.BASE/'cpu/commands.json')['commands']]
    h.save(CPU/'commands.json',dict(commands=commands))
    initial=sources();h.save(CPU/'build-sources.json',initial)
    passed=True
    for name,argv in commands:
        if not command(CPU,name,argv):passed=False;break
    logs='\n'.join(p.read_text() for p in CPU.glob('*.std*'))
    zero=re.search(r'\bwarning:',logs,re.I) is None and 'cmake warning' not in logs.lower()
    h.save(CPU/'build-result.json',dict(passed=passed,zero_warnings=zero,
        binary_sha256=h.sha(BASE/'engine/q4t') if (BASE/'engine/q4t').exists() else None,
        runtime_sources_unchanged_during_build=sources()==initial))
    h.require(passed and zero and sources()==initial,'build failed, warned or changed')


def test():
    h.require(h.read(CPU/'build-result.json')['passed'] and sources()==h.read(CPU/'build-sources.json'),'build/source gate')
    folder=CPU/'tests';folder.mkdir()
    from tools.serve_ops.rule_tests import build as scenarios
    h.save(folder/'rules.json',scenarios(ROOT/'deploy/serve-ops/q4t.rules.yml'))
    tool=ROOT/'.q4t-work/serve-ops-20261103/deps/promtool'
    h.require(h.sha(tool)=='d4ff433304144fae3d4490bd381be983e99064c601bc35e8dfe29f52300a9bc4','promtool identity')
    commands=[('host-'+mode,[str(BASE/('host-'+mode)/'q4t_host_tests')]) for mode in ('off','on')]
    commands += [('host-required',['/usr/bin/python3','-B','tools/release/check_host_contracts.py',
                  '--test-binary',str(BASE/'engine/q4t_host_tests'),'--output',str(folder/'host-gate')]),
        ('backend',[str(BASE/'engine/q4t_host_tests'),'structured_decoder_']),
        ('operations',['/usr/bin/python3','-B','-m','unittest','tools.serve_ops.test_ops',
                       'tools.serve_observation_capacity.test_ops','-v']),
        ('policy',['/usr/bin/python3','-B','tools/serve_ops/rules.py','--check']),
        ('prometheus-rules',[str(tool),'check','rules',str(ROOT/'deploy/serve-ops/q4t.rules.yml')]),
        ('prometheus-scenarios',[str(tool),'test','rules',str(folder/'rules.json')]),
        ('inventory',['/usr/bin/python3','-B','tools/serve_audit/check_inventory.py','--self-check']),
        ('generated',['/usr/bin/python3','-B','tools/serve_protocol/build_contract.py','--check'])]
    results={name:command(folder,name,argv,timeout=180) for name,argv in commands}
    h.save(folder/'result.json',dict(passed=all(results.values()),commands=results,http_or_model_started=False))
    h.require(all(results.values()),'CPU failure retained; repair affected gates only')


if __name__=='__main__':
    if sys.argv[1:]==['build']:build()
    elif sys.argv[1:]==['test']:test()
    else:raise SystemExit('build | test')
