"""Bind current route capture to exact HTTP bodies, tokens and completed lease."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/trace'))
from offload_trace import validate_request

BASE = ROOT / '.q4t-work/shared-l2-20261020'
STAGE = BASE / 'capture-v2'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def main():
    manifest = read(STAGE / 'sealed-files.json')
    for name, digest in manifest.items():
        path = (STAGE / name).resolve()
        require(path.is_relative_to(STAGE) and sha(path) == digest, 'sealed capture changed')
    guard = read(STAGE / 'lease/exit.json')
    require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0, 'lease completion')
    out = STAGE / 'run'
    state, plan = read(out / 'state.json'), read(out / 'plan.json')
    require(state['status'] == 'CAPTURED_NOT_PERFORMANCE_ACCEPTANCE' and
            len(state['completed']) == 2, 'capture completion')
    require(sha(ROOT / 'build/q4t') == plan['binary_sha256'], 'current binary binding')
    for name, digest in read(out / 'sources.json').items():
        require(sha(ROOT / name) == digest, 'current capture tool binding')
    require(sha(BASE / 'capture-inputs.jsonl') == plan['workload_sha256'], 'workload identity')
    inputs = [json.loads(line) for line in (BASE / 'capture-inputs.jsonl').read_text().splitlines()]
    rows = []
    for index, item in enumerate(inputs):
        body = read(out / f'http-request-{index}.json')
        response = read(out / f'response-{index}.json')
        require('messages' not in body and body['prompt'] == item['prompt'] and
                body['max_tokens'] == item['max_tokens'], 'raw HTTP prompt altered')
        validation = validate_request(out / 'trace' / f'request-{index+1}.bin',
            checker=ROOT / 'build/q4t_router_trace_check', source_binary=ROOT / 'build/q4t')
        metadata, summary = validation['metadata'], validation['summary']
        require(metadata['http_id'] == response['id'] and
                metadata['prompt_tokens'] == response['usage']['prompt_tokens'] and
                summary['output_tokens'] == response['usage']['completion_tokens'], 'trace/HTTP binding')
        if index == 0:
            require(metadata['prompt_tokens'] == 45056 and
                    response['choices'][0]['message']['content'].strip() == '711146', 'calibration quality')
        rows.append(dict(id=item['id'], response_id=response['id'], summary=summary,
            trace_sha256=validation['trace_sha256'], prompt_sha256=hashlib.sha256(item['prompt'].encode()).hexdigest()))
    cleanup = read(out / 'scope/cleanup.json')
    require(cleanup['unit_removed'], 'capture scope cleanup')
    print(json.dumps(dict(status='PASS_CURRENT_ROUTE_CAPTURE', sealed_files=len(manifest),
        binary_sha256=plan['binary_sha256'], requests=rows, performance_acceptance=False,
        holdout_semantic_quality_acceptance=False), indent=2))


if __name__ == '__main__':
    main()
