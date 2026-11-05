"""Reconstruct this gate from its own SQLite responses; no model execution."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import pickle
import re
import sqlite3
import statistics
import subprocess

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('quantized_review_stage',
    Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
sha = stage.sha


def reconstruct(folder, case, quality):
    databases = list((folder / case).rglob('benchmark_data.db'))
    if len(databases) != 1:
        raise ValueError('missing/duplicate locally generated database')
    # These databases were created by this stage, never supplied remotely.
    with sqlite3.connect(databases[0]) as db:
        db.row_factory = sqlite3.Row
        records = list(db.execute('select * from result order by start_time'))
    rows = []
    for record in records:
        request = json.loads(record['request'])
        messages = pickle.loads(base64.b64decode(record['response_messages']))
        choices = [c for m in messages for c in m.get('choices', [])]
        text = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                       for c in choices)
        prompt = hashlib.sha256(request['prompt'].encode()).hexdigest()
        finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
        if not record['success'] or request.get('stream') is not True:
            raise ValueError('failed/nonstream HTTP')
        if case == 'quality':
            expected = quality[prompt]
            if (text.strip() != expected['expected'] or
                    record['prompt_tokens'] != expected['length'] or
                    finish != ['stop']):
                raise ValueError('quality/length/termination failed')
        elif (record['completion_tokens'] != 256 or finish != ['length'] or
                not 976 <= record['prompt_tokens'] <= 1024):
            raise ValueError('performance length/termination failed')
        rows.append(dict(prompt_sha256=prompt,
            output_sha256=hashlib.sha256(text.encode()).hexdigest(),
            input_tokens=record['prompt_tokens'],
            output_tokens=record['completion_tokens'], finish=finish,
            ttft=record['first_chunk_latency'], latency=record['latency']))
    if len(rows) != (11 if case == 'quality' else 3):
        raise ValueError('HTTP count differs')
    if case == 'quality' and {r['prompt_sha256'] for r in rows} != set(quality):
        raise ValueError('quality coverage differs')
    derived = json.loads((folder / case / 'result.json').read_text())['rows']
    for raw, cooked in zip(rows, derived):
        if any(raw[k] != cooked[k] for k in raw):
            raise ValueError('derived result differs from raw database')
    return rows


def review():
    base = ROOT / '.q4t-work'
    old_path = base / 'feasibility/plan.json'
    new_path = base / 'feasibility-resume/plan.json'
    old, new = [json.loads(p.read_text()) for p in (old_path, new_path)]
    if old['task_deadline_unix'] != new['task_deadline_unix']:
        raise ValueError('deadline extended')
    source_fallbacks = []
    for plan in (old, new):
        for name, digest in plan['files'].items():
            path = Path(name)
            if sha(path) == digest:
                continue
            if not path.is_relative_to(ROOT):
                raise ValueError('frozen source/dependency changed: ' + name)
            source = subprocess.check_output(['git', 'show',
                plan['source_commit'] + ':' + str(path.relative_to(ROOT))],
                cwd=ROOT)
            if hashlib.sha256(source).hexdigest() != digest:
                raise ValueError('original committed source not recoverable')
            source_fallbacks.append(str(path.relative_to(ROOT)))
        raw = Path(plan['output'])
        for name, digest in json.loads((raw / 'files.json').read_text()).items():
            if sha(raw / name) != digest:
                raise ValueError('raw evidence changed: ' + name)
    result = dict(requests=56, model_loads=4, arms={},
        decision='ENV_ABORT_MONITOR_FINALIZATION', performance_accepted=False,
        deployed=False, old_source_resolved_from_commit=source_fallbacks,
        original_deadline_unchanged=True, source_commit=new['source_commit'],
        candidate_sha256=sha(Path(new['candidate'])),
        parent_sha256=sha(Path(new['parent'])),
        plans={str(p): sha(p) for p in (old_path, new_path)})
    identity = {}
    for name, folder in [
            ('plain', Path(old['output']) / '00-plain'),
            *[(s['head'], Path(new['output']) / s['name']) for s in new['steps']]]:
        audit = stage.audit_server(folder, 14)
        performance = reconstruct(folder, 'context-1024', new['quality'])
        quality = reconstruct(folder, 'quality', new['quality'])
        identity[name] = sorted((r['prompt_sha256'], r['output_sha256'],
            r['input_tokens'], r['output_tokens'], tuple(r['finish']))
            for r in performance + quality)
        speeds = [(r['output_tokens'] - 1) / (r['latency'] - r['ttft'])
                  for r in performance]
        counts = [list(map(int, re.search(r'draft_counts=([0-9,]+)', line)[1]
                          .split(',')))
                  for line in (folder / 'server.log').read_text().splitlines()
                  if line.startswith('[mtp-accept]') and 'draft_counts=' in line]
        result['arms'][name] = dict(performance=performance, quality_passed=11,
            decode=speeds, decode_median=statistics.median(speeds),
            ttft_median=statistics.median(r['ttft'] for r in performance),
            draft_counts=counts[:3], server_quota=audit['quota'])
    for name in identity:
        result['arms'][name]['full_output_identical'] = (
            identity[name] == identity['full'])
    candidate = result['arms']['nvfp4']['decode_median']
    result['decode_gain_vs_full'] = candidate / result['arms']['full']['decode_median'] - 1
    result['decode_gain_vs_subset'] = candidate / result['arms']['subset']['decode_median'] - 1
    result['observed_target_met'] = result['decode_gain_vs_full'] >= .05
    result['observations_only'] = True
    if not result['arms']['nvfp4']['full_output_identical']:
        raise ValueError('candidate output differs')
    for directory, expected in [('http-lease', 'COMMAND_FAILED'),
                                 ('http-lease-02', 'COMPLETED')]:
        exit_path = ROOT.parent / directory / 'exit.json'
        exit_record = json.loads(exit_path.read_text())
        if exit_record['status'] != expected:
            raise ValueError('unexpected outer lease status')
        result[directory] = dict(path=str(exit_path), sha256=sha(exit_path),
                                status=expected)
    state = json.loads((Path(new['output']) / 'state.json').read_text())
    if (state['status'] != 'MEASURED_REQUIRES_REVIEW' or
            state['requests_completed'] != 42 or state['loads_started'] != 3):
        raise ValueError('remaining work did not finish')
    summary = json.loads((Path(new['output']) / 'telemetry-summary.json').read_text())
    if (summary['sampler_status'] != 'ENV_ABORT' or not state['monitor_failures'] or
            summary['exit_pending']['active'] != 1):
        raise ValueError('unexpected monitor finalization evidence')
    result['monitor'] = dict(sampler_status=summary['sampler_status'],
        failures=state['monitor_failures'], pending=summary['exit_pending'],
        flushed_samples=summary['flushed_samples'],
        note='Outer guard succeeded but inner telemetry failed after its last check; '
             'no performance acceptance or candidate NO_GO follows from this stage.')
    prior = base / 'feasibility-review.json'
    result['superseded_review'] = dict(path=str(prior), sha256=sha(prior),
        reason='First review omitted final sampler status; raw record retained.')
    output = base / 'feasibility-final-review.json'
    with output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(output)


if __name__ == '__main__':
    review()
