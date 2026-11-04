"""Only renew reviewed source anchors; retain/reuse all passed CPU groups."""
import argparse
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_startup_listeners import cpu
OUT=cpu.BASE/'cpu-gate-final'
ALLOWED={'tools/serve_audit/inventory.json','tools/serve_startup_listeners/stage.py',
         'tools/serve_startup_listeners/report.py'}


def original():
    value=cpu.read(cpu.BASE/'cpu-01/result.json')
    cpu.require(not value['passed'] and {k for k,v in value['checks'].items() if not v}=={'inventory'},'original failure differs')
    sources=cpu.read(cpu.BASE/'cpu-01/sources.json')
    changed={n for n,d in sources.items() if cpu.sha(ROOT/n)!=d}
    cpu.require(changed==ALLOWED,'unreviewed change since CPU: '+str(changed))
    return value


def run():
    value=original();OUT.mkdir()
    cpu.save(OUT/'sources.json',cpu.sources())
    prior=cpu.BASE/'cpu-inventory-fix'
    reviewed=cpu.read(prior/'sources.json')
    for name in ('tools/serve_audit/inventory.json','tools/serve_audit/check_inventory.py'):
        cpu.require(cpu.sha(ROOT/name)==reviewed[name],'inventory/checker changed')
    passed=cpu.read(prior/'inventory-exit.json')['returncode']==0
    cpu.save(OUT/'reused-inventory.json',{str(prior/name):cpu.sha(prior/name)
        for name in ('sources.json','inventory-exit.json','inventory.stdout','inventory.stderr','original-finish_cpu.py')})
    cpu.save(OUT/'result.json',dict(passed=passed,original=cpu.sha(cpu.BASE/'cpu-01/result.json'),
        changed=sorted(ALLOWED),reused=[k for k,v in value['checks'].items() if v],models=0,http=0,gpu=0))
    check()


def check():
    original();cpu.require(cpu.read(OUT/'result.json')['passed'],'inventory fix not passed')
    for n,d in cpu.read(OUT/'sources.json').items():
        cpu.require(cpu.sha(ROOT/n)==d,'repaired CPU source drift: '+n)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('run','check'))
    globals()[p.parse_args().action]()
