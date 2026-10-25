"""Bounded host/output contracts for the four fixed subset GPU observations.

No GPU or analysis tool is invoked.  These are host work and lifecycle checks,
not certification of trace completeness, GPU duration, or model equivalence.
"""
import hashlib
import json
from pathlib import Path
import re


LOG_LIMIT = 256 * 1024 * 1024
ORACLES = {
    8170: dict(case='context-8192', histogram=[7, 8, 7, 52], tail=3,
               extend_rows=252, restore=22, short_rejected=1,
               prompt='87123c57eb2a547045c6030fc4530940b92acb393f919c0b7a39ffcc2c0e8511',
               output='66bb6d8c428668a991608e634c39eabebd85918b0cdd400c7b22919214ad436e'),
    204781: dict(case='context-204800', histogram=[8, 7, 5, 54], tail=2,
                 extend_rows=253, restore=20, short_rejected=12,
                 prompt='6bebfddfdde3af3416629c1e969df18abde763df6be927afb96cdd93bc59e16e',
                 output='deffc238dd45199fab2769c7543813147228e6eee7a28be2acd1aae388ea35fe'),
}
ARMS = {
    'nsys8-off': (False, [8170], 8170),
    'nsys8-on': (True, [8170], 8170),
    'nsys200-on': (True, [8170, 204781], 204781),
    'nsys200-off': (False, [8170, 204781], 204781),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path, limit=LOG_LIMIT):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= limit,
            'missing/oversized evidence: ' + str(path))
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'evidence grew beyond limit')
    return data.decode('utf-8')


def fields(line, number):
    pairs = re.findall(r'(\w+)=(\S+)', line)
    require(len(dict(pairs)) == len(pairs), 'duplicate log field')
    return dict(pairs, line=number)


def number(row, key):
    value = row[key]
    require(re.fullmatch(r'-?\d+', str(value)) is not None,
            'noninteger ' + key)
    return int(value)


def histogram(row, key):
    parts = row[key].split(',')
    require(len(parts) == 4 and all(x.isdigit() for x in parts),
            'invalid fixed-k3 histogram')
    return list(map(int, parts))


def lifecycle(text):
    """Only the application request prefix can admit/end a request."""
    requests, active = {}, None
    for index, line in enumerate(text.splitlines(), 1):
        if not line.startswith('[q4t][request] '):
            continue
        row = fields(line, index)
        key, event = row.get('id'), row.get('event')
        if event == 'admitted':
            require(key and key != 'unregistered' and key not in requests and
                    active is None and row.get('seq') == '0',
                    'duplicate/overlapping/unbound admission')
            requests[key] = dict(admitted=row, stages=[])
            active = key
        elif event == 'end':
            require(key == active and key in requests and
                    'end' not in requests[key] and row.get('released') == '1' and
                    row.get('seq') == '0' and
                    row.get('instance') == requests[key]['admitted'].get('instance'),
                    'request end/slot release identity mismatch')
            requests[key]['end'] = row
            active = None
        elif event == 'stage' and key in requests:
            require(key == active, 'stage outside its admitted request')
            requests[key]['stages'].append(row)
    require(active is None and requests and
            all('end' in r for r in requests.values()), 'incomplete lifecycle')
    return requests


