"""Retain the accepted older controller with the newly qualified engine.

This constructs a new payload. It is NOT the old payload's business acceptance
and cannot be registered until its own managed service checks have passed.
"""
import copy
import os
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, version_contract
from tools.serve_service import package as p

WORK = ROOT/'.q4t-work/serve-refusal-20261112'
OUT = WORK/'fallback-01'
OLD = ROOT/'.q4t-work/serve-outer-recovery-20261112/worktree/.q4t-work/serve-outer-business-20261112/business-03/retained-fallback'
OLD_SHA = '5993494254afc76d3963be42cd3adcf10729068a376a07e4786fd2bbc79e938b'


def main():
    p.require(not OUT.exists(), 'preserve prior fallback attempt')
    before = {n:p.sha(ROOT/n) for n in ('build/q4t', 'build/q4t.release.json',
        'build/service/manifest.json', 'build/service.release.json')}
    old = bundle.verify(OLD, OLD_SHA)
    receipt = p.read(WORK/'candidate-03/receipt.json')
    primary = Path(receipt['package'])
    accepted = bundle.verify(primary, receipt['manifest_sha256'])
    p.require(old['recovery_profile_policy'] == accepted['recovery_profile_policy'],
              'profile configuration differs')
    OUT.mkdir(mode=0o700)
    target = OUT/'bundle'
    shutil.copytree(OLD, target, symlinks=True,
                    ignore=lambda folder,_: {'engine'} if Path(folder) == OLD else set())
    shutil.copytree(primary/'engine', target/'engine', symlinks=True)
    changed = copy.deepcopy(old)
    changed['engine_manifest_sha256'] = accepted['engine_manifest_sha256']
    changed['recovery_profiles'] = accepted['recovery_profiles']
    for name in ('profile-primary.json', 'profile-light.json'):
        (target/name).chmod(0o600)
        shutil.copyfile(primary/name, target/name)
        (target/name).chmod(0o400)
        changed['files'][name] = p.sha(target/name)
    p.save(target/'manifest.json', changed)
    digest = p.sha(target/'manifest.json')
    (target/'bin/q4t').chmod(0o700)
    (target/'bin/q4t').write_bytes(bundle.launcher(digest, changed['python']['path']))
    (target/'bin/q4t').chmod(0o500)
    actual = bundle.verify(target, digest)
    p.require(actual == changed, 'unexpected fallback representation')
    difference = {n for n,h in old['files'].items() if changed['files'][n] != h}
    p.require(difference == {'profile-primary.json','profile-light.json'},
              'retained controller/helper source changed')
    root = dict(package=str(primary), manifest_sha256=receipt['manifest_sha256'])
    fallback = dict(package=str(target), manifest_sha256=digest)
    compatibility = version_contract.compatible(version_contract.payload(root),
                                                version_contract.payload(fallback))
    p.require(compatibility['automatic'], 'automatic feature contract differs')
    p.require(all(p.sha(ROOT/n) == h for n,h in before.items()), 'default changed')
    p.save(OUT/'review.json', dict(status='PREPARED_NEW_PAYLOAD_MANAGED_BUSINESS_PENDING',
        original=dict(package=str(OLD), manifest_sha256=OLD_SHA), primary=root,
        fallback=fallback, engine_binary_sha256=receipt['engine_binary_sha256'],
        controller_source_sha256=old['files']['source/tools/serve_recovery/controller.py'],
        native_programs_unchanged=True, all_controller_sources_unchanged=True,
        profile_policy_unchanged=True, original_engine_replaced_with_qualified_candidate=True,
        automatic_compatibility=compatibility, exact_managed_acceptance_pending=True,
        previous_acceptance_reused_as_new_payload_acceptance=False,
        scope='Retained older control implementation; same newly qualified inference engine. No claim of an independent older implementation of model refusal.',
        default_unchanged=True, gpu_starts=0))
    print(digest)


if __name__ == '__main__':
    os.umask(0o077)
    main()
