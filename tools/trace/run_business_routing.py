"""Collect routing traces for the frozen business set v1 (2026-09-30).

Executes the NEW entries of prepare_business_set.py through the local
service with moe-trace enabled; reuse entries are identity-verified only.
Runs are split by trace byte quota (server cap 4096 MiB):
  policy     - multi-turn, task-switching, long-output, quality 8K
  fv-long    - quality 44K + 8K(final) + acceptance tier 261888
  fv-200k    - quality 200K
Multi-turn entries run sequentially; the assistant history is the actual
greedy output of the previous turn (recorded in realized.json).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from analyze import sha
from prepare_business_set import ROOT

RUNS = {
    'policy': lambda e: e['split'] == 'policy' and e['mode'] != 'reuse',
    'fv-long': lambda e: e['split'] == 'final_validation'
    and e['id'] != 'accept-261888' and e.get('length') != 204800,
    'fv-200k': lambda e: e.get('length') == 204800,
}
TRACE_QUOTA_MIB = 3584


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def verify_reuse(entries, out):
    """Bind reuse entries to their existing traces by SHA + identity."""
    checked = []
    for e in entries:
        if e['mode'] != 'reuse':
            continue
        trace = Path(e['trace_dir']) / f"request-{e['trace_request']}.bin"
        meta = Path(e['trace_dir']) / f"request-{e['trace_request']}.json"
        if not trace.is_file():
            raise RuntimeError(f"missing reuse trace: {trace}")
        record = json.loads(meta.read_text())
        checked.append(dict(
            id=e['id'], trace=str(trace), trace_sha256=sha(trace),
            prompt_tokens=record['prompt_tokens'],
            request_id=record['request_id']))
    return checked


def chat_request(messages, max_tokens):
    return dict(messages=messages,
                chat_template_kwargs=dict(enable_thinking=False),
                temperature=0, max_tokens=max_tokens, stream=True,
                stream_options=dict(include_usage=True))


def post_chat(port, payload, timeout=3600):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}/v1/chat/completions', data=body,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode()
    if resp.status != 200:
        raise RuntimeError(f'HTTP {resp.status}: {text[:200]}')
    content, usage, finish = [], None, None
    for line in text.splitlines():
        if not line.startswith('data: '):
            continue
        data = line[6:]
        if data == '[DONE]':
            break
        chunk = json.loads(data)
        choices = chunk.get('choices') or []
        if choices:
            delta = choices[0].get('delta') or {}
            if delta.get('content'):
                content.append(delta['content'])
            if choices[0].get('finish_reason'):
                finish = choices[0]['finish_reason']
        if chunk.get('usage'):
            usage = chunk['usage']
    return dict(text=''.join(content), usage=usage, finish=finish)


def generate_accept_prompt(model_dir):
    """prepare_inputs.py algorithm, seed 20260920, length 261888."""
    import random
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir),
                                              local_files_only=True)
    notes = [
        'The service reads model weights and updates the sequence state on each step. '
        'The team records latency and checks the generated answer before accepting a change.',
        'The request contains technical notes from several experiments. '
        'Input length, output length, and concurrency must remain fixed when comparing runs.',
        'The storage worker reads selected rows from the lookup table. '
        'The table is much larger than the small buffer used by one request.',
        'A short request and a long request can follow different execution paths. '
        'The report must distinguish the time to the first answer from the later generation rate.',
        'A cache can reuse recently accessed data. '
        'An estimate of required bytes is different from a measurement of physical memory traffic.',
        'The scheduler tracks each request separately. '
        'It must preserve the correct state when a request finishes or another request arrives.',
        'A useful change can reduce code complexity while keeping the same accuracy and speed. '
        'A faster result on one input does not justify a regression on another input.',
        'The evaluation stores the complete request and response with the build identifier. '
        'The next run uses the same data so that the results remain comparable.',
    ]
    prefix = 'Technical review notes. Read the following records carefully.\n'
    suffix = ('\nTask: Write a detailed technical review of these records in at least '
              '500 words. Explain measurement, storage, scheduling, and correctness.\nAnswer:')
    length = 261888
    rng = random.Random(20260920)
    records = []
    while len(records) < length // 25 + 16:
        records.append(f'Record {len(records) + 1}: {rng.choice(notes)}\n')
    body_ids = tokenizer.encode(''.join(records), add_special_tokens=False)
    budget = length - len(tokenizer.encode(prefix + suffix,
                                           add_special_tokens=False))
    for _ in range(16):
        prompt = prefix + tokenizer.decode(body_ids[:budget]) + suffix
        count = len(tokenizer.encode(prompt, add_special_tokens=False))
        if count == length:
            break
        budget += length - count
    else:
        raise RuntimeError('could not prepare the acceptance prompt')
    return prompt


def wait_ready(log_path, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if 'serving on port' in log_path.read_text():
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError('server startup timeout')


def run_entries(name, entries, args, out):
    run_dir = out / name
    run_dir.mkdir(exist_ok=False)
    # Freeze the run workload BEFORE the server starts (trace identity).
    workload = dict(run=name, entries=[e['id'] for e in entries],
                    conditions=dict(max_seq=1, max_prefill=8192,
                                    max_len=262144, mtp=False,
                                    enable_thinking=False))
    (run_dir / 'workload.json').write_text(
        json.dumps(workload, indent=1, ensure_ascii=False))
    server_cmd = [str(args.binary.resolve()), 'serve',
                  '--model-dir', str(args.model_dir.resolve()),
                  '--port', str(args.port), '--max-seq', '1',
                  '--max-prefill', '8192', '--max-len', '262144',
                  '--no-mtp', '--moe-trace-dir', str(run_dir / 'trace'),
                  '--moe-trace-workload', str(run_dir / 'workload.json'),
                  '--moe-trace-max-mib', str(TRACE_QUOTA_MIB)]
    (run_dir / 'server-command.json').write_text(
        json.dumps(server_cmd, indent=1))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    with (run_dir / 'server.log').open('w') as log:
        server = subprocess.Popen(server_cmd, cwd=ROOT, env=env,
                                  stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_ready(run_dir / 'server.log', args.startup_timeout)
            realized = []
            for e in entries:
                record = dict(id=e['id'], split=e['split'])
                if e['mode'] == 'single':
                    messages = [dict(role='user', content=e['prompt'])]
                    resp = post_chat(args.port,
                                     chat_request(messages, e['output_cap']))
                    record.update(requests=[dict(messages=messages)],
                                  responses=[resp])
                elif e['mode'] == 'accept-tier':
                    prompt = generate_accept_prompt(args.model_dir)
                    record['prompt_sha256'] = sha256_bytes(prompt.encode())
                    messages = [dict(role='user', content=prompt)]
                    resp = post_chat(args.port,
                                     chat_request(messages, e['output_cap']),
                                     timeout=5400)
                    record.update(requests=[dict(messages=messages)],
                                  responses=[resp])
                else:  # multiturn / taskswitch
                    messages = []
                    for turn, spec in enumerate(e['turns'], 1):
                        task = spec if isinstance(spec, str) else spec['task']
                        messages.append(dict(role='user', content=task))
                        resp = post_chat(args.port,
                                         chat_request(messages,
                                                      e['output_cap']))
                        messages.append(dict(role='assistant',
                                             content=resp['text']))
                        record.setdefault('turns', []).append(
                            dict(turn=turn, request=dict(messages=messages),
                                 response=resp))
                ok = all(
                    r.get('usage') and r['finish'] in ('length', 'stop')
                    for r in (record.get('responses')
                              or [t['response'] for t in record.get('turns', [])]))
                record['ok'] = ok
                realized.append(record)
                (run_dir / 'realized.json').write_text(
                    json.dumps(realized, indent=1, ensure_ascii=False))
                print(f"{name}/{e['id']}: ok={ok}", flush=True)
                if not ok:
                    raise RuntimeError(f"entry failed: {e['id']}")
        finally:
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    manifest = json.loads((run_dir / 'trace' / 'manifest.json').read_text())
    if not manifest['complete'] or manifest['failure'] != 'none':
        raise RuntimeError(f"trace incomplete in {name}: "
                           f"{manifest.get('failure')}")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--model-dir', type=Path, required=True)
    ap.add_argument('--binary', type=Path, default=ROOT / 'build/q4t')
    ap.add_argument('--port', type=int, default=18090)
    ap.add_argument('--startup-timeout', type=int, default=300)
    ap.add_argument('--runs', default=','.join(RUNS))
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        if exe.name.startswith('q4t') and b'serve' in argv:
            raise RuntimeError('another q4t service is running')
    plan = json.loads(args.plan.read_text())
    out.mkdir(parents=True, exist_ok=False)
    (out / 'plan.json').write_bytes(args.plan.read_bytes())
    (out / 'identity.json').write_text(json.dumps(dict(
        binary_sha256=sha(args.binary),
        plan_sha256=sha(args.plan),
        commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        tools={p.name: sha(p) for p in
               (ROOT / 'tools/trace').glob('*.py')}), indent=1))
    (out / 'worktree.patch').write_bytes(
        subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    reuse = verify_reuse(plan['entries'], out)
    (out / 'reuse.json').write_text(json.dumps(reuse, indent=1))
    selected = [n.strip() for n in args.runs.split(',') if n.strip()]
    for name in selected:
        if name not in RUNS:
            ap.error(f'unknown run {name}')
        entries = [e for e in plan['entries'] if RUNS[name](e)]
        print(f'run {name}: {len(entries)} entries', flush=True)
        manifest = run_entries(name, entries, args, out)
        print(f'run {name}: complete, '
              f"{manifest['requests_published']} requests", flush=True)
    print('all runs complete', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