def server_evidence(output, expected_requests):
    """Preserve the old base/admission contracts, with anchored lifecycle."""
    output = Path(output)
    text = read(output / 'server.log')
    lines = text.splitlines()
    requests = lifecycle(text)
    require(len(requests) == expected_requests, 'admitted request count differs')
    final = [json.loads(line.split('] ', 1)[1]) for line in lines
             if line.startswith('[startup-reservation] ')]
    final = [r for r in final if r.get('event') == 'finalized']
    require(len(final) == 1 and final[0].get('status') == 'PASS' and
            final[0].get('release_unknown', 0) == 0,
            'startup ownership finalization failed')
    for marker in ('MTP trunk alloc failed', 'plain decode for this request',
                   'MTP load failed', 'MTP init failed', 'MTP allocation failed',
                   'retained after failed drain'):
        require(marker not in text, 'MTP fallback/drain failure: ' + marker)
    exit_row = json.loads(read(output / 'exit.json', 1024 * 1024))
    require(exit_row['server'] == 0 and exit_row['http_output_checks_passed'] is True,
            'HTTP or clean server exit failed')
    argv = json.loads(read(output / 'server-command.json', 1024 * 1024))['argv']
    enabled = '--mtp-suffix' in argv
    summaries, tables = [], []
    for index, line in enumerate(lines, 1):
        if line.startswith('[suffix] '):
            row = fields(line, index)
            require('disabled' not in row and number(row, 'invalid') == 0 and
                    number(row, 'exhausted') == 0,
                    'suffix invalid/capacity/disabled fallback')
            summaries.append(row)
        elif line.startswith('[suffix-classes] '):
            tables.append(fields(line, index))
    require(len(summaries) == (expected_requests if enabled else 0),
            'suffix activation/summary count differs')
    if not enabled:
        require(not tables, 'suffix OFF initialized classification table')
    else:
        require({r.get('request') for r in summaries} == requests.keys(),
                'duplicate/missing suffix request identity')
        init = [r for r in tables if r.get('event') == 'init']
        release = [r for r in tables if r.get('event') == 'release']
        banners = [line for line in lines if 'suffix enabled:' in line]
        require(len(init) == len(release) == len(banners) == 1 and len(tables) == 2,
                'missing/duplicate classification table lifecycle')
        banner = fields(banners[0], 0)
        require(banner.get('numeric_gate') == '1' and
                banner.get('min_admitted_match') == '32', 'wrong admission rule')
        require(number(init[0], 'bytes') == 248320 and
                number(init[0], 'cap_bytes') == 262144 and
                number(init[0], 'unknown') == 243 and
                number(init[0], 'init_ns') >= 0 and
                number(release[0], 'bytes') == 248320 and
                number(release[0], 'release_ns') >= 0,
                'classification size/capacity/cost mismatch')
        require(init[0]['line'] < min(r['admitted']['line'] for r in requests.values()) and
                release[0]['line'] > max(r['end']['line'] for r in requests.values()) and
                release[0]['line'] > max(r['line'] for r in summaries),
                'classification lifetime does not enclose requests')
        for row in summaries:
            req = requests[row['request']]
            require(req['admitted']['line'] < row['line'] < req['end']['line'],
                    'suffix summary outside request lifetime')
            names = ('queries', 'hits', 'peak_bytes', 'query_ns', 'numeric_checks',
                     'classified_tokens', 'numeric_rejected', 'short_rejected',
                     'numeric_ns', 'no_match', 'incomplete', 'd0_mismatch')
            v = {k: number(row, k) for k in names}
            require(all(x >= 0 for x in v.values()) and
                    0 < v['peak_bytes'] <= 2 * 1024 * 1024,
                    'invalid suffix resource/counter value')
            hit, rejected, short = v['hits'], v['numeric_rejected'], v['short_rejected']
            outcomes = sum(v[k] for k in ('hits', 'no_match', 'incomplete',
                                         'd0_mismatch', 'numeric_rejected', 'short_rejected'))
            require(v['numeric_checks'] == hit + rejected + short and
                    v['numeric_checks'] <= v['queries'] == outcomes and
                    32 * hit + 3 * (rejected + short) <= v['classified_tokens'] <=
                    64 * (hit + rejected) + 31 * short and
                    v['numeric_ns'] <= v['query_ns'],
                    'admission outcomes/scan bounds/nested cost mismatch')
            if row['request'].endswith('-cancel'):
                require(hit > 0 and req['end'].get('outcome') == 'cancelled' and
                        req['end'].get('reason') == 'explicit_cancel',
                        'cancel did not follow suffix use')
    return text, requests, summaries, tables, final[0], enabled


def audit_server(output, expected_requests):
    """Legacy callback: write compatible receipts, raise on invalid evidence."""
    try:
        text, requests, suffix, table, final, enabled = server_evidence(
            output, expected_requests)
        output = Path(output)
        (output / 'server-audit.json').write_text(json.dumps(dict(
            admitted_requests=len(requests), finalization=final, suffix=suffix,
            lifecycle_prefix='[q4t][request]', mtp_records=[line for line in text.splitlines()
                if line.startswith(('[mtp-', '[suffix]', '[suffix-', '[mtp-suffix'))]),
            indent=2) + '\n')
        if enabled:
            (output / 'admission-audit.json').write_text(json.dumps(dict(
                table=table, requests=suffix, min_admitted_match=32, table_bytes=248320,
                table_cap_bytes=262144, request_index_cap_bytes=2 * 1024 * 1024,
                cost_scope='numeric_ns is included in query_ns; never add both'),
                indent=2) + '\n')
    except (OSError, KeyError, ValueError, TypeError) as error:
        raise RuntimeError('GPU-pair server contract: ' + str(error)) from error


