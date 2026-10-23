"""Freeze one 26-request diagnostic; no model execution or live telemetry."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import telemetry

OLD = ROOT / '.q4t-work/suffix-admission-20261024'
CANDIDATE_SHA = '6a4d3d62c09d814a9342cc698e1523e1029d1d3aa3e36e1b80cf7a625a30b090'


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(path, obj):
    with path.open('x') as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8756)
    args = parser.parse_args()
    stage = args.stage.resolve()
    if not stage.is_relative_to(ROOT / '.q4t-work') or stage.exists():
        parser.error('use a new stage inside .q4t-work')
    old_plan_path = OLD / 'acceptance-v2/frozen-plan.json'
    old = json.loads(old_plan_path.read_text())
    analysis = json.loads((OLD / 'analysis.json').read_text())
    candidate = Path(old['candidate'])
    if sha(candidate) != CANDIDATE_SHA:
        raise RuntimeError('previous candidate identity changed')
    # Reuse requires unchanged runtime, tools, contracts, artifacts and inputs.
    # Only documentation has intentionally advanced since the old freeze.
    for name, digest in old['files'].items():
        p = Path(name)
        if p.is_relative_to(ROOT / 'docs'):
            continue
        if sha(p) != digest:
            raise RuntimeError('reused dependency differs from old freeze: ' + name)
    stage.mkdir()
    fixtures = stage / 'fixtures'
    fixtures.mkdir()
    cases = []
    ids = ['context-8192', 'context-45056', 'context-204800', 'repeat-code', 'repeat-text']
    for name in ids:
        original = next(c for c in old['performance'] + old['scenarios'] if c['id'] == name)
        c = dict(original)
        path = fixtures / (name + '.jsonl')
        source = Path(c['path'])
        path.write_text(source.read_text().splitlines()[0] + '\n')
        c.update(path=str(path), source_path=str(source), source_sha256=sha(source),
                 role='development_diagnostic', repeats=1)
        cases.append(c)
    save(fixtures / 'manifest.json', cases)
    output = stage / 'run'
    plan = dict(
        version=1, classification='bounded zero-hit causal diagnosis; no acceptance throughput claim',
        candidate=str(candidate), candidate_sha256=CANDIDATE_SHA,
        candidate_source_commit=old['candidate_source_commit'],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        model=old['model'], artifact=old['artifact'],
        subset_manifest_sha256=old['subset_manifest_sha256'],
        parent=str(ROOT/'build/q4t'), parent_sha256=old['parent_sha256'],
        output=str(output), lease=str(stage/'lease'), port=args.port,
        stage_timeout_seconds=3600, direct_contract_timeout_seconds=0,
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=5,
        http_requests=26, http_loads=6, retries=0, warmups=0,
        global_task_timeout_seconds=25200,
        global_deadline_rule='first stage state.started + 25200; includes development/build and gaps before conditional acceptance',
        conditional_acceptance_max_requests=346, conditional_acceptance_max_loads=18,
        conditional_health_max_requests=3, conditional_health_max_loads=3,
        task_max_requests=375, task_max_loads=27,
        candidate_limit=1, parameter_scan=False,
        scope='B1 greedy MTP k3; original min32/numeric/global-ID/verify/extend contracts unchanged',
        cache='natural inherited page cache, startup auto; no manual warmup, drop_caches, affinity or power changes',
        first_test='one uninstrumented evalscope HTTP request; stage-phase timing only after success',
        interpretation='A/A is a descriptive same-identity control, not a universal noise tolerance. Timing services are diagnostic only; 0.1ms phase rounding and whole-generation windows must be retained.',
        stop='first identity/output/HTTP/cleanup failure, timeout or ENV_ABORT; no runtime change or retry in diagnostic stage',
        exit='analyze only after complete shutdown; select at most one evidence-supported local runtime candidate or close diagnosis without fabricating a fix',
        telemetry_sources=telemetry.discover_sources(),
        steps=[], files={}, model_file_metadata=old['model_file_metadata'],
        inherited_contracts=dict(path=str(OLD/'audit-evidence.json'), sha256=sha(OLD/'audit-evidence.json'),
            quality=22, host=86, stage_suite=10,
            scope='same program/dependencies; direct oracle original two-GDN-layer target scope only; no rerun implied'))
    runner = ROOT / 'tools/mtp_suffix_zero_hit/run_diagnostic.py'
    for name, head, suffix, timing in [
        ('first-http','full',True,False), ('subset-off-a1','subset',False,True),
        ('subset-on-b','subset',True,True), ('subset-off-a2','subset',False,True),
        ('full-on','full',True,True), ('full-off','full',False,True)]:
        chosen = [dict(c) for c in (cases if timing else cases[:1])]
        for c in chosen:
            comparison = analysis['paired_comparisons'][head][c['id']]
            if not comparison['all_blocks_output_identity_equal']:
                raise RuntimeError('old reference output is not stable')
            # Select the matching head and original OFF arm; all original arms
            # were checked equal, so this does not select a favorable output.
            raw = next(e for e in analysis['protections']['entries']
                       if e['head']==head and e['case']==c['id'] and e['block']==1)
            c['expected_output_sha256'] = raw['off_first_request']['output_sha256']
        plan['steps'].append(dict(name=name, head=head, suffix=suffix, timing=timing,
            kind='timing' if timing else 'block', cases=chosen, block=None,
            timeout=900, http_loads=1, http_requests=len(chosen),
            command=[sys.executable,'-B',str(runner),'--plan',str(stage/'frozen-plan.json'),
                     '--custom-step',name]))
    paths = set(map(Path, old['files']))
    paths |= {old_plan_path, OLD/'analysis.json', OLD/'audit-evidence.json',
              ROOT/'docs/MTP_SUFFIX_ZERO_HIT_2026-10-24.md', ROOT/'docs/STATUS.md',
              ROOT/'docs/log/2026-10-24.md'}
    paths |= {p for p in (ROOT/'tools/mtp_suffix_zero_hit').rglob('*')
              if p.is_file() and '__pycache__' not in p.parts}
    paths |= set(fixtures.glob('*'))
    paths |= set(stage.parent.glob('diagnosis-*'))
    paths.add(stage.parent/'eval-design.md')
    snapshot = stage/'source-snapshot'
    snapshot.mkdir()
    for path in sorted(paths):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target = snapshot/path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path,target)
    (snapshot/'worktree.patch').write_bytes(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT))
    plan['source_snapshot']=str(snapshot)
    save(stage/'frozen-plan.json',plan)
    print(json.dumps(dict(plan=str(stage/'frozen-plan.json'), sha256=sha(stage/'frozen-plan.json'),
                         candidate=CANDIDATE_SHA, requests=26, starts=6),indent=2))


if __name__ == '__main__':
    main()
