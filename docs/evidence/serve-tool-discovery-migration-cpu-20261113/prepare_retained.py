"""New current-engine/retained-control payload; real acceptance still required."""
import copy
import os
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, cold_compat, version_contract
from tools.serve_service import package as p

WORK = Path(__file__).resolve().parent
OUT = WORK / 'retained-01'
OLD = ROOT / '.q4t-work/serve-refusal-20261112/selfcheck-repair-01/fallback'
OLD_SHA = 'c93e9714786643a76a933039bff0843df1d40030f2669085f6041af8db0e1ae7'
PRIMARY = WORK / 'candidate-01/bundle'
PRIMARY_SHA = 'cb40656e41281c4e057edd746b5c72982b9e1ef2d60def74eb8bc1a2481c2299'


def main():
    os.umask(0o077)
    before = {n: p.sha(ROOT/n) for n in ('build/q4t', 'build/q4t.release.json',
        'build/service/manifest.json', 'build/service.release.json')}
    old = bundle.verify(OLD, OLD_SHA)
    current = bundle.verify(PRIMARY, PRIMARY_SHA)
    p.require(not OUT.exists(), 'preserve prior preparation')
    p.require(old['recovery_profile_policy'] == current['recovery_profile_policy'],
              'profile policy differs')
    OUT.mkdir(mode=0o700)
    target = OUT / 'bundle'
    shutil.copytree(OLD, target, symlinks=True,
        ignore=lambda folder, _: {'engine'} if Path(folder) == OLD else set())
    shutil.copytree(PRIMARY/'engine', target/'engine', symlinks=True)
    changed = copy.deepcopy(old)
    changed['engine_manifest_sha256'] = current['engine_manifest_sha256']
    changed['recovery_profiles'] = current['recovery_profiles']
    names = ('profile-primary.json', 'profile-light.json', 'native-build.json',
             'bin/q4t-guardian', 'bin/q4t-cuda-probe', 'bin/q4t-ingress')
    for name in names:
        path = target/name
        path.chmod(0o600)
        shutil.copyfile(PRIMARY/name, path)
        path.chmod(0o500 if name.startswith('bin/') else 0o400)
        changed['files'][name] = p.sha(path)
    p.save(target/'manifest.json', changed)
    digest = p.sha(target/'manifest.json')
    launch = target/'bin/q4t'
    launch.chmod(0o700)
    launch.write_bytes(bundle.launcher(digest, changed['python']['path']))
    launch.chmod(0o500)
    p.require(bundle.verify(target, digest) == changed, 'payload verification')
    difference = {n for n,h in old['files'].items() if changed['files'][n] != h}
    p.require(difference == set(names)-{'bin/q4t-cuda-probe'}, 'unexpected control changes')
    p.require(cold_compat.aware(target), 'cold custody awareness missing')
    control = 'source/tools/serve_recovery/controller.py'
    p.require(changed['files'][control] == old['files'][control] !=
              current['files'][control], 'retained implementation must differ')
    root = dict(package=str(PRIMARY), manifest_sha256=PRIMARY_SHA)
    fallback = dict(package=str(target), manifest_sha256=digest)
    permit = version_contract.compatible(version_contract.payload(root),
                                         version_contract.payload(fallback))
    p.require(permit['automatic'], 'automatic compatibility')
    p.require(all(p.sha(ROOT/n) == h for n,h in before.items()), 'default changed')
    bundle.verify(OLD, OLD_SHA)
    p.save(OUT/'review.json', dict(status='NEW_PAYLOAD_ACTUAL_ACCEPTANCE_PENDING',
        original=dict(package=str(OLD), manifest_sha256=OLD_SHA), primary=root,
        fallback=fallback, changed_files=sorted(difference), compatibility=permit,
        native_helpers='Exact current accepted helper bytes and build receipt; v12 does not install the v18 device-control executable.',
        control_implementation='Exact accepted c93e9714 older Python control; current engine and native helpers. No independent older engine rollback.',
        original_acceptance_not_relabelled=True, default_unchanged=True,
        driver_sha256=p.sha(Path(__file__))))
    print(digest)


if __name__ == '__main__':
    main()
