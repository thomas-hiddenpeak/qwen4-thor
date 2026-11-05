"""Verify only the four completed tool responses from the preserved failed driver."""
from dataclasses import asdict
import io
import json
from tools.serve_client_governance import cpu
from tools.serve_clients import short as s
from tools.serve_error_params.stage import h

BASE = cpu.BASE
FIRST = BASE/'execution-02'
PACKAGE = BASE/'package-03'


def check():
    h.verify_seal(PACKAGE)
    plan = cpu.read(PACKAGE/'plan.json')
    result = cpu.read(FIRST/'business/result.json')
    cpu.require(result['status'] == 'FAILED' and result['counts'] == dict(
        starts=1, generation_attempts=4, controls=4) and
        result['error'] == "RuntimeError('HTTP status: active-preflight')", 'unexpected original failure')
    error = cpu.read(FIRST/'business/active-preflight-wire.bin')['error']
    cpu.require(error['param'] == 'model' and error['message'] == 'unsupported preflight field', 'failure identity')
    cpu.require(cpu.sha(PACKAGE/'candidate/q4t') == plan['candidate_sha256'] and
        cpu.runtime() == cpu.read(PACKAGE/'candidate/runtime-source.json'), 'unchanged runtime required')
    s.pkg.verify_declarations(cpu.read(PACKAGE/'previous/q4t.release.json'), required=True)
    completed = []
    for name in ('alpha', 'beta'):
        prefix = 'project-'+name
        values = []
        for suffix in ('read', 'answer'):
            label = prefix+'-'+suffix
            p = FIRST/'business'
            req = cpu.read(p/(label+'-request.json'))['body']
            value = s.a.read_sse(io.BytesIO((p/(label+'-wire.bin')).read_bytes()))
            record = cpu.read(p/(label+'-response.json'))
            elapsed = record.pop('elapsed')
            cpu.require(record == asdict(value) and value.id == req['request_id'] and
                        value.usage['completion_tokens'] <= req['max_tokens'], 'original wire/usage')
            completed.append(dict(label=label, usage=value.usage, elapsed=elapsed,
                                  wire=h.identity(p/(label+'-wire.bin'))))
            values.append(value)
        calls = s.a.prepare_tool_calls(values[0], s.a.VALIDATORS)
        cpu.require(len(calls) == 1 and calls[0].arguments ==
                    dict(path='docs/MODEL.md', start_line=1, line_count=24), 'tool arguments')
        actual = s.a.ProjectRecords(cpu.ROOT).execute(calls[0])
        actual['application_budget'] = dict(remaining_model_requests=1, remaining_tool_calls=0,
                                            final_response_required=True)
        saved = cpu.read(FIRST/'business'/(prefix+'-tool-result.json'))
        cpu.require(saved['tool_call_id'] == calls[0].call_id and json.loads(saved['content']) == actual, 'real record')
        s.a.validate_answer(values[1].message['content'], {actual['source']['citation']}, 1)
        cpu.require(values[1].finish_reason == 'stop' and
                    'Qwen4ExpForConditionalGeneration' in values[1].message['content'], 'project quality')
    leases = []
    for folder in (BASE/'execution/lease', FIRST/'lease'):
        rows = [json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
        obs = [r for r in rows if r['event'] == 'monitor']
        cpu.require(rows[0]['event'] == 'lease_acquired' and rows[-1]['event'] == 'lease_released' and
                    cpu.read(folder/'exit.json')['status'] == 'COMMAND_FAILED' and
                    all(not r['conflicts'] and r['unreadable_processes'] == 0 for r in obs), 'prior guard')
        leases.append(dict(path=str(folder), seconds=rows[-1]['monotonic']-rows[0]['monotonic'],
                           observations=len(obs), status='COMMAND_FAILED'))
    from tools.serve_cuda_quota_integration.verify import custody
    for file in (FIRST/'business').glob('*/identity.json'):
        before = cpu.read(file)
        try:
            now = custody.proc(before['pid'])
        except FileNotFoundError:
            now = None
        cpu.require(now is None or now['start_ticks'] != before['start_ticks'], 'original child retained')
    return dict(status='REUSE_FOUR_COMPLETED_RESPONSES_ONLY', prior_stage_accepted=False,
                completed=completed, real_tools=2, candidate_sha256=plan['candidate_sha256'],
                sources_unchanged=True, leases=leases, lease_seconds=sum(v['seconds'] for v in leases))


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
