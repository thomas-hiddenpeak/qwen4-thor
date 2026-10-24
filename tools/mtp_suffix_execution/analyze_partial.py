#!/usr/bin/env python3
"""Analyze the completed normal/CPU prefix of the stopped execution rerun.

The original 49/13 frozen plan and STOPPED state remain untouched. This fixed
adapter accepts precisely nine completed services (37 normal + 6 CPU HTTP),
requires no telemetry failure, validates all consumed raw seals, and excludes
the incomplete Nsight branch. It imports only the original offline parser.
There are no HTTP, model, CUDA, profiler, proc/sysfs, or hardware calls.
"""
import argparse
from collections import defaultdict
import datetime
import json
import math
from pathlib import Path

import analyze_results as original


PLAN_SHA256 = 'f999e0950cd14dd364e4cb249c1e58184cf229887380d38a6ca0a903c8db69e1'
NORMAL = ['first-http', 'full-off', 'full-on', 'subset-off-a1',
          'subset-on-b', 'subset-off-a2']
CPU = ['exec-off-a1', 'exec-on-b', 'exec-off-a2']
COMPLETE = NORMAL + CPU
EXCLUDED = ['nsys8-off', 'nsys8-on', 'nsys200-on', 'nsys200-off']
require = original.require


def audit_prefix(reader, audit_path):
    if audit_path is None:
        return None
    audit = reader.json(audit_path)
    scope = audit.get('partial_analysis', {})
    require(audit.get('evidence_integrity') == 'PASS' and
            audit.get('evidence_validity') == 'PARTIAL_VALID_PROFILE_ENV_ABORT' and
            audit.get('measurement_complete') is False and
            not audit.get('errors') and not audit.get('monitor_environment_errors') and
            audit.get('identities', {}).get('plan_sha256') == PLAN_SHA256 and
            scope.get('supported') is True and scope.get('normal_groups') == NORMAL and
            scope.get('cpu_groups') == CPU and scope.get('normal_requests') == 37 and
            scope.get('cpu_requests') == 6 and scope.get('excluded_groups') == EXCLUDED,
            'independent audit does not authorize this exact completed prefix')
    return dict(path=str(audit_path), sha256=reader.inputs[str(audit_path)],
                evidence_validity=audit['evidence_validity'],
                measurement_complete=False, partial_analysis=scope)


def completed_group(reader, root, step):
    folder = root / step['name']
    exit_record = reader.json(folder / 'exit.json')
    require(exit_record['server'] == 0 and exit_record['http_output_checks_passed'] and
            not exit_record['failure'] and not exit_record['cleanup_errors'] and
            exit_record['execution_records_complete'] and exit_record['launcher'] is None and
            exit_record['trace_exports_complete'] is None,
            'completed prefix contains a failed or profiled service')
    require(reader.json(folder / 'step.json') == step, 'step differs from frozen plan')
    execution_audit = reader.json(folder / 'execution-audit.json')
    require(execution_audit['complete'] and not execution_audit['errors'],
            'driver raw execution audit failed')
    events = reader.jsonl(folder / 'events.jsonl')
    shutdowns = [e for e in events if e['event'] == 'server_after_shutdown']
    require(len(shutdowns) == 1 and shutdowns[0]['server_returncode'] == 0 and
            not shutdowns[0].get('errors'), 'server shutdown closure failed')
    server_pid = int(reader.text(folder / 'server.pid'))
    command = reader.json(folder / 'server-command.json')
    env = command['environment']
    require('Q4T_EXEC_NVTX' not in env and 'Q4T_EXEC_PROFILE_INPUT' not in env and
            (env.get('Q4T_EXEC_DIAGNOSTIC') == '1') == step['execution_diagnostic'],
            'normal/CPU activation differs or profile leaked into prefix')
    logs, tables, captures = original.parse_log(
        reader.text(folder / 'server.log'), step, server_pid)
    require(not captures, 'prefix unexpectedly contains profiler capture')
    selected = []
    for case in step['cases']:
        directory = folder / case['id']
        responses = reader.json(directory / 'responses.json')
        boundaries = reader.json(directory / 'request-boundaries.json')
        require(len(responses) == len(boundaries) == case['repeats'],
                'completed case request count differs')
        wires = original.wire_records(reader, directory, responses)
        for ordinal, (response, boundary, wire) in enumerate(
                zip(responses, boundaries, wires), 1):
            key = response['response_id']
            require(key in logs, 'response lacks native request identity')
            selected.append(original.request_record(step, case, response, boundary,
                wire, logs[key], ordinal, len(selected) + 1, server_pid))
    require(len(selected) == len(logs) == step['http_requests'],
            'unmatched completed prefix HTTP/native count')
    if step['suffix']:
        require(len(tables) == 2 and tables[0]['event'] == 'init' and
                tables[1]['event'] == 'release' and tables[0]['bytes'] == tables[1]['bytes'] and
                tables[1]['line'] > max(r['request_end']['line'] for r in selected),
                'suffix class table lifecycle incomplete')
    else:
        require(not tables, 'suffix OFF allocated class table')
    group = dict(name=step['name'], head=step['head'], suffix=step['suffix'],
        measurement_role=step['measurement_role'], server_pid=server_pid,
        table_lifecycle=tables, execution_driver_audit=execution_audit,
        shutdown=shutdowns[0],
        cases={case['id']: original.case_summary(
            [r for r in selected if r['case'] == case['id']]) for case in step['cases']})
    return group, selected


