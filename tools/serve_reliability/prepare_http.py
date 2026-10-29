"""Freeze one 4-hour serving reliability run after builds and CPU checks."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-reliability-20261030'
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/'
             'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def assemble():
    prior = json.loads((BASE/'baseline/q4t.release.json').read_text())
    fixtures = BASE/'fixtures/quality'
    references = json.loads((fixtures/'results.json').read_text())
    prompts = [json.loads(line) for line in
               (fixtures/'inputs/requests.jsonl').read_text().splitlines()]
    quality = {}
    for length in [1024, 8192, 45056]:
        row = next(r for r in references if r['actual_input'] == length)
        prompt = next(p['prompt'] for p in prompts if
            hashlib.sha256(p['prompt'].encode()).hexdigest() == row['prompt_sha256'])
        quality[str(length)] = dict(prompt=prompt, expected=row['expected'],
                                   prompt_sha256=row['prompt_sha256'])
    decode = json.loads((BASE/'fixtures/performance/inputs/context-1024.jsonl')
                        .read_text().splitlines()[0])['prompt']
    cache = (ROOT/'.q4t-work/session-incremental-delivery-20261029/frozen/'
             'health-prompt.txt').read_text()
    steps = []
    for binary in ['parent', 'candidate']:
        for mode in ['plain', 'full', 'subset']:
            steps.append(dict(name=binary+'-'+mode, kind='quality', binary=binary,
                              mode=mode, requests=6))
    for i, (point, mode) in enumerate([
        ('handler.before_register', 'full'), ('handler.after_slot', 'subset'),
        ('handler.after_active', 'plain'), ('handler.before_content', 'full'),
        ('scheduler.after_select', 'subset'), ('scheduler.after_submit', 'plain')]):
        scheduler = point.startswith('scheduler.')
        steps.append(dict(name='fault-'+point.replace('.', '-'), kind='fault',
            binary='fault', mode=mode, point=point, exit=int(scheduler), slow=i<3,
            requests=(1 if scheduler else 2)+(6 if i<3 else 0),
            environment=dict(Q4T_SERVE_FAULT=point, Q4T_SERVE_TEST_SNDBUF='1',
                             Q4T_SERVE_OBSERVE='write.backpressure')))
    for mode in ['full', 'subset']:
        for incremental in [0, 1]:
            steps.append(dict(name=f'cache-{mode}-{incremental}', kind='cache',
                binary='fault', mode=mode, requests=5,
                environment=dict(Q4T_SESSION_INCREMENTAL=str(incremental))))
    for timing in [0, 1]:
        steps.append(dict(name='stats-'+str(timing), kind='stats',
            binary='candidate', mode='plain', timing=timing, requests=4,
            environment=dict(Q4T_RESIDENCY_TIMING=str(timing))))
    return dict(version=1, model=str(MODEL), artifact=prior['subset_artifact'],
        quality=quality, decode_prompt=decode, cache_prompt=cache, steps=steps,
        port=8764, maximum_http=110, maximum_starts=23,
        planned_measurement_http=sum(s['requests'] for s in steps),
        planned_measurement_starts=len(steps), health_http=6, health_starts=2,
        performance_acceptance=False,
        output=str(BASE/'http'), parent=str(BASE/'frozen/parent'),
        candidate=str(BASE/'frozen/candidate'), fault=str(BASE/'frozen/fault'),
        limits=['fault executable has deterministic injection/gates only',
                'resource observations are not SSD capacity acceptance',
                '4 extra HTTP/3 starts only for an identified failure retest'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true')
    args = parser.parse_args()
    plan = assemble()
    # Verify actual token counts without model loading or CUDA API calls.
    strings = [plan['quality'][str(n)]['prompt'] for n in [1024, 8192, 45056]]
    strings += [plan['decode_prompt'], plan['cache_prompt']]
    probe = subprocess.run([str(BASE/'tokenizer-candidate'), str(MODEL/'tokenizer.json')],
        input=''.join(json.dumps(s, ensure_ascii=False)+'\n' for s in strings),
        text=True, capture_output=True, check=True, timeout=60)
    counts = [len(json.loads(line)) for line in probe.stdout.splitlines()]
    if counts[:4] != [1024, 8192, 45056, 1024] or not 8192 < counts[4] < 16384:
        raise AssertionError(('frozen fixture token counts', counts))
    plan['fixture_token_counts'] = counts
    if not args.freeze:
        save(BASE/'http-draft.json', plan)
        print(json.dumps(dict(http=plan['planned_measurement_http'],
                             starts=plan['planned_measurement_starts'], counts=counts)))
        return
    frozen = BASE/'frozen'
    frozen.mkdir(exist_ok=False)
    for source, name in [('baseline/q4t', 'parent'), ('build/q4t', 'candidate'),
                         ('fault-build/q4t', 'fault'), ('build/CMakeCache.txt',
                          'CMakeCache.txt'), ('fault-build/CMakeCache.txt',
                          'FaultCMakeCache.txt')]:
        shutil.copy2(BASE/source, frozen/name)
    runtime = {str(p.relative_to(ROOT)):sha(p) for base in ['src', 'include']
               for p in (ROOT/base).rglob('*') if p.is_file()}
    save(frozen/'runtime-source.json', runtime)
    files = [p for p in frozen.iterdir() if p.is_file()]
    files += [ROOT/p for p in runtime]
    files += [ROOT/'CMakeLists.txt', Path(__file__),
              ROOT/'tools/evalscope/serve_reliability.py',
              ROOT/'tools/evalscope/gpu_exclusive.py',
              ROOT/'tools/evalscope/nvml_process_query.py',
              ROOT/'tools/evalscope/monitor_memory.py',
              ROOT/'tools/evalscope/resource_metrics.py',
              ROOT/'docs/SERVE_RELIABILITY_PLAN_2026-10-30.md']
    files += [MODEL/n for n in ['config.json', 'tokenizer.json',
                                'model.safetensors.index.json']]
    files += [Path(plan['artifact'])/n for n in ['head.bf16', 'token_ids.bin', 'manifest.json']]
    plan['files'] = {str(p):sha(p) for p in files}
    plan['source_commit'] = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    plan['started_unix'] = time.time()
    plan['deadline_unix'] = plan['started_unix']+4*3600
    save(BASE/'http-plan.json', plan)
    save(BASE/'budget.json', {k:plan[k] for k in ['started_unix', 'deadline_unix',
         'maximum_http', 'maximum_starts', 'planned_measurement_http',
         'planned_measurement_starts', 'health_http', 'health_starts']})
    print(json.dumps(dict(plan=str(BASE/'http-plan.json'),
                         sha256=sha(BASE/'http-plan.json'), counts=counts)))


if __name__ == '__main__':
    main()
