"""Bounded preparation cancellation build and CPU contracts; no HTTP/GPU."""
import argparse
from pathlib import Path
import os
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_listener import cpu as shared
from tools.serve_operations_listener.cpu import sha,read,save,require,command,runtime

BASE=ROOT/'.q4t-work/serve-prepare-lock-20261104'
ENGINE,HOST=BASE/'engine',BASE/'host'
CHANGED={'CMakeLists.txt','include/q4t/server/request_control.h','include/q4t/server/request_diagnostics.h',
         'src/server/chat_generation.cpp','src/server/chat_preflight.cpp'}


def sources():
    result=shared.sources()
    result.update({str(p.relative_to(ROOT)):sha(p)
                   for p in (ROOT/'tools/serve_prepare_lock').glob('*') if p.is_file()})
    return result


def build(label):
    shared.BASE,shared.ENGINE,shared.HOST=BASE,ENGINE,HOST
    shared.build(label)
    out=BASE/(label+'-probe');out.mkdir()
    save(out/'sources.json',sources())
    ok=command(out,'probe',['cmake','--build',ENGINE,'--target',
                           'q4t_prepare_lock_probe','--parallel','2'],240)
    zero=not any(re.search(r'\bwarning:',p.read_text(),re.I) for p in out.glob('*.std*'))
    passed=ok and zero and sources()==read(out/'sources.json')
    save(out/'result.json',dict(passed=passed,zero_warnings=zero,
        binary_sha256=sha(ENGINE/'q4t_prepare_lock_probe') if ok else None))
    require(passed,'native probe build; preserve attempt')


def test(label):
    out=BASE/label;out.mkdir()
    save(out/'sources.json',sources())
    checks={}
    for name,argv in (
        ('public-host',[HOST/'q4t_host_tests']),
        ('required-host',[sys.executable,'-B','tools/release/check_host_contracts.py',
             '--test-binary',ENGINE/'q4t_host_tests','--output',out/'host-gate']),
        ('structured',[ENGINE/'q4t_host_tests','structured_decoder_']),
        ('inventory',[sys.executable,'-B','tools/serve_audit/check_inventory.py','--self-check']),
        ('generated',[sys.executable,'-B','tools/serve_protocol/build_contract.py','--check'])):
        checks[name]=command(out,name,argv)
    passed=all(checks.values()) and sources()==read(out/'sources.json')
    save(out/'result.json',dict(passed=passed,checks=checks,models=0,http=0,gpu=0))
    require(passed,'CPU failure; preserve attempt')


def finish(label):
    out=BASE/label;out.mkdir()
    prior=read(BASE/'cpu-01/result.json')
    reused={name:ok for name,ok in prior['checks'].items() if name!='inventory'}
    require(len(reused)==4 and all(reused.values()),'only inventory may fail')
    reviewed={'tools/serve_audit/inventory.json',
              'tools/serve_prepare_lock/cpu.py','tools/serve_prepare_lock/stage.py'}
    for name,digest in read(BASE/'cpu-01/sources.json').items():
        if name not in reviewed:require(sha(ROOT/name)==digest,'CPU reuse drift: '+name)
    for name,digest in read(BASE/'build-02/result.json')['binaries'].items():
        require(sha(name)==digest,'built binary changed')
    save(out/'sources.json',sources())
    ok=command(out,'inventory',[sys.executable,'-B','tools/serve_audit/check_inventory.py','--self-check'])
    save(out/'result.json',dict(passed=ok,reused_cpu=str(BASE/'cpu-01/result.json'),
        reused_checks=reused,reviewed_source_changes=sorted(reviewed),models=0,http=0,gpu=0))
    require(ok,'inventory still failed; preserve attempt')


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','test','finish'))
    parser.add_argument('--label',required=True)
    args=parser.parse_args()
    require(re.fullmatch(r'(build|cpu)-[a-zA-Z0-9_-]+',args.label),'new label')
    globals()[args.action](args.label)