def analyze(plan_path, audit_path=None):
    require(original.sha(plan_path) == PLAN_SHA256, 'not the frozen authorized rerun')
    plan = json.loads(plan_path.read_text())
    original.validate(plan)
    root, lease = Path(plan['output']).resolve(), Path(plan['lease']).resolve()
    state = json.loads((root / 'state.json').read_text())
    guard = json.loads((lease / 'exit.json').read_text())
    require(state.get('status') == 'STOPPED' and state.get('finished') and
            state['completed'] == COMPLETE and state['requests_completed'] == 43 and
            state['loads_started'] == 10 and state['current'] == 'nsys8-off' and
            state['plan_sha256'] == PLAN_SHA256 and
            not state.get('monitor_failures') and not state.get('cleanup_errors') and
            guard.get('status') == 'COMMAND_FAILED' and guard.get('child_returncode') == 1,
            'stage is not the exact stopped clean-monitor prefix; no analysis')
    reader = original.Reader(plan)
    for path in (plan_path, root / 'state.json', lease / 'exit.json',
                 Path(original.__file__).resolve(),
                 Path(original.__file__).with_name('spec.py').resolve(),
                 Path(__file__).resolve()):
        reader.verify(path)
    events = reader.jsonl(lease / 'events.jsonl')
    require(any(e.get('event') == 'lease_released' for e in events),
            'stage GPU lease has not been released')
    postflight = reader.json(plan_path.parent / 'postflight.json')
    summary = reader.json(root / 'telemetry-summary.json')
    require(postflight['status'] == 'STOPPED' and
            not postflight['monitor_failures'] and not postflight['cleanup_errors'] and
            not postflight.get('child_cleanup_errors') and
            not postflight.get('final_monitor_errors') and
            postflight['state_sha256'] == reader.inputs[str(root / 'state.json')] and
            postflight['final_summary_sha256'] == reader.inputs[str(root / 'telemetry-summary.json')] and
            summary['sampler_status'] == 'STOPPED' and
            summary['sampler_failure'] is None and not summary['failures_at_exit'] and
            summary['unflushed_attempts'] == 0 and
            summary['sample_attempts'] == summary['flushed_samples'] and
            summary['flushed_samples'] > 0, 'stopped-stage telemetry/cleanup invalid')
    # The success-only final verification barrier was never executed after
    # the profile stop. Do not synthesize it or promote the whole stage.
    audit = audit_prefix(reader, audit_path)
    requests, groups = [], []
    for step in plan['steps'][:len(COMPLETE)]:
        require(step['name'] == COMPLETE[len(groups)] and
                step['measurement_role'] != 'nsys_profile', 'prefix order changed')
        group, selected = completed_group(reader, root, step)
        groups.append(group)
        requests.extend(selected)
    require(len(requests) == 43 and
            sum(r['group'] in NORMAL for r in requests) == 37 and
            sum(r['group'] in CPU for r in requests) == 6,
            'normal/CPU prefix budget mismatch')
    hashes = defaultdict(set)
    for row in requests:
        hashes[row['head'] + '/' + row['case']].add(row['output_sha256'])
    require(all(len(values) == 1 for values in hashes.values()),
            'completed prefix same-head outputs differ')
    gs = {g['name']: g for g in groups}
    lookup = {(r['group'], r['case'], r['ordinal']): r for r in requests}
    repeat = {}
    for case in ('repeat-code', 'repeat-text'):
        full = gs['full-on']['cases'][case]['medians']['decode_tps'] / gs['full-off']['cases'][case]['medians']['decode_tps']
        ratios = [gs['subset-on-b']['cases'][case]['medians']['decode_tps'] /
                  gs[name]['cases'][case]['medians']['decode_tps']
                  for name in ('subset-off-a1', 'subset-off-a2')]
        gm = math.sqrt(math.prod(ratios))
        repeat[case] = dict(full_ratio=full, subset_B_A1=ratios[0], subset_B_A2=ratios[1],
            subset_shared_B_geomean=gm,
            frozen_descriptive_target_met=full >= 1.05 and min(ratios) > 1 and gm >= 1.05,
            raw_and_medians={name: gs[name]['cases'][case] for name in
                ('full-off', 'full-on', 'subset-off-a1', 'subset-on-b', 'subset-off-a2')})
    comparisons = {}
    for case in ('context-8192', 'context-204800'):
        comparisons[case] = {}
        for kind, names in [('normal', ('subset-off-a1', 'subset-on-b', 'subset-off-a2')),
                            ('execution', ('exec-off-a1', 'exec-on-b', 'exec-off-a2'))]:
            a1, b, a2 = names
            comparisons[case][kind] = {
                label: original.pair(lookup[(left, case, 1)], lookup[(right, case, 1)])
                for label, left, right in [('B-A1', a1, b), ('B-A2', a2, b), ('A2-A1', a1, a2)]}
    return dict(created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        evidence_validity=('PARTIAL_PREFIX_BOUND_TO_INDEPENDENT_AUDIT' if audit else
                           'CONSUMED_RAW_CHECKS_ONLY_PENDING_INDEPENDENT_AUDIT'),
        whole_stage_outcome='STOPPED_AFTER_PROFILE_DIAGNOSTIC_FAILURE',
        whole_stage_measurement_complete=False,
        analyzed_prefix_complete=True, analyzed_normal_http=37, analyzed_cpu_http=6,
        excluded_profile_groups=EXCLUDED, analyzed_profile_http=0,
        gpu_activity_evidence='UNAVAILABLE_NOT_ANALYZED',
        statistical_conclusion='DESCRIPTIVE_ONLY', performance_acceptance=False,
        deployment_authorized_by_analysis=False,
        plan_sha256=PLAN_SHA256, candidate_sha256=plan['candidate_sha256'],
        original_state=state, original_guard=guard, original_postflight=postflight,
        telemetry_summary=summary, independent_audit=audit,
        requests=requests, groups=groups,
        same_head_output_hashes={key: sorted(values) for key, values in hashes.items()},
        normal_repeat_confirmation=repeat, long_comparisons=comparisons,
        long_probe_zero_hit={r['group'] + '/' + r['case']: r['suffix_counters'].get('hits', 0) == 0
                             for r in requests if r['case'].startswith('context-')},
        inputs_sha256=reader.inputs, limits=[
            'The original frozen plan remains 49 requests/13 starts. Only nine '
            'completed normal/CPU groups (43 requests) enter this report.',
            'The failing profile group and all unstarted groups are excluded. '
            'There is no valid GPU busy/queue/kernel attribution here.',
            'The success-only post-shutdown monitor barrier was not reached. '
            'This report preserves STOPPED/COMMAND_FAILED and does not mark the whole stage VALID.',
            *original.LIMITS[1:9],
            'CPU observed services and normal services are separate startups. '
            'CPU phase differences cannot establish the cause of a normal HTTP '
            'difference unless supported by further independent evidence.',
            'Thread CPU includes host computation, CUDA driver work, submission '
            'and possible busy waiting. A pageable D2H API may itself block; '
            'a later explicit synchronization with wall close to thread CPU '
            'does not prove there was no wait. Neither is GPU busy time.',
            'mtp.forward exclusive is the measured remainder after its nested '
            'scopes: it includes workspace carving, position copying, embedding, '
            'RMSNorm/FC and HC work; it is not one operation. Its generation '
            'totals combine neural proposal and accepted-prefix extend.',
            'This adapter was written after collection for the exact completed '
            'prefix; source and imported parser hashes are retained. It changes '
            'no frozen input, metric definition or original stage status.',
        ])