def execution(text, requests, target_pid):
    batches, captures, histograms = {}, [], {}
    for index, line in enumerate(text.splitlines(), 1):
        if line.startswith(('[mtp-timing]', '[layer-timing]')):
            raise ValueError('legacy intrusive timing enabled')
        if not line.startswith(('[exec-', '[mtp-accept] ')):
            continue
        row = fields(line, index)
        owners = [key for key, r in requests.items()
                  if r['admitted']['line'] < index < r['end']['line']]
        require(len(owners) == 1, 'host record outside unique admitted request')
        row['request'] = owners[0]
        if line.startswith('[mtp-accept] '):
            require(fields(line, index).get('request') == owners[0] and
                    owners[0] not in histograms, 'duplicate/unbound acceptance')
            histograms[owners[0]] = histogram(row, 'draft_counts')
            continue
        marker = line.split(']', 1)[0] + ']'
        if marker == '[exec-capture]':
            captures.append(row)
            continue
        key = number(row, 'batch')
        if marker == '[exec-batch]':
            require(key not in batches and number(row, 'pid') == target_pid and
                    number(row, 'seq') == 0 and row['label'] in (owners[0], 'mtp.multi'),
                    'duplicate/wrong-process/unbound execution batch')
            batches[key] = dict(row, spans=[])
        elif marker == '[exec-span]':
            require(key in batches and row['request'] == batches[key]['request'],
                    'span outside its batch/request')
            batches[key]['spans'].append(row)
        elif marker == '[exec-flush]':
            require(key in batches and 'flush' not in batches[key] and
                    row['request'] == batches[key]['request'], 'duplicate/unbound flush')
            batches[key]['flush'] = row
        else:
            raise ValueError('unknown execution record')
    require(histograms.keys() == requests.keys(), 'missing acceptance histogram')
    for batch in batches.values():
        rows, flush = batch['spans'], batch['flush']
        require(batch['complete'] == '1' and batch['overflow'] == '0' and
                batch['clock_error'] == '0' and number(batch, 'capacity') == 4096 and
                0 < number(batch, 'records') == len(rows) <= 4096 and
                flush['clock_error'] == '0' and number(flush, 'wall_ns') >= 0 and
                number(flush, 'cpu_ns') >= 0 and flush['line'] > rows[-1]['line'],
                'incomplete execution batch/flush')
        for index, row in enumerate(rows):
            parent = number(row, 'parent')
            require(number(row, 'id') == index and -1 <= parent < index and
                    row['closed'] == '1' and row['pid'] == batch['pid'] and
                    row['tid'] == batch['tid'] and row['seq'] == batch['seq'] and
                    row['line'] > batch['line'], 'bad execution span identity/tree')
            for clock in ('wall', 'cpu'):
                start, end = number(row, clock + '_begin_ns'), number(row, clock + '_end_ns')
                require(0 <= start <= end, 'invalid execution clocks')
                if parent >= 0:
                    require(number(rows[parent], clock + '_begin_ns') <= start <= end <=
                            number(rows[parent], clock + '_end_ns'), 'child escapes parent')
    return batches, captures, histograms


def phase(rows, name):
    return [r for r in rows if r['phase'] == name]


