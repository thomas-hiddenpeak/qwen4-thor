"""Bounded R1/R2 acceptance; no optimization or deployment decision."""
import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'refresh_http', ROOT / 'tools/mtp_selected_head/stage.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
legacy, telemetry, sha, save = base.legacy, base.telemetry, base.sha, base.save
sys.path.insert(0, str(ROOT / 'tools/examples'))
from chat_client import (ChatClient, append_final, append_tool_results,
                         make_request, prepare_tool_calls, require)
from project_query import SYSTEM, validate_answer
from project_records import ProjectRecords, TOOLS, VALIDATORS

MODES = {'baseline': {}, 'off': {},
         'on': {'Q4T_LAYER_SUBTIMING': '1', 'Q4T_QSA_SUBTIMING': '1'}}
QUESTION = ('先调用一次read_project_record读取docs/MODEL.md的第1到24行，'
            'start_line=1、line_count=24。再回答architectures字段的准确值，'
            '并附工具给出的引用。')


def validate(plan):
    require(plan['stage'] == 'REFERENCE_REFRESH_R1_R2', 'stage identity')
    require((plan['http_loads'], plan['http_requests'],
             plan['stage_timeout_seconds'], plan['approved_total_wall_seconds'])
            == (6, 12, 1140, 1200), 'approved budget')
    expected = [(m, h) for m in MODES for h in ('full', 'subset')]
    require([(s['mode'], s['head']) for s in plan['steps']] == expected,
            'normal HTTP before observation')
    for step in plan['steps']:
        tool = step['mode'] == 'off' and step['head'] == 'full'
        contexts = [45056] if step['mode'] == 'on' else [1024, 45056]
        require(step['tool_loop'] == tool and step['http_loads'] == 1 and
                step['http_requests'] == len(contexts) + 2 * tool,
                'per-start ledger')
        require([(c['id'], c['count'], c['tokens'], c['parallel'])
                 for c in step['cases']] ==
                [(f'context-{n}', 1, 256, 1) for n in contexts], 'case budget')
        binary = plan['baseline'] if step['mode'] == 'baseline' else plan['candidate']
        require(step['server_command'][0] == binary, 'binary binding')
    require(time.time() < plan['task_deadline_unix'], 'absolute deadline')


def tool_loop(plan, out, check):
    directory = out / 'project-tool'
    directory.mkdir()
    client = ChatClient(f"http://127.0.0.1:{plan['port']}/v1", timeout=90)
    history = [{'role': 'system', 'content': SYSTEM},
               {'role': 'user', 'content': QUESTION}]
    ids, citations = [], set()
    for turn in range(2):
        check()
        request = make_request(history, [TOOLS[0]], max_tokens=256,
                               timeout_ms=80000)
        request['parallel_tool_calls'] = False
        request['tool_choice'] = ({'type': 'function', 'function': {
            'name': 'read_project_record'}} if turn == 0 else 'none')
        save(directory / f'{turn}-request.json', request)
        base.http.event(out, 'tool_client_before', turn=turn,
                        request_id=request['request_id'])
        with (directory / f'{turn}-wire.bin').open('xb') as wire:
            response = client.complete(request, sink=wire.write)
        base.http.event(out, 'tool_client_after', turn=turn,
                        request_id=request['request_id'])
        save(directory / f'{turn}-response.json', asdict(response))
        ids.append(response.id)
        if turn == 0:
            calls = prepare_tool_calls(response, VALIDATORS)
            require(len(calls) == 1 and calls[0].name == 'read_project_record'
                    and calls[0].arguments == dict(path='docs/MODEL.md',
                        start_line=1, line_count=24), 'real tool selection')
            save(directory / 'tool-started.json', asdict(calls[0]))
            value = ProjectRecords(ROOT).execute(calls[0])
            save(directory / 'tool-result.json', value)
            require(value['ok'] and value['source']['sha256'] ==
                    plan['files'][str(ROOT / 'docs/MODEL.md')], 'real source')
            citations.add(value['source']['citation'])
            history = append_tool_results(history, response, {
                calls[0].call_id: json.dumps(value, ensure_ascii=False)})
        else:
            history = append_final(history, response)
            answer = response.message['content']
            validate_answer(answer, citations, 1)
            require('Qwen4ExpForConditionalGeneration' in answer,
                    'architecture answer quality')
        save(directory / f'{turn}-history.json', history)
    save(directory / 'completed.json', dict(requests=2, tool_calls=1,
         request_ids=ids, answer=answer, citations=sorted(citations)))


def custom_step(plan, name):
    step = next(s for s in plan['steps'] if s['name'] == name)
    clean, run_case = legacy.clean_env, base.multi.case

    def with_tool(p, s, c, out, env, check):
        result = run_case(p, s, c, out, env, check)
        if step['tool_loop'] and c['id'] == step['cases'][-1]['id']:
            tool_loop(plan, out, check)
        return result

    try:
        legacy.clean_env = lambda: {k: v for k, v in clean().items()
            if not k.startswith(('CUPTI_', 'NSYS_', 'NVTX_')) and
            k != 'CUDA_INJECTION64_PATH'} | MODES[step['mode']]
        base.multi.case = with_tool
        base.custom_step(plan, name)
    finally:
        legacy.clean_env, base.multi.case = clean, run_case


def audit_server(out, expected):
    result = base.audit_server(out, expected)
    step = json.loads((out / 'step.json').read_text())
    env = json.loads((out / 'server-command.json').read_text())['environment']
    require(env == MODES[step['mode']], 'observation environment drift')
    raw = (out / 'server.log').read_text()
    require(not any(s in raw for s in ('sync failed:', 'valid=0',
                'overflow=1', 'clock_error=1')), 'invalid observer record')
    if step['mode'] == 'on':
        require('[qsa-subtiming]' in raw, 'QSA observer missing')
    else:
        require(not any(s in raw for s in ('[sub-timing]', '[qsa-subtiming]',
                '[mtp-timing]', '[layer-timing]')), 'OFF observer contamination')
    if step['tool_loop']:
        value = json.loads((out / 'project-tool/completed.json').read_text())
        require((value['requests'], value['tool_calls']) == (2, 1), 'tool ledger')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--step')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    if args.step:
        custom_step(plan, args.step)
    else:
        base.monitored_stage.run_stage(legacy, telemetry, plan,
            args.plan.resolve(), audit_server=audit_server)
        print('COLLECTED_REQUIRES_OFFLINE_REVIEW')


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('reference refresh interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