def markdown(report):
    # Reuse the original metric tables; correct its whole-stage heading/count
    # and omit the excluded profile comparison entirely.
    compatible = dict(report, evidence_validity=report['evidence_validity'])
    text = original.markdown(compatible)
    text = text.replace('逐请求合同：49/49；服务：13。仅描述性定位，GPU活动另表审计。',
        '已完成前缀：43/43请求、9服务（37正常＋6 CPU观测）。'
        '原阶段STOPPED，整体测量不完整；失败profile全部排除，没有GPU活动结论。')
    lines = ['', 'CPU核对摘要（所有数值从原始整数ns换算为ms）：', '',
        '| 探针 | CPU组 | target prefill wall | target prefill thread CPU | '
        '首个内容轮后 forward exclusive wall | 对应thread CPU | 对应非运行wall |',
        '|---|---|---:|---:|---:|---:|---:|']
    for row in report['requests']:
        if row['group'] not in CPU:
            continue
        observation = row['execution']
        prefill = observation['prefill_and_init_batches']['phases']['target_prefill_call']
        forward = observation['after_server_first_content_round']['phases']['mtp.forward']
        values = (prefill['inclusive_wall_ns'], prefill['inclusive_cpu_ns'],
                  forward['exclusive_wall_ns'], forward['exclusive_cpu_ns'],
                  forward['exclusive_nonrunning_wall_ns'])
        lines.append(f"| {row['case']} | {row['group']} | " +
                     ' | '.join(f'{v / 1e6:.6f}' for v in values) + ' |')
    lines += ['', 'target prefill为各真实chunk调用之和；forward exclusive剔除了已观测子区间，'
              '且包含神经proposal和接受前缀extend。首个内容轮后是server轮次代理，'
              '不等同于客户端TTFT精确切片。上述CPU与非运行wall均不能解释为GPU耗时。', '']
    text += '\n'.join(lines)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--audit', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--markdown', type=Path, required=True)
    args = parser.parse_args()
    plan_path = args.plan.resolve()
    plan = json.loads(plan_path.read_text())
    protected = [plan_path.parent, Path(plan['output']).resolve(),
                 Path(plan['lease']).resolve(), Path(plan['source_snapshot']).resolve()]
    for path in (args.output, args.markdown):
        require(not path.exists() and
                not any(path.resolve().is_relative_to(p) for p in protected),
                'output exists or enters frozen evidence')
    require(args.output.resolve() != args.markdown.resolve(), 'outputs overlap')
    report = analyze(plan_path, args.audit.resolve() if args.audit else None)
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    with args.markdown.open('x') as stream:
        stream.write(markdown(report))
    print(json.dumps(dict(output=str(args.output), markdown=str(args.markdown),
        analyzed_normal=report['analyzed_normal_http'], analyzed_cpu=report['analyzed_cpu_http'],
        whole_stage_measurement_complete=False, gpu_activity_evidence='UNAVAILABLE')))


if __name__ == '__main__':
    main()
