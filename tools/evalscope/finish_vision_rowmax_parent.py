"""Complete only the interrupted parent; retain three closed lifetimes."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess

from tools.evalscope import complete_vision_rowmax_mainline as original
from tools.evalscope import audit_vision_rowmax_completion as audit

s, read = original.s, original.retained.read
BASE = original.BASE
FOLDER = BASE/'parent-completion-01'
LIMITS = dict(starts=1, generations=26, controls=16, native=0, seconds=1200)
GROUPS = ('mtp-parent',)
REUSED = original.GROUPS[:3]


def prefix():
    archived = original.retained.verify_archive(
        BASE/'aborted-originals.json', BASE/'aborted-originals')
    plan = read(original.FOLDER/'plan.json')
    for name, digest in archived.items():
        if Path(name).is_relative_to(original.FOLDER):
            s.require(s.sha(name) == digest, 'aborted original changed')
    s.require(read(original.FOLDER/'lease/exit.json')['status'] == 'ENV_ABORT'
        and read(original.FOLDER/'execution/result.json')['status'] == 'FAILED',
        'original failure must remain failed')
    sources = audit.source_identity(plan)
    events = original.retained.jsonlines(original.FOLDER/'lease/events.jsonl')
    queries = original.retained.jsonlines(original.FOLDER/'lease/nvml-queries.jsonl')
    out = original.FOLDER/'execution'
    lifetimes = {g:original.retained.verify_lifetime(out, 'model-'+g, events, queries)
                 for g in REUSED}
    failed = [q for q in queries if q['status'] != 'OK']
    s.require(len(failed) == 1 and failed[0]['status'] == 'CLEANUP_FAILED',
              'unexpected original failure scope')
    failure_begin = json.loads(failed[0]['stdout'].splitlines()[0])['monotonic']
    s.require(max(v['through_monotonic'] for v in lifetimes.values()) < failure_begin,
              'reused lifetime overlaps failed observation')
    audit.matrix(plan, out, REUSED)
    return dict(groups=list(REUSED), lifetimes=lifetimes, git_sources=sources,
        requests=78, excluded=['original mtp-parent, including its performance rows'],
        original_stage_status='ENV_ABORT', original_query_failure=failed[0],
        originals_sha256=s.sha(BASE/'aborted-originals.json'))


def prepare():
    s.require(not FOLDER.exists(), 'preserve prior completion attempt')
    s.require(not subprocess.check_output(['git','status','--porcelain'],cwd=s.ROOT),
              'commit completion source before freeze')
    reused = prefix()
    old = read(original.FOLDER/'plan.json')
    s.require(all(s.sha(v['path']) == v['sha256'] for v in old['binaries'].values()),
              'tested final pair changed')
    FOLDER.mkdir(mode=0o700)
    inputs = FOLDER/'inputs'
    inputs.mkdir()
    cases = copy.deepcopy(old['cases'])
    for case in cases:
        source = Path(case['path'])
        s.require(s.sha(source) == old['files'][str(source)], 'fixture drift')
        target = inputs/source.name
        shutil.copyfile(source,target)
        case['path'] = str(target)
    s.save(FOLDER/'prefix.json',reused)
    paths = {Path(p) for p in old['files']}
    paths.update(s.ROOT/n for n in subprocess.check_output(
        ['git','ls-files'],cwd=s.ROOT,text=True).splitlines()
        if n.startswith(('tools/','src/','include/','cmake/','tests/')))
    paths.update(inputs.iterdir())
    paths.update((FOLDER/'prefix.json', original.FOLDER/'plan.json',
        BASE/'aborted-originals.json', BASE/'acceptance-abort.json',
        s.ROOT/'docs/VISION_ROWMAX_PARENT_COMPLETION_2026-11-07.md'))
    lock=(s.ROOT/'.q4t-work/gpu-exclusive.lock').stat()
    s.save(FOLDER/'plan.json',dict(profile='vision-rowmax-final-parent-completion-v1',
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=s.ROOT,text=True).strip(),
        runtime_source_commit=old['source_commit'], parent_commit=old['parent_commit'],
        limits=LIMITS,groups=GROUPS,cases=cases,quality=old['quality'],
        binaries=old['binaries'],model=old['model'],port=old['port'],
        lock_device=lock.st_dev,lock_inode=lock.st_ino,
        reuse=original.numerical_reuse(),completed_prefix=reused,
        files={str(p):s.sha(p) for p in paths},default_unchanged=True,
        performance_acceptance=False))
    return dict(status='PREPARED_INTERRUPTED_PARENT_ONLY',limits=LIMITS,reused_requests=78)


class Run(original.Run):
    limits=LIMITS
    groups=GROUPS
    modes=()  # The offline audit compares this group against the sealed prefix.


if __name__ == '__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','verify-prefix'))
    action=parser.parse_args().action
    print(json.dumps({'prepare':prepare,'run':lambda:Run(FOLDER).run(),
                      'verify-prefix':prefix}[action](),ensure_ascii=False))
