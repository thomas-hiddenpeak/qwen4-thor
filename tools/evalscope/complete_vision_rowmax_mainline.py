"""Qualify the final row-max/mainline combination without reusing old E2E."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess

from tools.evalscope import resume_vision_rowmax as retained

prior = retained.prior
s = retained.s
BASE = s.ROOT/'.q4t-work/vision-rowmax-mainline-20261107'
FOLDER = BASE/'accept-02'
PARENT = s.ROOT/'.q4t-work/serve-closeout-20261107/runtime/q4t'
PARENT_COMMIT = 'fad0e9292df79f95ecc7540e4e9a88281251e8e5'
PARENT_SHA = '478a325e3b7baaee90f96381468be17fab2511f861b9f92353f84ed9564ceb54'
LIMITS = dict(starts=4, generations=104, controls=64, native=0, seconds=7200)
GROUPS = prior.GROUPS


def numerical_reuse():
    # Old binaries and their archived source stay bound. Their HTTP timings are
    # not assigned to the newly compiled mainline combination.
    old = retained.verify_reuse(archive_only=True)
    plan = retained.read(prior.FOLDER/'plan.json')
    math_sources = {}
    for name, digest in plan['files'].items():
        path = Path(name)
        if not path.is_relative_to(s.ROOT):
            continue
        relative = str(path.relative_to(s.ROOT))
        if relative.startswith(('src/vision/', 'src/quant/', 'include/q4t/quant/',
                                'include/q4t/model/vision')):
            s.require(s.sha(path) == digest, 'numerical dependency drift: '+relative)
            math_sources[relative] = digest
    s.require('src/vision/vision.cu' in math_sources and len(math_sources) >= 10,
              'missing numerical source inventory')
    return dict(archive=old, numerical_sources=math_sources,
        reused_lifetimes=['kernel', 'hf-small', 'hf-rectangular'],
        reused_http=False,
        reason='serve sources changed; run both modes/five tiers on the final pair')


def prepare():
    s.require(not FOLDER.exists(), 'preserve prior attempt')
    s.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=s.ROOT),
              'commit final source before freezing')
    diff = subprocess.check_output(['git', 'diff', '--name-only', PARENT_COMMIT, 'HEAD',
        '--', 'CMakeLists.txt', 'src', 'include', 'cmake'], cwd=s.ROOT, text=True).splitlines()
    s.require(diff == ['src/vision/vision.cu'], 'final runtime delta is not only row-max')
    s.require(s.sha(PARENT) == PARENT_SHA, 'accepted mainline parent changed')
    reused = numerical_reuse()
    old = retained.read(prior.FOLDER/'plan.json')
    FOLDER.mkdir(mode=0o700)
    s.save(FOLDER/'reuse.json', reused)
    inputs = FOLDER/'inputs'
    inputs.mkdir()
    cases = copy.deepcopy(old['cases'])
    for case in cases:
        source = Path(case['path'])
        s.require(s.sha(source) == old['files'][str(source)], 'old fixture changed')
        destination = inputs/source.name
        shutil.copyfile(source, destination)
        case['path'] = str(destination)
    paths = [s.ROOT/name for name in subprocess.check_output(
        ['git', 'ls-files'], cwd=s.ROOT, text=True).splitlines()
        if name.startswith(('src/', 'include/', 'cmake/', 'tools/', 'tests/'))
        or name == 'CMakeLists.txt']
    for engine in (BASE/'engine', PARENT.parent):
        paths += [engine/name for name in ('q4t', 'CMakeCache.txt',
            'generated/q4t/server/chat_protocol_sources.json')]
        # Both accepted builds used Makefiles without exporting a command DB.
        # Bind the actual generated compile/link recipes; never reconstruct an
        # absent database and present it as original build evidence.
        targets = ['q4t', 'q4t_model', 'q4t_text', 'q4t_ple', 'q4t_quant',
                   'q4t_server', 'q4t_startup_plan', 'q4t_trace', 'q4t_io',
                   'q4t_runtime']
        directories = [engine/'CMakeFiles'/(name+'.dir') for name in targets]
        directories += [engine/'_deps/q4t_xgrammar-build/CMakeFiles/xgrammar.dir']
        for directory in directories:
            paths += [directory/name for name in ('flags.make', 'link.txt', 'build.make')]
            paths += sorted(directory.glob('*.rsp'))
    paths += [FOLDER/'reuse.json', prior.FOLDER/'plan.json', BASE/'build.json',
        s.ROOT/'docs/VISION_ROWMAX_MAINLINE_2026-11-07.md',
        s.ROOT/'build/q4t', s.ROOT/'build/q4t.release.json', prior.pilot.EVALSCOPE]
    paths += list(inputs.iterdir())+list((BASE/'logs').iterdir())
    package = prior.pilot.EVALSCOPE.parent.parent/'lib/python3.12/site-packages/evalscope'
    paths += sorted(package.rglob('*.py'))
    paths += [s.MODEL/name for name in ('config.json', 'tokenizer.json',
                                      'model.safetensors.index.json', 'preprocessor_config.json')]
    lock = (s.ROOT/'.q4t-work/gpu-exclusive.lock').stat()
    binaries = {arm:dict(path=str(path), sha256=s.sha(path)) for arm,path in
        (('parent', PARENT), ('candidate', BASE/'engine/q4t'))}
    s.save(FOLDER/'plan.json', dict(profile='vision-rowmax-mainline-completion-v1',
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=s.ROOT,text=True).strip(),
        parent_commit=PARENT_COMMIT, runtime_diff=diff, limits=LIMITS, groups=GROUPS,
        cases=cases, quality=old['quality'], binaries=binaries, model=str(s.MODEL),
        port=s.PORT, lock_device=lock.st_dev, lock_inode=lock.st_ino,
        reuse=reused, files={str(p):s.sha(p) for p in paths},
        default_unchanged=True, performance_acceptance=False))
    return dict(status='PREPARED_FINAL_MAINLINE_PAIR', binaries=binaries, limits=LIMITS)


class Run(prior.Run):
    limits = LIMITS
    groups = GROUPS

    def contracts(self):
        s.require(self.plan['reuse']['reused_http'] is False and
            self.plan['reuse']['numerical_sources']['src/vision/vision.cu'] ==
            s.sha(s.ROOT/'src/vision/vision.cu'), 'numerical reuse identity')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','run','verify-reuse'))
    action = parser.parse_args().action
    value = {'prepare':prepare, 'run':lambda:Run(FOLDER).run(),
             'verify-reuse':numerical_reuse}[action]()
    print(json.dumps(value, ensure_ascii=False))
