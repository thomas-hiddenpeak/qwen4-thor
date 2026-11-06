"""Offline audit of the frozen R1/R2 short acceptance; no model execution."""
import argparse
from dataclasses import asdict
import importlib.util
import io
import json
import math
from pathlib import Path
import re

spec = importlib.util.spec_from_file_location('refresh_stage',
    Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
spec = importlib.util.spec_from_file_location('profile_reader',
    stage.ROOT / 'tools/mtp_default_profile/analyze.py')
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)
from chat_client import read_sse, prepare_tool_calls, append_tool_results

require, sha = stage.require, stage.sha
QSA = ['prepare', 'index_proj', 'index_prepare', 'score_select', 'sparse']
NUMBER = r'(\d+(?:\.\d+)?)'
QSA_LINE = re.compile(r'^\[qsa-subtiming\] T=(\d+) valid=1 layers=(\d+) ' +
    ' '.join(k + '=' + NUMBER for k in QSA + ['total']) + r' ms$')


def request_logs(raw, expected):
    result, active = {}, None
    for number, line in enumerate(raw.splitlines(), 1):
        fields = dict(re.findall(r'(\w+)=(\S+)', line))
        if line.startswith('[q4t][request] ') and fields.get('event') == 'admitted':
            require(active is None and fields['id'] not in result,
                    'overlapping/duplicate request')
            active = fields['id']
            result[active] = dict(begin=number, histogram=None, qsa=[], core=[])
        if line.startswith('[mtp-accept] '):
            require(active is not None and active == fields.get('request') and
                    result[active]['histogram'] is None, 'histogram binding')
            counts = [int(x) for x in fields['draft_counts'].split(',')]
            require(len(counts) == 4 and sum(counts) > 0, 'histogram shape')
            result[active]['histogram'] = counts
        if line.startswith('[sub-timing] '):
            require(active is not None, 'unbound base timing')
            values = dict(re.findall(r'(\w+)=([0-9]+(?:\.[0-9]+)?)', line))
            require(all(k in values for k in ('T', 'full_core')), 'base fields')
            if int(values['T']) > 8:
                result[active]['core'].append(dict(T=int(values['T']),
                    full_core=float(values['full_core']), line=number))
        if line.startswith('[qsa-subtiming] '):
            match = QSA_LINE.fullmatch(line)
            require(active is not None and match is not None, 'invalid QSA row')
            values = match.groups()
            row = dict(T=int(values[0]), layers=int(values[1]), line=number)
            row.update(zip(QSA + ['total'], map(float, values[2:])))
            require(row['T'] > 8 and row['layers'] == 12 and
                    all(math.isfinite(row[k]) and row[k] > 0
                        for k in QSA + ['total']), 'QSA layer/value contract')
            require(abs(sum(row[k] for k in QSA) - row['total']) <= .0031,
                    'printed QSA sum/rounding')
            result[active]['qsa'].append(row)
        if line.startswith('[q4t][request] ') and fields.get('event') == 'end':
            require(active == fields['id'] and fields.get('released') == '1'
                    and fields.get('outcome') == 'success', 'request completion')
            result[active]['end'] = number
            active = None
    require(active is None and len(result) == expected, 'request coverage')
    return result


def tool_audit(folder, plan):
    completions, requests = [], []
    for turn in range(2):
        request = json.loads((folder / f'{turn}-request.json').read_text())
        parsed = read_sse(io.BytesIO((folder / f'{turn}-wire.bin').read_bytes()))
        require(asdict(parsed) == json.loads(
            (folder / f'{turn}-response.json').read_text()), 'tool wire differs')
        require(parsed.id == request['request_id'] and
                parsed.model == request['model'], 'tool response binding')
        requests.append(request)
        completions.append(parsed)
    calls = prepare_tool_calls(completions[0], stage.VALIDATORS)
    require(len(calls) == 1 and asdict(calls[0]) == json.loads(
        (folder / 'tool-started.json').read_text()) and
        calls[0].name == 'read_project_record' and calls[0].arguments ==
        dict(path='docs/MODEL.md', start_line=1, line_count=24), 'tool arguments')
    value = json.loads((folder / 'tool-result.json').read_text())
    actual = stage.ProjectRecords(stage.ROOT).execute(calls[0])
    require(value == actual and value['ok'] and value['source']['sha256'] ==
        plan['files'][str(stage.ROOT / 'docs/MODEL.md')], 'tool real source')
    require(requests[1]['messages'] == append_tool_results(
        requests[0]['messages'], completions[0], {
            calls[0].call_id: json.dumps(value, ensure_ascii=False)}), 'tool history')
    stage.append_final(requests[1]['messages'], completions[1])
    answer = completions[1].message['content']
    stage.validate_answer(answer, {value['source']['citation']}, 1)
    require('Qwen4ExpForConditionalGeneration' in answer, 'tool answer quality')
    return dict(request_ids=[c.id for c in completions], requests=2, tool_calls=1,
                answer=answer, source=value['source'])