def request_work(request_id, tokens, req, batches, hist):
    expected = ORACLES[tokens]
    own = [b for b in batches.values() if b['request'] == request_id]
    rows = [r for b in own for r in b['spans']]
    require(len(own) == 77, 'three prompt batches plus 74 speculative batches required')
    require(req['end'].get('outcome') == 'success' and
            req['end'].get('reason') == 'completed', 'request did not complete normally')
    stages = [r['stage'] for r in req['stages']]
    require(stages.count('prefill') == stages.count('decode') == 1,
            'prefill/decode request boundaries missing/duplicate')
    chunks = [(base, min(8192, tokens - base)) for base in range(0, tokens, 8192)]
    calls = [(number(r, 'p'), number(r, 't')) for r in phase(rows, 'target_prefill_call')]
    require(calls == chunks, 'target prefill chunk coverage differs')
    for name in ('handler_prefill', 'target_begin_sequence', 'handler_mtp_init',
                 'prompt_draft_reset', 'prompt_draft_extend_call', 'mtp.prompt_extend.total',
                 'prompt_verify_reserve', 'prompt_trunk_free'):
        found = phase(rows, name)
        require(len(found) == 1 and number(found[0], 'p') == 0 and
                number(found[0], 't') == tokens, 'missing/wrong prompt phase: ' + name)
    for name in ('prefill_final_d2h_submit', 'prefill_final_readback_wait_commit'):
        found = phase(rows, name)
        require(len(found) == 1 and (number(found[0], 'p'), number(found[0], 't')) == chunks[-1],
                'final prefill boundary differs')
    draft_chunks = [(number(r, 'p'), number(r, 't'))
                    for r in phase(rows, 'mtp.prompt_extend.chunk')]
    require(draft_chunks == (chunks if tokens > 8192 else []),
            'prompt draft chunk coverage differs')
    target_chunks = [(number(r, 'p'), number(r, 't'))
                     for r in phase(rows, 'target_prefill_chunk')]
    require(target_chunks == (chunks if tokens > 8192 else []),
            'target chunk wrapper coverage differs')
    require(len(phase(rows, 'prefill_chunk_readback_wait_commit')) == len(chunks) - 1,
            'intermediate prefill waits differ')
    prompt_rows = [r for b in own if b['label'] == request_id for r in b['spans']]
    require([(number(r, 'p'), number(r, 't')) for r in phase(prompt_rows, 'mtp.forward')]
            == chunks, 'prompt draft forward coverage differs')
    rounds = [b for b in own if b['label'] == 'mtp.multi']
    require(len(rounds) == 74 and hist == expected['histogram'], 'verify/acceptance oracle differs')
    for name, count in (('mtp.step', 74), ('mtp.verify_forward', 74),
                        ('mtp.draft_token', 148), ('mtp.accepted_extend', 74),
                        ('mtp.restore_checkpoint', expected['restore'])):
        require(len(phase(rows, name)) == count, 'extra/missing request work: ' + name)
    accepted, position, restores = [0, 0, 0, 0], tokens, 0
    for batch in rounds:
        rs = batch['spans']
        require((number(batch, 'p'), number(batch, 't')) == (position, 4),
                'speculative step position/shape differs')
        for name in ('mtp.step', 'mtp.verify_forward', 'mtp.accepted_extend'):
            require(len(phase(rs, name)) == 1, 'missing/duplicate round phase: ' + name)
        verify = phase(rs, 'mtp.verify_forward')[0]
        extend = phase(rs, 'mtp.accepted_extend')[0]
        root = phase(rs, 'mtp.step')[0]
        n = number(extend, 't')
        require((number(root, 'p'), number(root, 't')) == (position, 4) and
                (number(verify, 'p'), number(verify, 't')) == (position, 4) and
                number(extend, 'p') == position and 1 <= n <= 4,
                'verify/accepted extend shape differs')
        draft_scopes = phase(rs, 'mtp.draft_token')
        drafts = [(number(r, 'p'), number(r, 't')) for r in draft_scopes]
        require(drafts == [(position, 1), (position + 1, 1)],
                'neural draft coverage differs from zero-hit oracle')
        # MtpForward's position is max_position; the accepted-prefix forward
        # therefore ends at P+n-1, while its enclosing extend span starts at P.
        forwards = phase(rs, 'mtp.forward')
        require([(number(r, 'p'), number(r, 't'), number(r, 'parent')) for r in forwards] ==
                [(position, 1, number(draft_scopes[0], 'id')),
                 (position + 1, 1, number(draft_scopes[1], 'id')),
                 (position + n - 1, n, number(extend, 'id'))],
                'neural/accepted MtpForward shape or parent differs')
        restore = phase(rs, 'mtp.restore_checkpoint')
        require(len(restore) == int(n < 4) and
                all((number(r, 'p'), number(r, 't')) == (position, 4) for r in restore),
                'restore does not match accepted prefix')
        restores += len(restore)
        accepted[n - 1] += 1
        position += n
    require(accepted == hist and restores == expected['restore'],
            'accepted extend/restore histogram differs')
    committed = position - tokens
    tail = 255 - committed
    waits = number(req['end'], 'scheduler_waits')
    require(committed == expected['extend_rows'] and tail == expected['tail'] and
            waits == 74 + tail, 'output/pending/tail/scheduler closure differs')
    return dict(request_id=request_id, input_tokens=tokens, output_tokens=256,
                target_prefill_chunks=chunks, prompt_draft_chunks=draft_chunks,
                verify_T4=74, neural_draft_T1=148, restore=restores,
                accepted_extend_calls=74, accepted_extend_rows=committed,
                histogram=hist, scheduler_waits=waits, tail_T1=tail,
                tail_basis='256 minus one final pending token minus accepted-extend rows; '
                           'also scheduler_waits minus 74 host verify spans',
                target_rows_generation=4 * 74 + tail, batches=len(own), spans=len(rows))


