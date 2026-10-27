"""Validate original full-head records; preserve the failed runner state."""
import hashlib
import json
from pathlib import Path

from audit import runtime
from oracle import short_answer
from run_stage import ROOT, save


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    base = ROOT / '.q4t-work/session-cache-20261027'
    phase = base / 'phase-c-fixed-cap'
    plan = json.loads((phase / 'plan.json').read_text())
    directory = phase / 'run/01-full-on'
    exit_record = json.loads((directory / 'exit.json').read_text())
    if exit_record != dict(server=0, http_output_checks_passed=False,
                           failure="RuntimeError('same-session recovery quality')"):
        raise ValueError('unexpected original failure')
    post = json.loads((phase / 'postflight.json').read_text())
    if any(post[k] for k in ['monitor_failures','cleanup_errors','final_monitor_errors']):
        raise ValueError('environment failure')
    for scenario in plan['scenarios']:
        for round_name in ['first','interference','second']:
            result = json.loads((directory / (scenario['id']+'-'+round_name) /
                                 'result.json').read_text())
            for row, case in zip(result['rows'], scenario[round_name], strict=True):
                if (not row['success'] or row['finish'] != ['length'] or
                    row['input_tokens'] != case['input_tokens'] or
                    row['output_tokens'] != case['tokens'] or
                    row['request_sha256'] != case['prompt_sha256'] or
                    hashlib.sha256(row['text'].encode()).hexdigest() != row['output_sha256'] or
                    case.get('expected_sha256') and row['output_sha256'] != case['expected_sha256']):
                    raise ValueError('original repetition contract failed')
    request = json.loads((directory / 'after-cancel-request.json').read_text())
    response = json.loads((directory / 'after-cancel-response.json').read_text())
    cancel = json.loads((directory / 'cancel-request.json').read_text())
    if (request['prompt'] != plan['short_prompt'] or
        request['session_id'] != cancel['session_id'] or not response['done'] or
        response['disconnected'] or response['text'].strip() != short_answer(plan) or
        response['usage']['prompt_tokens_details']['cached_tokens'] != 0):
        raise ValueError('actual recovery contract failed')
    audit = runtime((directory / 'server.log').read_text(), 26)
    if sum(r['outcome']=='cancelled' for r in audit['lifecycle']) != 1:
        raise ValueError('expected one cancellation')
    save(directory / 'session-audit-adjudicated.json', audit)
    result = dict(passed=True, inference_repeated=False, validated_http_requests=26,
        actual_prompt_sha256=hashlib.sha256(request['prompt'].encode()).hexdigest(),
        expected_from_frozen_prompt_oracle=short_answer(plan), actual=response['text'],
        incorrect_length_tie_oracle=plan['short_expected'],
        original_exit=exit_record, original_postflight_status=post['status'],
        monitor_scope='All collected samples joined/flushed without errors; '
        'original failed stage has no explicit final post-shutdown barrier. '
        'Do not relabel that stage COMPLETED.',
        archive_audit_sha256=sha(phase/'archive/audit.json'),
        raw_sha256={n:sha(directory/n) for n in ['server.log','exit.json',
            'after-cancel-request.json','after-cancel-response.json',
            'cancel-request.json','cancel-response.json']},
        request_budget_change=0, start_budget_change=0)
    save(base / 'recovery-adjudication.json', result)
    print(json.dumps({k:v for k,v in result.items() if k!='raw_sha256'}, indent=2))


if __name__ == '__main__':
    main()
