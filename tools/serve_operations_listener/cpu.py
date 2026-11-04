"""Grouped local build and CPU checks, never opens an HTTP listener or CUDA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
BASE=ROOT/'.q4t-work/serve-operations-listener-20261104'
ENGINE,HOST=BASE/'engine',BASE/'host'
TESTS=('tools.serve_operations_listener.test_tools','tools.serve_ops.test_ops',
       'tools.serve_service.test_service','tools.serve_service.test_inspection',
       'tools.serve_service.test_resources','tools.serve_gateway.test_gateway',
       'tools.serve_gateway.test_managed','tools.serve_gateway.test_sources',
       'tools.serve_clients.test_packages','tools.serve_host.test_host')


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def read(path):return json.loads(Path(path).read_bytes())


def save(path,value):
    with Path(path).open('x') as stream:
        json.dump(value,stream,indent=2,sort_keys=True);stream.write('\n')


def require(ok,message):
    if not ok:raise RuntimeError(message)


def runtime():
    previous=read(ROOT/'build/q4t.release.json')
    return {name:sha(ROOT/name) for name in read(previous['tested_source_manifest'])}


def sources():
    paths=set(runtime())
    for folder in ('tests','cmake','tools/serve_operations_listener','tools/serve_ops',
                   'tools/serve_gateway','tools/serve_service','tools/serve_host',
                   'tools/serve_reliability','tools/serve_protocol','tools/serve_audit'):
        paths.update(str(p.relative_to(ROOT)) for p in (ROOT/folder).rglob('*')
                     if p.is_file() and (p.suffix in ('.py','.cpp','.h','.cmake','.json')
                         or p.name=='CMakeLists.txt'))
    paths.update(('CMakeLists.txt','tools/release/required_host_tests.txt',
        'tools/release/check_host_contracts.py','docs/serve-config-v1.schema.json',
        'docs/serve-openapi-v1.json','docs/serve-input-semantics-v1.json',
        'deploy/serve-ops/q4t.rules.yml','deploy/serve-ops/q4t-v4.rules.yml',
        'deploy/serve-ops/q4t-v3.rules.yml','tools/serve_clients/test_packages.py'))
    return {name:sha(ROOT/name) for name in sorted(paths)}


def command(out,name,argv,timeout=180):
    argv=list(map(str,argv));save(out/(name+'-command.json'),dict(argv=argv,timeout=timeout))
    start=time.monotonic()
    with (out/(name+'.stdout')).open('xb') as stdout,(out/(name+'.stderr')).open('xb') as stderr:
        result=subprocess.run(argv,cwd=ROOT,stdout=stdout,stderr=stderr,timeout=timeout)
    save(out/(name+'-exit.json'),dict(returncode=result.returncode,seconds=time.monotonic()-start))
    print(name,result.returncode,flush=True)
    return result.returncode==0


def build(label):
    out=BASE/label;out.mkdir(parents=True)
    save(out/'sources.json',sources());save(out/'runtime.json',runtime())
    save(out/'default.json',{name:sha(ROOT/name) for name in ('build/q4t','build/q4t.release.json')})
    old=read(read(ROOT/'build/q4t.release.json')['tested_source_manifest'])
    save(out/'runtime-delta.json',{name:dict(before=old[name],after=digest)
        for name,digest in runtime().items() if digest!=old[name]})
    common=['-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
            '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    commands=[('configure-host',['cmake','-S','tests/host','-B',str(HOST),*common,'-DQ4T_ENABLE_XGRAMMAR=OFF']),
        ('build-host',['cmake','--build',str(HOST),'--target','q4t_host_tests','--parallel','2']),
        ('configure-engine',['cmake','-S','.','-B',str(ENGINE),*common,
            '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
            '-DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc',
            '-DQ4T_CUDA_ARCHITECTURES=110a','-DQ4T_ENABLE_XGRAMMAR=ON',
            '-DQ4T_SERVE_FAULT_INJECTION=OFF','-DQ4T_STARTUP_RESOURCE_PLAN=OFF',
            '-DQ4T_BUILD_TESTS=ON','-DFETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR='+
            str(ROOT/'.q4t-work/structured-control-20261102/xgrammar')]),
        ('build-engine',['cmake','--build',str(ENGINE),'--target','q4t','q4t_host_tests',
                         'q4t_http_lifecycle_tests','--parallel','2'])]
    checks={}
    for name,argv in commands:
        checks[name]=command(out,name,argv,2400)
        if not checks[name]:break
    zero=not any(re.search(r'\bwarning:',p.read_text(),re.I) for p in out.glob('*.std*'))
    binaries=[HOST/'q4t_host_tests',ENGINE/'q4t',ENGINE/'q4t_host_tests',ENGINE/'q4t_http_lifecycle_tests']
    passed=len(checks)==len(commands) and all(checks.values()) and zero and runtime()==read(out/'runtime.json')
    save(out/'result.json',dict(passed=passed,checks=checks,zero_warnings=zero,
        binaries={str(p):sha(p) for p in binaries if p.is_file()}))
    require(passed,'build failed; preserve original attempt')


def test(label):
    out=BASE/label;out.mkdir(parents=True)
    save(out/'sources.json',sources())
    for name in sources():
        target=out/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
    checks={}
    checks['python']=command(out,'python',[sys.executable,'-B',__file__,'unit','--label',label],240)
    checks['public-host']=command(out,'public-host',[HOST/'q4t_host_tests'])
    checks['required-host']=command(out,'required-host',[sys.executable,'-B','tools/release/check_host_contracts.py',
        '--test-binary',ENGINE/'q4t_host_tests','--output',out/'host-gate'])
    checks['structured']=command(out,'structured',[ENGINE/'q4t_host_tests','structured_decoder_'])
    checks['inventory']=command(out,'inventory',[sys.executable,'-B','tools/serve_audit/check_inventory.py','--self-check'])
    checks['generated']=command(out,'generated',[sys.executable,'-B','tools/serve_protocol/build_contract.py','--check'])
    checks['rules-generated']=command(out,'rules-generated',[sys.executable,'-B','tools/serve_ops/rules.py','--check'])
    promtool=ROOT/'.q4t-work/serve-ops-20261103/deps/promtool'
    require(sha(promtool)=='d4ff433304144fae3d4490bd381be983e99064c601bc35e8dfe29f52300a9bc4','promtool identity')
    from tools.serve_ops import rule_tests
    for version,name in ((5,'q4t.rules.yml'),(4,'q4t-v4.rules.yml'),(3,'q4t-v3.rules.yml')):
        fixture=out/f'rules-v{version}.json';save(fixture,rule_tests.build(ROOT/'deploy/serve-ops'/name,version=version))
        checks[f'rules-v{version}']=command(out,f'rules-v{version}',[promtool,'test','rules',fixture])
    passed=all(checks.values()) and sources()==read(out/'sources.json')
    save(out/'result.json',dict(passed=passed,checks=checks,models=0,http=0,gpu=0))
    require(passed,'CPU failure; preserve original attempt')


def unit(_label):
    # Imported fixture TestCase classes can occur in several selected modules.
    # Run each actual case once, preserving its canonical unittest identity.
    selected,seen=unittest.TestSuite(),set()
    def add(value):
        if isinstance(value,unittest.TestSuite):
            for item in value:add(item)
        elif value.id() not in seen:
            seen.add(value.id());selected.addTest(value)
    for name in TESTS:add(unittest.defaultTestLoader.loadTestsFromName(name))
    result=unittest.TextTestRunner(verbosity=2).run(selected)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','test','unit'));parser.add_argument('--label',required=True)
    args=parser.parse_args();require(re.fullmatch(r'(build|cpu)-[a-zA-Z0-9_-]+',args.label),'new private label required')
    globals()[args.action](args.label)
