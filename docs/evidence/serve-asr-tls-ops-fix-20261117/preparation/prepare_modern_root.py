"""Keep the accepted modern controller, adding the exact ASR-qualified payload."""
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

W = Path(__file__).resolve().parent
PRIMARY = W / 'package-01/bundle'
OUT = W / 'modern-retained-01'
PREVIOUS = ROOT / '.q4t-work/serve-video-projective-20261117/modern-retained-01/bundle'
PREVIOUS_SHA = '51a6c274068a6c99e27153422743d033b1ea2882eb57edeb1734e9acf4bd1bfb'


def main():
    os.umask(0o077)
    protected = {n: p.sha(ROOT/n) for n in
        ['build/q4t', 'build/q4t.release.json', 'build/service/manifest.json',
         'build/service.release.json', 'build/service-native-state/ledger.json']}
    current = bundle.verify(PRIMARY, p.sha(PRIMARY/'manifest.json'))
    old = bundle.verify(PREVIOUS, PREVIOUS_SHA)
    p.require(not OUT.exists(), 'preserve previous modern root')
    rel = 'source/tools/serve_recovery/controller.py'
    p.require(current['version'] == 20 and old['version'] == 19 and
              current['files'][rel] != old['files'][rel],
              'distinct accepted modern control required')
    OUT.mkdir(mode=0o700)
    target = OUT/'bundle'
    shutil.copytree(PRIMARY, target, symlinks=True)
    manifest = copy.deepcopy(current)
    path = target/rel
    path.chmod(0o600)
    shutil.copyfile(PREVIOUS/rel, path)
    path.chmod(0o400)
    manifest['files'][rel] = p.sha(path)
    path = target/'manifest.json'
    path.chmod(0o600)
    p.save(path, manifest)
    path.chmod(0o400)
    digest = p.sha(path)
    path = target/'bin/q4t'
    path.chmod(0o700)
    path.write_bytes(bundle.launcher(digest, manifest['python']['path']))
    path.chmod(0o500)
    p.require(bundle.verify(target, digest) == manifest, 'modern retained verification failed')
    p.require({n for n,h in manifest['files'].items() if current['files'].get(n) != h} == {rel},
              'unexpected modern retained change')
    descriptor = dict(package=str(target), manifest_sha256=digest)
    legacy = p.read(W/'retained-01/review.json')['fallback']
    p.require(version_contract.compatible(version_contract.payload(descriptor),
                                         version_contract.payload(legacy))['automatic'],
              'automatic payload compatibility changed')
    p.save(OUT/'receipt.json', descriptor)
    p.save(OUT/'review.json', dict(status='EXACT_MODERN_ROOT_BUSINESS_PENDING',
        previous=dict(package=str(PREVIOUS), manifest_sha256=PREVIOUS_SHA),
        candidate=dict(package=str(PRIMARY), manifest_sha256=p.sha(PRIMARY/'manifest.json')),
        changed_from_candidate=[rel], exact_previous_controller_sha256=old['files'][rel],
        engine_and_native_and_profiles_identical_to_candidate=True,
        original_acceptance_not_relabelled=True, new_gpu_starts=0, default_unchanged=True))
    p.require(all(p.sha(ROOT/n) == h for n,h in protected.items()), 'default changed')
    print(digest)


if __name__ == '__main__':
    main()
