"""Grouped native build and differential CPU checks; no listeners or GPU."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_error_params.cases import cases

BASE = ROOT / '.q4t-work/serve-error-params-20261104'
ENGINE, HOST = BASE / 'engine', BASE / 'host'
CHANGED = {
    'include/q4t/server/input_contract.h',
    'include/q4t/server/output_constraints.h',
    'include/q4t/server/output_schema.h',
    'src/server/input_contract.cpp', 'src/server/output_constraints.cpp',
    'src/server/output_schema.cpp', 'src/server/chat_contract.cpp',
    'src/server/chat_plan.cpp'}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sources():
    release = read(ROOT / 'build/q4t.release.json')
    paths = set(read(release['tested_source_manifest']))
    paths.update(str(path.relative_to(ROOT)) for path in (ROOT/'tests').glob('*.cpp'))
    paths.update(('tools/serve_error_params/cpu.py', 'tools/serve_error_params/cases.py',
        'tools/serve_error_params/probe.cpp', 'tools/serve_error_params/CMakeLists.txt',
        'tools/release/required_host_tests.txt', 'tools/release/check_host_contracts.py',
        'tools/serve_audit/inventory.json', 'tools/serve_audit/check_inventory.py',
        'tools/serve_protocol/build_contract.py', 'docs/serve-openapi-v1.json',
        'docs/serve-chat-request-v1.schema.json', 'docs/serve-transport-v1.schema.json',
        'docs/SERVE_ERROR_PARAMS_2026-11-04.md'))
    return {name: sha(ROOT/name) for name in sorted(paths)}


def command(out, name, argv, timeout=180, input_text=None):
    save(out/(name+'-command.json'), dict(argv=list(map(str, argv)), timeout=timeout))
    with (out/(name+'.stdout')).open('xb') as stdout, \
            (out/(name+'.stderr')).open('xb') as stderr:
        run = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=stderr,
            input=None if input_text is None else input_text.encode(), timeout=timeout)
    save(out/(name+'-exit.json'), dict(returncode=run.returncode))
    print(name, run.returncode, flush=True)
    return run.returncode == 0


def build():
    out = BASE/'build-01'; out.mkdir(parents=True)
    save(out/'sources.json', sources())
    save(out/'default.json', {name:sha(ROOT/name) for name in ('build/q4t','build/q4t.release.json')})
    release = read(ROOT/'build/q4t.release.json')
    old = read(release['tested_source_manifest'])
    current = {name:sha(ROOT/name) for name in old}
    require({name for name in old if old[name] != current[name]} == CHANGED,
            'unreviewed runtime delta')
    save(out/'runtime.json', current)
    baseline = BASE/'baseline-source'
    baseline.mkdir()
    commit = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT,text=True).strip()
    for name in CHANGED:
        path = baseline/name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(subprocess.check_output(['git','show',commit+':'+name],cwd=ROOT))
        require(sha(path) == old[name], 'baseline differs from installed tested source')
    save(out/'baseline.json', dict(commit=commit, sources={name:old[name] for name in sorted(CHANGED)},
        installed_release_sha256=sha(ROOT/'build/q4t.release.json'),
        unchanged_runtime_sources=len(old)-len(CHANGED)))
    common = ['-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
              '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    commands = [
        ('configure-host', ['cmake','-S','tests/host','-B',str(HOST),*common,'-DQ4T_ENABLE_XGRAMMAR=OFF']),
        ('build-host', ['cmake','--build',str(HOST),'--target','q4t_host_tests','--parallel','2'])]
    for name, extra in [('baseline', ['-DQ4T_ERROR_BASELINE='+str(baseline)]), ('candidate', [])]:
        folder=BASE/(name+'-probe')
        commands += [(name+'-configure',['cmake','-S','tools/serve_error_params','-B',str(folder),*common,*extra]),
                     (name+'-build',['cmake','--build',str(folder),'--parallel','2'])]
    commands += [
        ('configure-engine', ['cmake','-S','.','-B',str(ENGINE),*common,
            '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
            '-DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc',
            '-DQ4T_CUDA_ARCHITECTURES=110a','-DQ4T_ENABLE_XGRAMMAR=ON',
            '-DQ4T_SERVE_FAULT_INJECTION=OFF','-DQ4T_STARTUP_RESOURCE_PLAN=OFF',
            '-DQ4T_BUILD_TESTS=ON','-DFETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR='+
            str(ROOT/'.q4t-work/structured-control-20261102/xgrammar')]),
        ('build-engine',['cmake','--build',str(ENGINE),'--target','q4t','q4t_host_tests',
                         '--parallel','2'])]
    results={}
    for name, argv in commands:
        results[name]=command(out,name,argv,2400)
        if not results[name]: break
    zero=not any(re.search(r'\bwarning:',p.read_text(),re.I) for p in out.glob('*.std*'))
    passed=len(results)==len(commands) and all(results.values()) and zero and sources()==read(out/'sources.json')
    binaries=[ENGINE/'q4t',ENGINE/'q4t_host_tests',HOST/'q4t_host_tests',
              BASE/'baseline-probe/error_param_probe',BASE/'candidate-probe/error_param_probe']
    save(out/'result.json',dict(passed=passed,checks=results,zero_warnings=zero,
        binaries={str(p):sha(p) for p in binaries if p.is_file()}))
    require(passed,'build or source gate failed; preserve attempt')


def test():
    out=BASE/'cpu-01';out.mkdir()
    built=read(BASE/'build-01/result.json')
    require(built['passed'] and sources()==read(BASE/'build-01/sources.json'),'frozen build drift')
    require(all(sha(path)==digest for path,digest in built['binaries'].items()),'binary drift')
    corpus=cases();save(out/'cases.json',corpus)
    payload=''.join(json.dumps(row['request'],ensure_ascii=False)+'\n' for row in corpus)
    checks={}
    for name in ('baseline','candidate'):
        checks[name]=command(out,name,[str(BASE/(name+'-probe')/'error_param_probe')],input_text=payload)
    old=[json.loads(line) for line in (out/'baseline.stdout').read_text().splitlines()]
    new=[json.loads(line) for line in (out/'candidate.stdout').read_text().splitlines()]
    require(len(old)==len(new)==len(corpus),'probe record loss')
    differences=[];changed=0
    for case,before,after in zip(corpus,old,new):
        if before['accepted'] != case['accepted'] or after['accepted'] != case['accepted']:
            differences.append(dict(name=case['name'],reason='acceptance',expected=case['accepted'],before=before,after=after));continue
        if case['accepted']:
            if before != after:differences.append(dict(name=case['name'],reason='success prompt/grammar',before=before,after=after))
        else:
            a,b=before['response']['error'],after['response']['error']
            if b['param'] != case['param'] or any(a[key]!=b[key] for key in ('message','type','code')):
                differences.append(dict(name=case['name'],reason='error contract',expected=case['param'],before=a,after=b))
            changed+=a['param']!=b['param']
    save(out/'differential.json',dict(passed=not differences,cases=len(corpus),
        accepted=sum(row['accepted'] for row in corpus),improved_locations=changed,differences=differences))
    checks['differential']=not differences
    checks['public-host']=command(out,'public-host',[str(HOST/'q4t_host_tests')])
    checks['required-host']=command(out,'required-host',['python3','-B','tools/release/check_host_contracts.py',
        '--test-binary',str(ENGINE/'q4t_host_tests'),'--output',str(out/'host-gate')])
    checks['structured']=command(out,'structured',[str(ENGINE/'q4t_host_tests'),'structured_decoder_'])
    checks['inventory']=command(out,'inventory',['python3','-B','tools/serve_audit/check_inventory.py','--self-check'])
    checks['generated']=command(out,'generated',['python3','-B','tools/serve_protocol/build_contract.py','--check'])
    passed=all(checks.values()) and sources()==read(BASE/'build-01/sources.json')
    save(out/'result.json',dict(passed=passed,checks=checks,sources=read(BASE/'build-01/sources.json'),
        models=0,http=0,gpu=0))
    require(passed,'CPU contract failure; preserve evidence')


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','test'))
    globals()[parser.parse_args().action]()
