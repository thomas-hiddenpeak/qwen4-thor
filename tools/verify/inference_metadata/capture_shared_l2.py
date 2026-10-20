"""One bounded current-binary HTTP route capture; no optimization evaluation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
BASE = ROOT / '.q4t-work/shared-l2-20261020'
OUT = BASE / 'capture-v1/run'
MODEL = ROOT.parent / 'llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def identity(path):
    with path.open('rb') as f:
        return dict(path=str(path.resolve()), sha256=hashlib.file_digest(f, 'sha256').hexdigest())


def main():
    from file_cache import model_files
    from isolated_scope import IsolatedScope
    from run_budget_experiment import prepare_target_cache, payload_residual
    OUT.mkdir(parents=True, exist_ok=False)
    requests = [json.loads(line) for line in (BASE / 'capture-inputs.jsonl').read_text().splitlines()]
    plan = json.loads((BASE / 'capture-plan.json').read_text())
    if len(requests) != 2 or identity(ROOT / 'build/q4t')['sha256'] != plan['binary_sha256']:
        raise RuntimeError('frozen capture identity mismatch')
    if identity(BASE / 'capture-inputs.jsonl')['sha256'] != plan['workload_sha256']:
        raise RuntimeError('workload identity changed')
    save(OUT / 'plan.json', plan)
    save(OUT / 'sources.json', {str(p.relative_to(ROOT)): identity(p)['sha256'] for p in
        list((ROOT / 'tools/evalscope').glob('*.py')) + [Path(__file__)]})
    errors, after, attempts = prepare_target_cache(model_files(MODEL))
    save(OUT / 'cache-preparation.json', dict(errors=errors, attempts=attempts))
    if errors or payload_residual(after) != 0:
        raise RuntimeError('strict startup cache gate failed; no load')
    trace = OUT / 'trace'
    command = [str(ROOT / 'build/q4t'), 'serve', '--model-dir', str(MODEL),
        '--port', '8741', '--max-seq', '1', '--max-prefill', '8192', '--max-len', '262144',
        '--no-mtp', '--moe-resident-slots', '256', '--moe-trace-dir', str(trace),
        '--moe-trace-workload', str(BASE / 'capture-inputs.jsonl'), '--moe-trace-max-mib', '1024']
    env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    env.update(Q4T_MOE_L2_SLOTS='16', Q4T_MOE_MIRROR_K='8', Q4T_MOE_CHUNK_ORDER='0')
    state = dict(status='RUNNING', started=time.time(), completed=[])
    save(OUT / 'state.json', state)
    service = None
    try:
        service = IsolatedScope(command, cwd=ROOT, env=env, log_path=OUT / 'server.log',
            unit=f'q4t-shared-l2-{time.time_ns()}.scope', memory_max=64 << 30,
            evidence=OUT / 'scope')
        deadline = time.monotonic() + 300
        while 'serving on port' not in (OUT / 'server.log').read_text():
            if service.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('capture startup failed or timed out')
            time.sleep(.5)
        if '[trace] enabled full-request' not in (OUT / 'server.log').read_text():
            raise RuntimeError('trace capture disabled')
        for index, item in enumerate(requests):
            body = dict(model='qwen3.8-flash-next', messages=[dict(role='user', content=item['prompt'])],
                        temperature=0, max_tokens=item['max_tokens'], stream=False)
            req = urllib.request.Request('http://127.0.0.1:8741/v1/chat/completions',
                data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
            before = time.time()
            with urllib.request.urlopen(req, timeout=600) as response:
                raw = response.read()
            (OUT / f'response-{index}.json').write_bytes(raw)
            result = json.loads(raw)
            choice = result['choices'][0]
            if (choice['finish_reason'] not in ('stop', 'length') or
                    result['usage']['prompt_tokens'] <= 0 or result['usage']['completion_tokens'] <= 0):
                raise RuntimeError('invalid captured HTTP completion')
            if item.get('expected') and choice['message']['content'].strip() != item['expected']:
                raise RuntimeError('calibration retrieval quality changed')
            state['completed'].append(dict(index=index, id=item['id'], response_id=result['id'],
                usage=result['usage'], finish=choice['finish_reason'], before=before, after=time.time()))
            save(OUT / 'state.json', state)
        service.terminate()
        rc = service.wait(timeout=30)
        if rc != 0:
            raise RuntimeError('capture server shutdown failed')
        state['status'] = 'CAPTURED_NOT_PERFORMANCE_ACCEPTANCE'
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        if service:
            service.close()
        state['finished'] = time.time()
        save(OUT / 'state.json', state)
    # Bind the preselected two requests after successful complete publication.
    manifest = json.loads((trace / 'manifest.json').read_text())
    if not manifest['complete'] or manifest['requests_published'] != 2:
        raise RuntimeError('incomplete captured routing')
    old = json.loads((ROOT / '.q4t-work/offload-replay-goal-20261002/plan.json').read_text())
    source_plan = dict(source_binary=identity(ROOT / 'build/q4t'),
        checker=identity(ROOT / 'build/q4t_router_trace_check'), sort_helper=old['sort_helper'],
        requests=[dict(name=item['id'], repeats=2 if i == 0 else 1,
                       **identity(trace / f'request-{i+1}.bin')) for i,item in enumerate(requests)])
    save(BASE / 'current-source-plan.json', source_plan)
    screen = json.loads((BASE / 'plan.json').read_text())
    screen['historical_plan'] = identity(BASE / 'current-source-plan.json')
    screen['current_runtime_routes'] = True
    screen['selection'] = 'current calibration retrieval45K; preselected business join45K holdout; primary repeated counterfactually'
    save(BASE / 'current-plan.json', screen)


if __name__ == '__main__':
    main()
