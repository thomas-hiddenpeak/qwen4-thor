"""Reconcile unchanged A06 runtime evidence after the counter-only driver fix."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jsonschema import Draft202012Validator
from tools.serve_completions.acceptance import best_of_expected, check_accounting, requests
from tools.serve_service.package import read, require, save, sha


def review(directory):
    result, plan, cleanup = (read(directory/name) for name in
                            ('result.json', 'plan.json', 'cleanup.json'))
    require(result['status'] == 'FAILED' and
            result['error'].endswith('RuntimeError: business accounting\n') and
            result['counts']['starts'] == 1 and result['counts']['chats'] == 7,
            'not the recorded counter-only failure')
    require(plan['requests'] == requests(), 'request drift')
    changed = {name for name, digest in plan['sources'].items()
               if sha(ROOT/name) != digest}
    require(changed == {'tools/serve_completions/acceptance.py',
                        'tools/serve_completions/test_acceptance.py'},
            'unexpected source drift: '+str(changed))
    require(sha(plan['binary']) == plan['binary_sha256'] == result['binary_sha256'],
            'tested ELF changed')
    runtime = {name: digest for name, digest in plan['sources'].items()
               if name.startswith(('src/', 'include/', 'cmake/')) or name == 'CMakeLists.txt'}
    # Verify the original frozen source blobs, not just current path contents.
    raw = subprocess.check_output(['git', 'cat-file', '--batch'], input=''.join(
        plan['source_commit']+':'+name+'\n' for name in runtime).encode(), cwd=ROOT)
    import hashlib
    offset = 0
    for name, digest in runtime.items():
        end = raw.index(b'\n', offset)
        header = raw[offset:end].split()
        require(len(header) == 3 and header[1] == b'blob', 'missing original blob')
        size = int(header[2])
        require(hashlib.sha256(raw[end+1:end+size+1]).hexdigest() == digest,
                'original runtime blob differs: '+name)
        offset = end+size+2
    require(cleanup['model_exit'] == 0 and cleanup['remaining'] == [] and
            cleanup['ports_released'] and cleanup['lease_continuously_held'],
            'cleanup incomplete')
    for pid, row in cleanup['owned'].items():
        path = Path('/proc')/pid/'stat'
        if path.exists():
            require(int(path.read_text().rsplit(')', 1)[1].split()[19]) != row['start_ticks'],
                    'owned process still exists')
    lease = directory/'lease-inherited'
    end = read(lease/'exit.json')
    require(end['status'] == 'COMMAND_FAILED' and end['child_returncode'] == 1,
            'original failed lease result changed')
    samples = [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    require(samples and all(row['status'] == 'OK' for row in samples), 'NVML failure')
    events = [json.loads(line) for line in (lease/'events.jsonl').read_text().splitlines()]
    acquired = [e['monotonic'] for e in events if e['event'] == 'lease_acquired']
    released = [e['monotonic'] for e in events if e['event'] == 'lease_released']
    require(len(acquired) == len(released) == 1, 'continuous lease not proven')
    require(read(directory/'launch-correction.json')['counts'] == dict(starts=0, chats=0),
            'initial zero-model failure changed')
    schema = read(ROOT/'docs/serve-completion-transport-v1.schema.json')
    validator = Draft202012Validator(dict(schema, **{'$ref': '#/$defs/completion'}))

    def output(label, *, stream=False):
        result = read(directory/(label+'.sdk.json'))
        wire = (directory/(label+'.wire')).read_bytes()
        require(result['ok'] and result['sdk_version'] == '7.47.0' and
                len(result['exchanges']) == 1, 'SDK request outcome')
        value = result['result']
        if stream:
            chunks = [json.loads(line[6:]) for line in wire.splitlines()
                      if line.startswith(b'data: {')]
            require(chunks == result['chunks'] and wire.count(b'data: [DONE]') == 1,
                    'SDK chunks differ from raw stream')
        else:
            require(json.loads(wire) == value, 'SDK result differs from wire')
        if label in ('echo', 'batch-json', 'batch-sse', 'best-of', 'suffix'):
            validator.validate(value)
        return value

    listed = output('models')
    model = next(row for row in listed['data'] if row['id'] == requests()['echo']['model'])
    require(output('model-detail') == model == output('model-detail-repeat'),
            'model registry changed')
    for label, status, code in [('model-absent', 404, 'model_not_found'),
                               ('model-readonly', 403, 'model_not_deletable')]:
        result = read(directory/(label+'.sdk.json'))
        wire = json.loads((directory/(label+'.wire')).read_bytes())
        require(not result['ok'] and result['error']['status'] == status and
                result['error']['code'] == wire['error']['code'] == code,
                'model error contract')
    echo = output('echo')
    prompt = requests()['echo']['prompt']
    row = echo['choices'][0]
    require(row['text'] == prompt and echo['usage']['completion_tokens'] == 0 and
            row['finish_reason'] == 'length', 'zero-output echo')
    probs = row['logprobs']
    require(len(probs['tokens']) == echo['usage']['prompt_tokens'] and
            probs['token_logprobs'][0] is None and probs['top_logprobs'][0] is None and
            probs['text_offset'][0] == 0 and probs['text_offset'][-1] == len(prompt)-1,
            'prompt probability and Unicode offset')
    plain, stream, best = output('batch-json'), output('batch-sse', stream=True), output('best-of')
    require(plain['choices'] == stream['choices'] and
            plain['usage'] == stream['usage'] == best['usage'], 'JSON/SSE/hidden usage')
    ids = [read(directory/('batch-tokenize-'+str(i)+'.wire'))['token_ids'] for i in range(2)]
    require(plain['usage']['prompt_tokens'] == sum(map(len, ids)) and
            plain['usage']['completion_tokens'] == 24, 'actual token input/usage')
    require(read(directory/'batch-json.request.json')['request']['prompt'] == ids,
            'batch did not execute exact preflight IDs')
    expected = best_of_expected(plain)
    require(len(best['choices']) == len(expected), 'best_of choice count')
    for want, got in zip(expected, best['choices']):
        require(got == dict(want['candidate'], index=want['index']), 'best_of ranking')
    suffix = output('suffix')['choices'][0]
    require(suffix['text'].strip().replace(' ', '') in ('a+b', 'b+a') and
            suffix['finish_reason'] == 'stop', 'real FIM business')
    cancel = (directory/'cancel.wire').read_bytes()
    frames = [json.loads(line[6:]) for line in cancel.splitlines() if line.startswith(b'data: {')]
    require(cancel.count(b'data: [DONE]') == 1 and
            frames[-1]['error']['code'] == 'explicit_cancel' and
            not any(row.get('usage') for row in frames) and
            not any(c.get('finish_reason') for row in frames for c in row.get('choices', [])),
            'cancel terminal')
    chat = read(directory/'same-pid-chat.result.json')
    require(chat['finish_reason'] == 'stop' and chat['message']['content'].strip() == '42',
            'same PID Chat result')
    metrics = read(directory/'final.metrics.json')[-1]
    check_accounting(metrics)
    require(metrics['q4t_active_chats'] == 0 and metrics['q4t_structured_workers_active'] == 0,
            'requests did not drain')
    return dict(status='PASS_COMPLETIONS_MODELS_REVIEW',
        source_commit=plan['source_commit'], runtime_source_files=len(runtime),
        runtime_unchanged=True, binary_sha256=plan['binary_sha256'],
        original_failed_result_preserved=True,
        correction='Driver expected cancellation as error; existing contract is separate aborted outcome.',
        changed_driver_sources={n: sha(ROOT/n) for n in sorted(changed)},
        starts=1, chats=7, seconds=result_seconds(directory),
        lease_seconds=released[0]-acquired[0], nvml_samples=len(samples), nvml_all_ok=True,
        additional_gpu_tests=0, schema_and_raw_wire_reviewed=True,
        records={str(p.relative_to(directory)): sha(p) for p in directory.iterdir()
                 if p.is_file()}, remaining_master_tasks_open=True)


def result_seconds(directory):
    return read(directory/'result.json')['seconds']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve prior review')
    result = review(args.directory.resolve(strict=True))
    save(args.output, result)
    print(json.dumps({k: result[k] for k in ('status', 'starts', 'chats', 'seconds',
                                           'lease_seconds', 'nvml_samples')}))