def analyze(plan_path, lease):
    plan = json.loads(plan_path.read_text())
    output = Path(plan['output'])
    stage.base.monitored_stage.require_complete_monitor(plan)
    stage.legacy.verify_files(plan)
    guard = json.loads((lease / 'exit.json').read_text())
    require(guard['status'] == 'COMPLETED', 'outer lease')
    seal = json.loads((output / 'files.json').read_text())
    require(all(sha(output / n) == h for n, h in seal.items()), 'raw seal')
    cases, tools, audits = {}, None, {}
    for step in plan['steps']:
        folder = output / step['name']
        audits[step['name']] = stage.audit_server(folder, step['http_requests'])
        logs = request_logs((folder / 'server.log').read_text(), step['http_requests'])
        bound = set()
        for case in step['cases']:
            response = profile.raw_response(folder / case['id'])
            require(response['response_id'] in logs and
                    case['min'] <= response['input_tokens'] <= case['max'], 'HTTP binding')
            log = logs[response['response_id']]
            bound.add(response['response_id'])
            require(log['histogram'] is not None, 'missing MTP trajectory')
            if step['mode'] == 'on':
                require([r['T'] for r in log['qsa']] == [8192] * 5 + [4084]
                        and len(log['core']) == 6, 'known prefill coverage')
                for q, core in zip(log['qsa'], log['core']):
                    require(q['T'] == core['T'] and q['line'] == core['line'] + 1
                            and q['total'] <= core['full_core'] + .006,
                            'QSA detail outside corresponding full_core')
                sums = {k: sum(r[k] for r in log['qsa']) for k in QSA + ['total']}
                core = sum(r['full_core'] for r in log['core'])
                detail = dict(rows=log['qsa'], sum_ms=sums, full_core_ms=core,
                    unassigned_full_core_ms=core - sums['total'],
                    share_of_qsa={k: sums[k] / sums['total'] for k in QSA})
            else:
                require(not log['qsa'] and not log['core'], 'OFF emitted timings')
                detail = None
            cases[step['name'] + '/' + case['id']] = dict(head=step['head'],
                mode=step['mode'], case=case['id'], http=response,
                histogram=log['histogram'], qsa=detail,
                source=dict(log=str(folder / 'server.log'),
                            begin=log['begin'], end=log['end']))
        if step['tool_loop']:
            tools = tool_audit(folder / 'project-tool', plan)
            bound.update(tools['request_ids'])
        require(bound == logs.keys(), 'unaccounted HTTP requests')
    comparisons = []
    for head in ('full', 'subset'):
        for context in ('context-1024', 'context-45056'):
            matched = {r['mode']: r for r in cases.values()
                       if r['head'] == head and r['case'] == context}
            reference = matched['baseline']
            for mode, row in matched.items():
                require(row['histogram'] == reference['histogram'] and
                    all(row['http'][k] == reference['http'][k] for k in
                        ('prompt_sha256', 'input_tokens', 'output_sha256',
                         'output_tokens', 'finish')), 'output/trajectory mismatch')
            metrics = {m: {k: r['http'][k] for k in ('ttft', 'latency', 'decode_tps')}
                       for m, r in matched.items()}
            changes = {m: {k: metrics[m][k] / metrics['off'][k] - 1 for k in
                          ('ttft', 'latency', 'decode_tps')} for m in metrics if m == 'on'}
            comparisons.append(dict(head=head, context=context, identical=True,
                observations=metrics, observation_change_vs_off=changes))
    state = json.loads((output / 'state.json').read_text())
    require(state['loads_started'] == 6 and state['requests_completed'] == 12 and
            state['elapsed_seconds'] <= 1140, 'actual ledger')
    return dict(status='BOUNDED_ACCEPTANCE_PASS', performance_accepted=False,
        deployed=False, cases=cases, comparisons=comparisons, tool=tools,
        audits=audits, elapsed_seconds=state['elapsed_seconds'],
        verified_inputs=len(plan['files']), verified_raw_files=len(seal),
        limitations=['Two contexts and two accepted head modes; no five-tier performance claim.',
                    'CUDA event intervals include host/observer gaps; not kernel-only time.',
                    'ON includes legacy sub-timing and runs 45K first; OFF runs it after 1K. '
                    'Differences include request order/cold-start effects, not isolated QSA overhead.',
                    'Canonical real config HTTP plus CPU alias checks; no alternate checkpoint.',
                    'No long soak, managed protocol migration, or default replacement.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--lease', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.plan.resolve(), args.lease.resolve())
    with args.output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('status', 'elapsed_seconds',
                                           'verified_inputs', 'verified_raw_files')}))
