"""Fresh delivery directory after strict guard ancestry rejected relative argv."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_clients import delivery as d

OUTPUT=d.BASE/'delivery-absolute'
RECEIPT=d.BASE/'delivery-launch.json'


def prepare():
    d.check()
    d.h.require(not RECEIPT.exists() and not OUTPUT.exists(),'preserve delivery attempts')
    d.h.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit launch correction')
    failure=d.OUTPUT/'lease/exit.json'
    d.h.require(d.s.pkg.read(failure)['status']=='COMMAND_FAILED' and
                sorted(path.name for path in d.OUTPUT.iterdir())==['lease'],
                'original rejection changed default or reached transaction')
    d.a.verify_hashes(ROOT/'build',d.check()['old_hashes'])
    d.cpu.save(RECEIPT,dict(source=d.h.identity(Path(__file__)),
        package=d.h.identity(d.PACKAGE/'sealed-files.json'),
        original_guard=d.h.identity(failure),output=str(OUTPUT),
        reason='Absolute guard executable and output argv required by require_guard; first relative invocation stopped before begin/install.',
        production_unchanged=True,models_started=0,http_requests=0))


def check():
    value=d.s.pkg.read(RECEIPT)
    for name in ('source','package','original_guard'):
        d.h.check_identity(value[name])
    d.h.require(value['output']==str(OUTPUT),'foreign correction output')
    d.OUTPUT=OUTPUT
    return d.check()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','check','execute','finalize','rollback'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.action=='prepare':
        prepare()
    else:
        check()
        if args.action=='execute':
            d.bind();d.transaction.execute()
        elif args.action=='finalize':
            d.finalize()
        elif args.action=='rollback':
            d.h.require(args.output is not None,'fresh rollback lease directory required')
            d.rollback(args.output.resolve())
    print(json.dumps(dict(action=args.action,passed=True)))
