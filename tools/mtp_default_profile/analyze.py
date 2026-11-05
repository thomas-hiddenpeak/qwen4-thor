"""Offline request-bound timing reconstruction; no model or GPU operations."""
import argparse
import base64
from collections import Counter
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import pickle
import re
import sqlite3

spec = importlib.util.spec_from_file_location(
    'default_profile_stage', Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)

NUMBER = r'(\d+(?:\.\d+)?)'
MTP = re.compile(r'^\[mtp-timing\] B=(\d+) draft=' + NUMBER +
    r' verify=' + NUMBER + r' extend\+accept=' + NUMBER +
    r' ms \(k=(\d+) a=\[(\d+)\]\) suffix=(\d+) gpu\[ draft=' + NUMBER +
    r' verify=' + NUMBER + r' extend=' + NUMBER + r' \]$')
TOP = ['ple', 'grread', 'attn', 'wrar', 'moe', 'write']
ATTENTION = ['lin_inproj', 'lin_state', 'lin_outproj',
             'full_qkv', 'full_core', 'full_oproj']
DETAIL = {
    'layer-timing': ['embed', 'lin', 'full', 'head', 'total'],
    'sub-timing': TOP + ['total'] + ATTENTION,
    'moe-subtiming': ['prep_gu', 'gemm_gu', 'prep_dn', 'gemm_dn', 'combine', 'total'],
    'hc-mix-subtiming': ['norm', 'down_gemm', 'silu', 'up_gemm', 'gate',
                         'inject', 'combine', 'total'],
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def request_logs(raw):
    """Only single active slot, paired request IDs, closed lifecycle windows."""
    result, active = {}, None
    for number, line in enumerate(raw.splitlines(), 1):
        fields = dict(re.findall(r'(\w+)=(\S+)', line))
        if line.startswith('[q4t][request] ') and fields.get('event') == 'admitted':
            require(active is None and fields['id'] not in result,
                    'overlapping or duplicate request')
            active = fields['id']
            result[active] = dict(start_line=number, mtp=[], histogram=None,
                                  timings={k: [] for k in DETAIL})
        if line.startswith('[mtp-timing] '):
            match = MTP.fullmatch(line)
            require(active is not None and match is not None, 'unbound/invalid MTP timing')
            values = [float(x) for x in match.groups()]
            require(values[0] == 1 and values[4] == 3 and
                    0 <= values[5] <= 3 and values[6] == 0, 'unexpected MTP shape')
            result[active]['mtp'].append(dict(line=number, accepted=int(values[5]),
                host=dict(zip(['draft', 'verify', 'extend'], values[1:4])),
                event=dict(zip(['draft', 'verify', 'extend'], values[7:10]))))
        if line.startswith('[mtp-accept] '):
            require(active == fields.get('request') and
                    result[active]['histogram'] is None, 'acceptance request binding')
            counts = [int(x) for x in fields['draft_counts'].split(',')]
            require(len(counts) == 4 and sum(counts) > 0, 'invalid histogram')
            result[active]['histogram'] = counts
        marker = line.split(']', 1)[0].lstrip('[')
        if marker in DETAIL:
            require(active is not None, 'timing outside admitted request')
            numbers = dict(re.findall(r'(\w+)=([0-9]+(?:\.[0-9]+)?)', line))
            require('T' in numbers and all(k in numbers for k in DETAIL[marker]),
                    'missing/failed timing fields')
            row = {k: float(numbers[k]) for k in DETAIL[marker]}
            row.update(T=int(numbers['T']), line=number)
            require(all(math.isfinite(v) and v >= 0 for v in row.values()),
                    'nonfinite/negative timing')
            result[active]['timings'][marker].append(row)
        if line.startswith('[q4t][request] ') and fields.get('event') == 'end':
            require(active == fields['id'] and fields['released'] == '1' and
                    fields['outcome'] == 'success', 'unclosed/failed request')
            result[active]['end_line'] = number
            result[active]['server_end'] = fields
            active = None
    require(active is None and len(result) == 2, 'request lifecycle coverage')
    return result


def raw_response(directory):
    databases = list(directory.rglob('benchmark_data.db'))
    require(len(databases) == 1, 'database count')
    with sqlite3.connect(databases[0].as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT * FROM result').fetchall()
    require(len(rows) == 1 and rows[0]['success'], 'HTTP response count/success')
    row = rows[0]
    # This stage's own local evalscope output, never an untrusted external pickle.
    messages = pickle.loads(base64.b64decode(row['response_messages']))
    choices = [c for m in messages for c in m.get('choices', [])]
    content = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                      for c in choices)
    ids = {m['id'] for m in messages if m.get('id')}
    request = json.loads(row['request'])
    require(len(ids) == 1 and request['max_tokens'] == 256 and request['stream'],
            'raw response identity/request contract')
    value = dict(response_id=next(iter(ids)), input_tokens=row['prompt_tokens'],
        output_tokens=row['completion_tokens'],
        prompt_sha256=hashlib.sha256(request['prompt'].encode()).hexdigest(),
        output_sha256=hashlib.sha256(content.encode()).hexdigest(),
        finish=[c['finish_reason'] for c in choices if c.get('finish_reason')],
        ttft=row['first_chunk_latency'], latency=row['latency'])
    require(value['output_tokens'] == 256 and value['finish'] == ['length'] and
            0 < value['ttft'] < value['latency'], 'HTTP metric/output contract')
    derived = json.loads((directory / 'result.json').read_text())['rows'][0]
    require(all(derived[k] == v for k, v in value.items()), 'derived HTTP differs from raw')
    value['decode_tps'] = 255 / (value['latency'] - value['ttft'])
    value['database'] = str(databases[0])
    return value


def summarize(rows, keys, rounding):
    sums = {k: sum(row[k] for row in rows) for k in keys}
    return dict(count=len(rows), sum_ms=sums,
        mean_ms={k: v / len(rows) for k, v in sums.items()} if rows else {},
        first_ms={k: rows[0][k] for k in keys} if rows else {},
        subsequent_mean_ms={k: sum(r[k] for r in rows[1:]) / (len(rows) - 1)
                            for k in keys} if len(rows) > 1 else {},
        printed_sum_rounding_bound_ms=rounding * len(rows))


def analyze(plan_path, lease):
    plan = json.loads(plan_path.read_text())
    output = Path(plan['output'])
    stage.base.monitored_stage.require_complete_monitor(plan)
    stage.legacy.verify_files(plan)
    guard = json.loads((lease / 'exit.json').read_text())
    require(guard['status'] == 'COMPLETED', 'outer GPU lease failed')
    seal = json.loads((output / 'files.json').read_text())
    for name, digest in seal.items():
        require(stage.sha(output / name) == digest, 'raw seal mismatch: ' + name)
    result = dict(status='VALID_BOUNDED_OBSERVATION', performance_accepted=False,
        statistics='DESCRIPTIVE_SINGLE_START_PER_MODE_HEAD', cases={},
        limitations=['CUDA event intervals include stream gaps/observer costs.',
                    'No pure-kernel, hardware bandwidth, or GPU-idle measurement.',
                    'Nested details are not additional top-level time.'])
    for step in plan['steps']:
        folder = output / step['name']
        stage.audit_server(folder, 2)
        logs = request_logs((folder / 'server.log').read_text())
        observed_ids = set()
        for case in step['cases']:
            response = raw_response(folder / case['id'])
            require(response['response_id'] in logs, 'HTTP/server binding')
            observed_ids.add(response['response_id'])
            log = logs[response['response_id']]
            require(case['min'] <= response['input_tokens'] <= case['max'], 'context range')
            count = sum(log['histogram'])
            timings = {}
            if step['mode'] != 'normal':
                hist = Counter(r['accepted'] for r in log['mtp'])
                require([hist[i] for i in range(4)] == log['histogram'],
                        'MTP rows/histogram disagreement')
                for clock in ('host', 'event'):
                    timings['mtp_' + clock] = summarize(
                        [r[clock] for r in log['mtp']], ['draft', 'verify', 'extend'], .05)
                required = ['layer-timing'] if step['mode'] == 'coarse' else [
                    'sub-timing', 'moe-subtiming', 'hc-mix-subtiming']
                for marker in required:
                    rows = log['timings'][marker]
                    verify = [r for r in rows if r['T'] == 4]
                    require(len(verify) == count, 'verify timing count: ' + marker)
                    require(all(r['total'] > 0 for r in verify), 'zero verify interval')
                    timings[marker] = {str(t): summarize([r for r in rows if r['T'] == t],
                        DETAIL[marker], .005) for t in sorted({r['T'] for r in rows})}
                    if marker == 'sub-timing':
                        require(all(abs(sum(r[k] for k in TOP) - r['total']) <= .036
                                    for r in verify), 'sub-timing sum/rounding mismatch')
            result['cases'][step['name'] + '/' + case['id']] = dict(
                head=step['head'], mode=step['mode'], case=case['id'],
                http=response, histogram=log['histogram'], rounds=count,
                source=dict(log=str(folder / 'server.log'),
                            begin=log['start_line'], end=log['end_line']), timings=timings)
        require(observed_ids == logs.keys(), 'unaccounted HTTP request')
    comparisons = []
    for head in ('full', 'subset'):
        for context in ('context-1024', 'context-45056'):
            matched = {r['mode']: r for r in result['cases'].values()
                       if r['head'] == head and r['case'] == context}
            normal = matched['normal']
            for mode in ('coarse', 'fine'):
                observed = matched[mode]
                require(observed['histogram'] == normal['histogram'] and
                    all(observed['http'][k] == normal['http'][k] for k in (
                        'prompt_sha256', 'input_tokens', 'output_sha256',
                        'output_tokens', 'finish')), 'observer changed output/trajectory')
            comparisons.append(dict(head=head, context=context,
                same_output_and_acceptance=True,
                observations={mode: {k: r['http'][k] for k in
                    ('ttft', 'latency', 'decode_tps')} for mode, r in matched.items()},
                decode_wall_change_vs_normal={mode:
                    (matched[mode]['http']['latency'] - matched[mode]['http']['ttft']) /
                    (normal['http']['latency'] - normal['http']['ttft']) - 1
                    for mode in ('coarse', 'fine')}))
    result['observer_comparisons'] = comparisons
    result['verified_raw_files'] = len(seal)
    result['verified_inputs'] = len(plan['files'])
    return result


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
    print(json.dumps(dict(status=result['status'], cases=len(result['cases']),
                         raw_files=result['verified_raw_files'])))
