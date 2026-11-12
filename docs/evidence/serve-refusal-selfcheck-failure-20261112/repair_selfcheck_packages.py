"""New immutable payloads with the reviewed self-check parser repair only."""
import copy
import os
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,version_contract
from tools.serve_service import package as engine

WORK=Path(__file__).resolve().parent
NAME='source/tools/serve_recovery/core_http.py'


def repair(old,digest,target):
    manifest=bundle.verify(old,digest)
    engine.require(not target.exists(),'preserve prior package')
    shutil.copytree(old,target,symlinks=True)
    (target/NAME).chmod(0o600)
    shutil.copyfile(ROOT/NAME.removeprefix('source/'),target/NAME)
    (target/NAME).chmod(0o400)
    value=copy.deepcopy(manifest);value['files'][NAME]=engine.sha(target/NAME)
    engine.save(target/'manifest.json',value)
    changed=engine.sha(target/'manifest.json')
    (target/'bin/q4t').chmod(0o700)
    (target/'bin/q4t').write_bytes(bundle.launcher(changed,value['python']['path']))
    (target/'bin/q4t').chmod(0o500)
    bundle.verify(target,changed);bundle.verify(old,digest)
    engine.require({n for n,h in value['files'].items() if h!=manifest['files'][n]}=={NAME},
                   'unreviewed payload change')
    engine.require(value['engine_manifest_sha256']==manifest['engine_manifest_sha256'],
                   'engine identity changed')
    return dict(package=str(target),manifest_sha256=changed)


def main():
    os.umask(0o077)
    out=WORK/'selfcheck-repair-01';out.mkdir(mode=0o700)
    sources=[('primary',WORK/'candidate-03/unqualified-bundle',
        'ef8881343d3ab30ef9b100e54c0a0162cdeb4ce9a7be56a8fd8e8640c76c1384'),
        ('fallback',WORK/'fallback-01/bundle',
        'bb96c5f41720e9332ce048e5fc7215301c0647373728bbea27d49fb0045d63d9')]
    result={name:repair(old,digest,out/name) for name,old,digest in sources}
    permit=version_contract.compatible(*(version_contract.payload(result[n])
                                       for n in ('primary','fallback')))
    engine.save(out/'review.json',dict(status='PREPARED_TARGETED_SELF_CHECK_REPAIR',
        payloads=result,changed_source=NAME,sha256=engine.sha(ROOT/NAME.removeprefix('source/')),
        other_sealed_files_unchanged=True,engine_qualification_reused=True,
        old_payloads_unchanged=True,compatibility=permit,actual_business_pending=True))
    print(result)


if __name__=='__main__':main()
