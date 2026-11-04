"""Grouped startup-memory builds and direct contracts; no model or HTTP here."""
import re
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_lifecycle import stage as old
from tools.serve_observation_capacity.cpu import command
h,a=old.h,old.a
BASE=ROOT/'.q4t-work/serve-startup-memory-20261103'
CPU=BASE/'cpu'


def sources():
    release=h.read(ROOT/'build/q4t.release.json')
    paths=set(h.read(release['tested_source_manifest']))|{'include/q4t/server/startup_admission.h'}
    return {p:h.sha(ROOT/p) for p in sorted(paths)}


def build():
    CPU.mkdir()
    initial=sources();h.save(CPU/'build-sources.json',initial)
    commands=[(n,[v.replace(str(old.PRIOR),str(BASE)) for v in args]) for n,args in
              h.read(old.PRIOR/'cpu/commands.json')['commands']]
    h.save(CPU/'commands.json',dict(commands=commands))
    passed=True
    for name,argv in commands:
        if not command(CPU,name,argv):passed=False;break
    logs='\n'.join(p.read_text() for p in CPU.glob('*.std*'))
    zero=re.search(r'\bwarning:',logs,re.I) is None
    h.save(CPU/'build-result.json',dict(passed=passed,zero_warnings=zero,
        runtime_sources_unchanged_during_build=sources()==initial,
        binary_sha256=h.sha(BASE/'engine/q4t') if (BASE/'engine/q4t').exists() else None))
    h.require(passed and zero and sources()==initial,'build failure/warning/source drift')


def test():
    h.require(h.read(CPU/'build-result.json')['passed'] and
              sources()==h.read(CPU/'build-sources.json'), 'build/source gate')
    folder=CPU/'tests';folder.mkdir()
    commands=[('host-'+mode,[str(BASE/('host-'+mode)/'q4t_host_tests')]) for mode in ('off','on')]
    commands += [('host-required',['/usr/bin/python3','-B','tools/release/check_host_contracts.py',
        '--test-binary',str(BASE/'engine/q4t_host_tests'),'--output',str(folder/'host-gate')]),
        ('backend',[str(BASE/'engine/q4t_host_tests'),'structured_decoder_']),
        ('driver',['/usr/bin/python3','-B','-m','unittest','tools.serve_startup_memory.test_driver','-v']),
        ('config',['/usr/bin/python3','-B','tools/serve_admission/config_check.py',
            '--engine',str(BASE/'engine'),'--output',str(folder/'config')]),
        ('inventory',['/usr/bin/python3','-B','tools/serve_audit/check_inventory.py','--self-check']),
        ('generated',['/usr/bin/python3','-B','tools/serve_protocol/build_contract.py','--check'])]
    results={name:command(folder,name,argv,timeout=180) for name,argv in commands}
    if results['config']:
        from tools.serve_startup_memory.config_check import run
        try:run(folder/'config/config_probe',folder/'startup-config');results['startup-config']=True
        except Exception as error:
            results['startup-config']=False
            h.save(folder/'startup-config-error.json',dict(error=repr(error)))
    h.save(folder/'result.json',dict(passed=all(results.values()),commands=results,
        http_or_model_started=False))
    h.require(all(results.values()),'CPU failure retained; repair affected checks only')


if __name__=='__main__':
    if sys.argv[1:]==['build']:build()
    elif sys.argv[1:]==['test']:test()
    else:raise SystemExit('build | test')
