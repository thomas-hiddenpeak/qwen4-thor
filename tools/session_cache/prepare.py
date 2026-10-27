"""Freeze A identities/material/order on CPU; this does not execute tests."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from run_stage import ROOT, reuse, save


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    from transformers import AutoTokenizer
    base = ROOT / '.q4t-work/session-cache-20261027'
    frozen = base / 'phase-a'
    frozen.mkdir()
    (frozen / 'programs').mkdir()
    old = json.loads((ROOT / '.q4t-work/suffix-agent-20261027/acceptance/frozen-plan.json').read_text())
    for name in ['q4t', 'q4t_session_storage_contract']:
        shutil.copy2(base / 'build' / name, frozen / 'programs' / name)
    model = old['model']
    tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
    start = '<|im_start|>user\n<archive>\n'
    end = ('\n</archive>\nOutput the following Python function repeatedly, '
           'without markdown fences, until asked to stop:\n'
           'def greet(name):\n    return "Hello, " + name\n'
           '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')
    records = []
    for i in range(1000):
        records.append(f'Archived record {i}: the agent waits for a tool and then continues its task.\n')
        prompt = start + ''.join(records) + end
        length = len(tokenizer.encode(prompt, add_special_tokens=False))
        if length >= 9216:
            break
    (frozen / 'session-prompt.txt').write_text(prompt)
    qcase = dict(old['steps'][0]['cases'][0])
    qsource = Path(qcase['path'])
    shutil.copy2(qsource, frozen / 'quality.jsonl')
    qcase['path'] = str(frozen / 'quality.jsonl')
    rows = [json.loads(x) for x in qsource.read_text().splitlines()]
    short = min(rows, key=lambda r: len(r['prompt']))['prompt']
    now = time.time()
    plan = dict(stage='SESSION_A', output=str(frozen / 'run'),
        candidate=str(frozen / 'programs/q4t'),
        candidate_sha256=sha(frozen / 'programs/q4t'),
        storage_contract=str(frozen / 'programs/q4t_session_storage_contract'),
        model=model, artifact=str(ROOT / '.q4t-work/suffix-agent-delivery-20261027/package-v2/artifact'),
        parent=str(ROOT / 'build/q4t'), parent_sha256=sha(ROOT / 'build/q4t'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        model_file_metadata=old['model_file_metadata'],
        http_requests=36, http_loads=2, maximum_http_requests=240,
        maximum_starts=14, maximum_gpu_contracts=4,
        stage_timeout_seconds=3600, task_deadline_unix=now+21600,
        direct_contract_timeout_seconds=600, port=8762,
        quality=old['quality'], quality_case=qcase,
        session_prompt=str(frozen / 'session-prompt.txt'),
        session_prompt_tokens=length, short_prompt=short,
        telemetry_sources=reuse.telemetry.discover_sources(),
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=10,
        cache='No cache drop; service process starts fresh, SSD cache empty. No warmup requests.',
        order=['full:11 HTTP quality, CPU storage,7 lifecycle', 'subset:11 HTTP quality,7 lifecycle'],
        steps=[], files={})
    path = frozen / 'plan.json'
    for head in ['full', 'subset']:
        name = head + '-http'
        plan['steps'].append(dict(name=name, head=head, kind='block',
            http_requests=18, http_loads=1, timeout=1800,
            command=[sys.executable, '-B', str(ROOT/'tools/session_cache/run_stage.py'),
                     '--plan', str(path), '--custom-step', name]))
    files = [p for group in ['src', 'include'] for p in (ROOT/group).rglob('*') if p.is_file()]
    files += [ROOT/'CMakeLists.txt', ROOT/'docs/SESSION_CACHE_PLAN_2026-10-27.md']
    files += [p for group in ['session_cache','mtp_suffix_multi','mtp_suffix_lazy',
        'mtp_suffix_zero_hit','mtp_suffix_admission','mtp_suffix','evalscope']
        for p in (ROOT/'tools'/group).glob('*.py')]
    files += list((frozen/'programs').iterdir()) + [frozen/'quality.jsonl', frozen/'session-prompt.txt']
    files += [Path(plan['artifact'])/name for name in ['head.bf16','token_ids.bin','manifest.json']]
    plan['files'] = {str(p):sha(p) for p in files}
    save(path, plan)
    (frozen/'source.diff').write_bytes(subprocess.check_output(['git','diff','HEAD'], cwd=ROOT))
    save(frozen/'identity.json', dict(candidate_sha256=plan['candidate_sha256'],
         parent_sha256=plan['parent_sha256'], files=len(plan['files']),
         plan_sha256=sha(path), session_prompt_tokens=length, created=now))
    print(json.dumps(json.loads((frozen/'identity.json').read_text()), indent=2))


if __name__ == '__main__':
    main()