def validate_work(output, step, target_pid):
    """Read current capture evidence, return a fail-closed local work result."""
    result = dict(schema=1, status='INVALID', errors=[], counters={}, requests=[],
                  performance_acceptance=False, whole_trace_completeness_claimed=False,
                  scope='Host work/output identity only; tail T1 is derived, not GPU activity. '
                        'Generation counts exclude target prefill/full-prompt draft extend.')
    try:
        require(type(target_pid) is int and target_pid > 0, 'invalid target PID')
        enabled, inputs, capture = ARMS[step['name']]
        require(step['head'] == 'subset' and step['suffix'] is enabled and
                step['execution_diagnostic'] is True and step['measurement_role'] == 'nsys_profile' and
                step['profile_input_tokens'] == capture and step['http_requests'] == len(inputs) and
                step['http_loads'] == 1 and
                [c['input_tokens'] for c in step['cases']] == inputs and
                all(c['repeats'] == 1 and c['output_tokens'] == 256 for c in step['cases']),
                'frozen four-arm work scope differs')
        output = Path(output)
        text, requests, suffix, _, _, actual_enabled = server_evidence(output, len(inputs))
        require(actual_enabled == enabled, 'server suffix flag differs from step')
        batches, captures, histograms = execution(text, requests, target_pid)
        responses = json.loads(read(output / 'results.json'))
        require(len(responses) == len(inputs), 'HTTP case count differs')
        ids = []
        for item, tokens, case in zip(responses, inputs, step['cases']):
            oracle = ORACLES[tokens]
            require(item['id'] == case['id'] == oracle['case'] and
                    item['outputs'] == [oracle['output']] and len(item['responses']) == 1 and
                    case['prompt_sha256'] == oracle['prompt'] and
                    case['expected_output_sha256'] == oracle['output'], 'HTTP output/prompt oracle differs')
            response = item['responses'][0]
            require(response['success'] == 1 and response['actual_input'] == tokens and
                    response['actual_output'] == 256 and response['finish'] == ['length'] and
                    response['prompt_sha256'] == oracle['prompt'], 'HTTP response contract differs')
            key = response['response_id']
            require(key in requests and key not in ids, 'HTTP/request log identity differs')
            ids.append(key)
            result['requests'].append(request_work(key, tokens, requests[key], batches, histograms[key]))
        require(ids == list(requests), 'request order/8K predecessor differs')
        selected = ids[inputs.index(capture)]
        require([r.get('event') for r in captures] == ['begin', 'end'] and
                all(number(r, 'pid') == target_pid and number(r, 'tid') > 0 and
                    number(r, 'input_tokens') == capture and r['error'] == '0' and
                    r['request'] == selected for r in captures) and
                captures[0]['tid'] == captures[1]['tid'], 'capture PID/input/request boundary differs')
        for batch in batches.values():
            inside = captures[0]['line'] < batch['line'] < batch['flush']['line'] < captures[1]['line']
            require(inside == (batch['request'] == selected), 'capture includes wrong request/omits host batches')
        for row in suffix:
            tokens = inputs[ids.index(row['request'])]
            oracle = ORACLES[tokens]
            require(number(row, 'queries') == 74 and number(row, 'hits') == 0 and
                    number(row, 'short_rejected') == oracle['short_rejected'] and
                    number(row, 'numeric_rejected') == 0 and
                    histogram(row, 'accepted') == [0, 0, 0, 0] and
                    histogram(row, 'neural_accepted') == oracle['histogram'],
                    'zero-hit suffix work differs')
        result.update(status='VALID', target_pid=target_pid, capture_input_tokens=capture,
                      capture_request_id=selected, captures=captures,
                      counters=next(r for r in result['requests'] if r['request_id'] == selected),
                      suffix=suffix, source=dict(path=str(output / 'server.log'),
                          sha256=hashlib.sha256(text.encode()).hexdigest()))
    except (OSError, KeyError, ValueError, TypeError, IndexError) as error:
        result['errors'].append(str(error))
    return result
