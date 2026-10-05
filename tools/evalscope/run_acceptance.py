"""Run real evalscope HTTP quality/performance checks, owning one server.

No connection probe, unit test, microbenchmark or profile precedes the requests.
Only deserialize evalscope databases created locally by this invocation.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import re
import shutil
import sqlite3
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
LENGTHS = [1024, 4096, 8192, 45056, 204800]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))


def clock_sample():
    """Pair wall time with the same host monotonic clock used by evalscope."""
    before = time.perf_counter()
    wall = time.time()
    after = time.perf_counter()
    return {'monotonic_seconds': (before + after) / 2,
            'unix_seconds': wall,
            'utc': datetime.fromtimestamp(wall, timezone.utc).isoformat(),
            'clock_pair_uncertainty_seconds': (after - before) / 2}


def event(out, name, **details):
    value = {'event': name, **clock_sample(), **details}
    with (out / 'events.jsonl').open('a') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')
    return value


def parse_lengths(value, name):
    try:
        lengths = [int(item.strip()) for item in value.split(',')]
    except ValueError as error:
        raise ValueError(f'{name} requires comma-separated positive integers') from error
    if not lengths or any(length <= 0 for length in lengths):
        raise ValueError(f'{name} requires positive lengths')
    if len(lengths) != len(set(lengths)):
        raise ValueError(f'{name} contains duplicate tiers')
    return lengths


def performance_plan(lengths, repeats, extra_lengths, target_total, max_len,
                     bounded):
    if len(set(lengths)) != len(lengths):
        raise ValueError('performance tiers overlap')
    requests = []
    for length in lengths:
        tokens = target_total - length if target_total and length in extra_lengths else 256
        if tokens <= 0 or length + tokens > max_len:
            raise ValueError(f'input {length} + output {tokens} exceeds requested max_len {max_len}')
        requests.append({'input_tokens': length, 'max_tokens': tokens,
                         'total_tokens': length + tokens, 'repeats': repeats})
    complete = set(LENGTHS).issubset(lengths) and repeats >= 3
    offload_complete = complete and 261887 in lengths and any(
        r['input_tokens'] == 261887 and r['max_tokens'] == 257 for r in requests)
    return {'mode': 'performance', 'lengths': lengths, 'repeats': repeats,
            'requests': requests, 'partial': not complete,
            'scope': 'six_tier' if offload_complete else 'five_tier' if complete else 'partial',
            'full_five_tier_requested': complete,
            'full_offload_matrix_requested': offload_complete,
            'performance_acceptance': False,
            'client_protocol': 'one_evalscope_process_per_request' if bounded else 'one_evalscope_process_per_tier',
            'client_protocol_note': ('Explicit selected-tier runs restart the evalscope client/tokenizer for each request; '
                                     'inter-request idle gaps differ from the historical batched matrix.' if bounded else
                                     'Default batched scheduling is unchanged.')}


def capacity_evidence(log, requested_max_len):
    requested = {'max_len': requested_max_len, 'max_seq': 1, 'max_prefill': 8192}
    explicit = re.findall(r'\[q4t\]\[capacity\] ([^\n]+)', log)
    if explicit:
        fields = dict(re.findall(r'(\w+)=(\S+)', explicit[-1]))
        try:
            effective = {key: int(fields[f'effective_{key}']) for key in requested}
            reported = {key: int(fields[f'requested_{key}']) for key in requested}
        except (KeyError, ValueError):
            effective, reported = None, None
        valid = (len(explicit) == 1 and effective == requested and reported == requested and
                 fields.get('budget_feasible') in ('true', 'not_evaluated'))
        return {'requested': requested, 'effective': effective,
                'reported_requested': reported, 'source': 'server effective capacity report',
                'matches_requested': valid, 'budget_enabled': fields.get('budget_enabled'),
                'budget_feasible': fields.get('budget_feasible'), 'raw_lines': explicit}
    # Legacy budget lines report the selected capacity. An absent report is
    # unknown, never an assumption that the requested allocation succeeded.
    pairs = re.findall(r'\[q4t\]\[budget\]\s+=> max_len=(\d+) max_seq=(\d+)', log)
    observed = [{'max_len': int(length), 'max_seq': int(seq)} for length, seq in pairs]
    effective = observed[-1] if len(observed) == 1 else None
    valid = effective is not None and effective['max_len'] == requested_max_len and effective['max_seq'] == 1
    return {'requested': requested, 'effective': effective,
            'source': 'server budget report', 'observations': observed,
            'matches_requested': valid,
            'effective_max_prefill': None,
            'note': 'Legacy startup reports do not independently expose effective max_prefill.'}


def request_timing(start, end, before, after):
    """Preserve HTTP clock values; UTC conversion is explicitly an estimate."""
    valid = all(isinstance(v, (int, float)) and not isinstance(v, bool) and
                math.isfinite(v) for v in (start, end))
    valid = valid and before['monotonic_seconds'] <= start <= end <= after['monotonic_seconds']
    offset_before = before['unix_seconds'] - before['monotonic_seconds']
    offset_after = after['unix_seconds'] - after['monotonic_seconds']
    def utc(value):
        return datetime.fromtimestamp(value + offset_before, timezone.utc).isoformat() if valid else None
    return {'start_monotonic_seconds': start, 'end_monotonic_seconds': end,
            'start_utc_estimate': utc(start), 'end_utc_estimate': utc(end),
            'source': 'evalscope result.start_time/completed_time (perf_counter)',
            'utc_method': 'monotonic HTTP timestamps mapped using pre-client wall/monotonic anchor; not independent wall-clock samples',
            'clock_offset_change_seconds': offset_after - offset_before,
            'within_client_boundaries': valid}


def response_identity(messages):
    """Use the actual response ID, never client order, to join server evidence."""
    values = [message.get('id') for message in messages
              if message.get('choices') or 'id' in message]
    valid = bool(values) and all(isinstance(value, str) and
        re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) for value in values)
    valid = bool(valid and len(set(values)) == 1)
    return {'response_id': values[0] if valid else None,
            'response_id_valid': valid, 'response_ids_observed': values,
            'response_id_source': 'actual HTTP/SSE response id fields'}


def run_case(out, case, inputs, count, minimum, maximum, tokens, streaming,
             evalscope, model, port, env, server, bounded, capacity, mode,
             phase_diagnostics=False):
    """Collect all available evidence even when the client returns nonzero."""
    parsed, boundaries, commands = [], [], []
    lines = inputs.read_text().splitlines()
    attempts = count if bounded else 1
    for attempt in range(attempts):
        client_dir = case / f'run-{attempt + 1}' if bounded else case
        client_inputs = inputs
        client_count = 1 if bounded else count
        label = f'{case.name}:run{attempt + 1}' if bounded else case.name
        if bounded:
            client_dir.mkdir()
            client_inputs = client_dir / 'requests.jsonl'
            client_inputs.write_text(lines[attempt] + '\n')
        cmd = [str(evalscope), 'perf', '--model', 'qwen3.8-flash-next',
               '--url', f'http://127.0.0.1:{port}/v1/chat/completions',
               '--api', 'openai', '--tokenizer-path', str(model),
               '--dataset', 'line_by_line', '--dataset-path', str(client_inputs),
               '--min-prompt-length', str(minimum), '--max-prompt-length', str(maximum),
               '--no-apply-chat-template', '--max-tokens', str(tokens),
               '--temperature', '0', '--seed', '20260920', '--parallel', '1',
               '--number', str(client_count), '--warmup-num', '0', '--connect-timeout', '30',
               '--read-timeout', '7200', '--total-timeout', '10800',
               '--no-test-connection', '--outputs-dir', str(client_dir),
               '--stream' if streaming else '--no-stream']
        commands.append(cmd)
        save(client_dir / 'command.json', cmd)
        (out / 'memory-phase.txt').write_text(f'requests:{label}\n')
        if hasattr(server, 'snapshot'):
            server.snapshot(f'before-{label}')
        before = event(out, 'client_before', case=case.name, request_group=label,
                       expected_requests=client_count, requested_output=tokens,
                       expected_input_min=minimum, expected_input_max=maximum,
                       capacity=capacity)
        code = None
        try:
            with (client_dir / 'client.log').open('w') as client:
                code = subprocess.run(cmd, cwd=ROOT, env=env, stdout=client,
                                      stderr=subprocess.STDOUT, check=False).returncode
        finally:
            after = event(out, 'client_after', case=case.name, request_group=label,
                          client_returncode=code)
            save(client_dir / 'client-exit.json', {'returncode': code, 'before': before, 'after': after})
            if hasattr(server, 'snapshot'):
                server.snapshot(f'after-{label}')
        databases = list(client_dir.rglob('benchmark_data.db'))
        rows = []
        if len(databases) == 1:
            with sqlite3.connect(databases[0]) as db:
                rows = db.execute('select success,prompt_tokens,completion_tokens,'
                                  'response_messages,first_chunk_latency,latency,request,'
                                  'start_time,completed_time from result order by start_time').fetchall()
        for row in rows:
            messages = pickle.loads(base64.b64decode(row[3]))
            identity = response_identity(messages)
            choices = [choice for msg in messages for choice in msg.get('choices', [])]
            text = ''.join(c.get('delta', c.get('message', {})).get('content', '') for c in choices)
            finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
            wire_request = json.loads(row[6])
            prompt = wire_request['prompt']
            timing = request_timing(row[7], row[8], before, after)
            number = len(parsed) + 1
            result = {'success': row[0], 'actual_input': row[1], 'actual_output': row[2],
                      'http_status_code': None,
                      'status_source': 'evalscope success flag; this database schema does not retain HTTP status codes',
                      'text': text, 'finish': finish,
                      'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                      'ttft': row[4], 'latency': row[5],
                      'request_stream': wire_request.get('stream'),
                      'requested_max_tokens': wire_request.get('max_tokens'),
                      'requested_capacity': capacity['requested'],
                      'effective_capacity': capacity['effective'],
                      'timing': timing, **identity}
            parsed.append(result)
            (case / f'output-{number - 1}.txt').write_text(text)
            boundaries.append({'request_index': number, 'request_group': label,
                               'client_before': before, 'client_after': after,
                               'http_timing': timing, 'success': row[0],
                               'actual_input': row[1], 'actual_output': row[2],
                               'finish': finish, 'requested_max_tokens': tokens,
                               'capacity': capacity, **identity})
        save(case / 'responses.json', parsed)
        save(case / 'request-boundaries.json', boundaries)
        if bounded:
            save(case / 'commands.json', commands)
        if code != 0 or len(databases) != 1 or len(rows) != client_count:
            raise RuntimeError(f'{label}: client/database/request count failed (rc={code}, databases={len(databases)}, rows={len(rows)})')
        if phase_diagnostics and (not all(r['response_id_valid'] for r in parsed)
                or len({r['response_id'] for r in parsed}) != len(parsed)):
            raise RuntimeError(f'{label}: missing, conflicting or reused response id')
        if not all(r['success'] and r['timing']['within_client_boundaries'] and
                   r['requested_max_tokens'] == tokens for r in parsed):
            raise RuntimeError(f'{label}: HTTP/timing/requested output contract failed')
        if not all(isinstance(r['actual_input'], int) and
                   isinstance(r['actual_output'], int) and
                   minimum <= r['actual_input'] <= maximum and
                   0 < r['actual_output'] <= tokens and
                   r['actual_input'] + tokens <= capacity['effective']['max_len'] and
                   r['request_stream'] == streaming for r in parsed):
            raise RuntimeError(f'{label}: input/output/capacity/stream contract failed')
        if mode != 'quality' and not all(r['actual_output'] == tokens and
                                         r['finish'] == ['length'] for r in parsed):
            raise RuntimeError(f'{label}: output length/finish contract failed')
        if mode == 'performance' and not all(
                all(isinstance(r[key], (int, float)) and not isinstance(r[key], bool) and
                    math.isfinite(r[key]) and r[key] > 0 for key in ('ttft', 'latency')) and
                r['latency'] > r['ttft'] for r in parsed):
            raise RuntimeError('invalid HTTP performance metrics')
        if mode == 'performance' and len({r['text'] for r in parsed}) != 1:
            raise RuntimeError('performance output is not deterministic')
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['quality', 'performance', 'limits'], required=True)
    parser.add_argument('--phase-diagnostics', action='store_true',
                        help='Require response IDs for instrumented offload '
                        'phase diagnosis; results never qualify performance')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'build/q4t')
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--startup-timeout', type=int, default=180,
                        help='Seconds to wait for model loading before HTTP requests')
    parser.add_argument('--max-len', type=int, default=208896,
                        help='serve --max-len (total sequence capacity)')
    parser.add_argument('--extra-lengths', type=str, default='',
                        help='comma-separated extra performance tiers '
                             '(e.g. 261887); appended after the fixed five')
    parser.add_argument('--perf-lengths', type=str,
                        help='Explicit comma-separated performance tiers; selected-tier '
                             'runs use one client process per request and report partial scope')
    parser.add_argument('--request-policy-sequence', action='store_true',
                        help='Frozen 21-request boundary/history sequence; '
                             'never substitutes for the six-tier matrix')
    parser.add_argument('--causality-sequence', type=Path,
                        help='Frozen four-request predecessor diagnosis JSON')
    parser.add_argument('--causality-sequence-sha256',
                        help='Required exact bytes SHA256 of causality JSON')
    parser.add_argument('--perf-repeats', type=int, default=3,
                        help='Requests per performance tier (default 3; fewer than 3 is partial)')
    parser.add_argument('--systemd-unit', type=str,
                        help='Optional independent q4t-* transient service; runner/client remain outside')
    parser.add_argument('--host-cache-max-bytes', type=int,
                        help='Positive memcg host/cache charge limit; requires --systemd-unit; '
                             'not a total physical RAM limit. Isolated service swap is disabled.')
    parser.add_argument('--target-total', type=int, default=0,
                        help='Total context (input+output) that extra-length '
                             'tiers must reach; per-request max_tokens becomes '
                             'target-total - input length and the acceptance '
                             'check uses that output count')
    parser.add_argument('--fixtures', type=Path,
                        help='Existing quality-inputs or performance matrix root')
    parser.add_argument('--allow-unqualified-binary', action='store_true',
                        help='Skip the deployment-identity check for a '
                             'candidate binary that has not been installed '
                             'as the default yet; the output still records '
                             'binary sha256, commit and worktree patch')
    parser.add_argument('--reference', type=Path,
                        help='Prior results.json: require identical prompts and outputs')
    parser.add_argument('--moe-trace-dir', type=Path)
    parser.add_argument('--moe-trace-workload', type=Path)
    parser.add_argument('--moe-trace-max-mib', type=int, default=1024)
    parser.add_argument('--moe-resident-slots', type=int, default=0,
                        help='serve --moe-resident-slots (0 = all experts '
                             'resident, the baseline path)')
    parser.add_argument('--moe-hot-list', type=Path,
                        help='serve --moe-hot-list JSON (per-layer static '
                             'hot list for tiered residency)')
    parser.add_argument('--request-deadline-ms', type=int, default=0,
                        help='serve --request-deadline-ms (0 = server '
                             'default 1200000; must be in [1000,10800000])')
    args = parser.parse_args()
    causality = args.causality_sequence is not None
    if causality != (args.causality_sequence_sha256 is not None):
        parser.error('causality sequence and SHA256 must be supplied together')
    if causality and (args.mode != 'performance' or
            args.request_policy_sequence or args.perf_lengths is not None or
            args.extra_lengths or args.target_total or args.perf_repeats != 3
            or args.phase_diagnostics or args.moe_trace_dir or
            args.moe_trace_workload or not args.fixtures):
        parser.error('causality sequence requires frozen fixtures and '
                     'uninstrumented performance without other selections')
    if args.request_policy_sequence and (args.mode != 'performance' or
            args.perf_lengths is not None or args.extra_lengths or
            args.target_total or args.perf_repeats != 3 or
            args.phase_diagnostics):
        parser.error('request-policy sequence requires uninstrumented performance '
                     'without other tier/repeat selection')
    if args.perf_repeats <= 0:
        parser.error('--perf-repeats must be positive')
    if args.mode != 'performance' and (args.perf_lengths is not None or args.perf_repeats != 3):
        parser.error('--perf-lengths/--perf-repeats require performance mode')
    if args.systemd_unit and not re.fullmatch(r'q4t-[A-Za-z0-9][A-Za-z0-9_.-]*\.service', args.systemd_unit):
        parser.error('--systemd-unit must be an independent q4t-*.service unit name')
    if args.host_cache_max_bytes is not None and (
            not args.systemd_unit or args.host_cache_max_bytes <= 0):
        parser.error('--host-cache-max-bytes must be positive and requires --systemd-unit')
    if bool(args.moe_trace_dir) != bool(args.moe_trace_workload):
        parser.error('trace directory and workload must be supplied together')
    if args.moe_resident_slots < 0 or args.moe_resident_slots > 512:
        parser.error('moe-resident-slots must be in [0,512]')
    if args.moe_hot_list and args.moe_resident_slots == 0:
        parser.error('moe-hot-list requires moe-resident-slots > 0')
    if args.startup_timeout <= 0:
        parser.error('startup-timeout must be positive')
    if args.request_deadline_ms and not (
            1000 <= args.request_deadline_ms <= 10800000):
        parser.error('request-deadline-ms must be in [1000,10800000]')
    if args.max_len <= 0:
        parser.error('max-len must be positive')
    try:
        extra_lengths = parse_lengths(args.extra_lengths, '--extra-lengths') if args.extra_lengths else []
        lengths = parse_lengths(args.perf_lengths, '--perf-lengths') if args.perf_lengths is not None else list(LENGTHS)
    except ValueError as error:
        parser.error(str(error))
    if args.target_total <= 0:
        if args.target_total != 0:
            parser.error('target-total must be positive')
    elif extra_lengths:
        for value in extra_lengths:
            if value >= args.target_total:
                parser.error(
                    f'target-total {args.target_total} leaves no output room '
                    f'for input length {value}')
    # Explicit lengths are the complete selection. Extra lengths may specify
    # the target-total rule for selected target tiers; defaults still append.
    if args.perf_lengths is not None:
        if not set(extra_lengths).issubset(lengths):
            parser.error('--extra-lengths must be selected by --perf-lengths')
    else:
        lengths += extra_lengths
    plan = None
    if args.mode == 'performance':
        try:
            plan = performance_plan(lengths, args.perf_repeats, extra_lengths,
                                    args.target_total, args.max_len, args.perf_lengths is not None)
        except ValueError as error:
            parser.error(str(error))
    if args.request_policy_sequence:
        from request_policy_protocol import sequence_plan
        plan = sequence_plan(args.max_len)
        lengths = plan['lengths']
    sequence_content = None
    if causality:
        from causality_protocol import (causality_request_identity,
                                        check_causality_environment,
                                        read_causality_sequence)
        try:
            plan, sequence_content = read_causality_sequence(
                args.causality_sequence, args.causality_sequence_sha256,
                args.max_len)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        lengths = plan['lengths']
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    if out.exists() and any(out.iterdir()):
        parser.error(f'output dir {out} exists and is not empty')
    out.mkdir(parents=True, exist_ok=True)
    event(out, 'runner_start', mode=args.mode)
    if causality:
        (out / 'causality-sequence.json').write_bytes(sequence_content)
    diagnostic_scope = (plan['diagnostic_scope'] if causality else
                        'offload_phase_boundary_v1'
                        if args.phase_diagnostics else None)
    if plan is not None:
        if not causality:
            plan['diagnostic_scope'] = diagnostic_scope
        save(out / 'performance-plan.json', plan)
        print(f"Performance scope: {plan['scope']}; performance acceptance is not implied", flush=True)
    binary = args.binary.resolve()
    model = args.model_dir.resolve()
    env = os.environ.copy()
    if args.phase_diagnostics:
        env['Q4T_OFFLOAD_PHASE_DIAGNOSTICS'] = '1'
        env['Q4T_RESIDENCY_TIMING'] = '1'
    elif env.get('Q4T_OFFLOAD_PHASE_DIAGNOSTICS') not in (None, '0'):
        raise RuntimeError('phase diagnostics environment requires explicit flag')
    if causality:
        check_causality_environment(plan, env)
    removed = {}
    for key in list(env):
        if key.startswith(('Q4T_FP8', 'Q4T_PROFILE', 'Q4T_MTP_TIMING',
                           'Q4T_SCHED_DEBUG', 'Q4T_ACCESS_LOG')):
            removed[key] = env.pop(key)
    (out / 'binary.sha256').write_text(hashlib.sha256(binary.read_bytes()).hexdigest())
    (out / 'commit.txt').write_bytes(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT))
    (out / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    shutil.copyfile(__file__, out / 'run_acceptance.py')
    cache = binary.parent / 'CMakeCache.txt'
    deployment = binary.with_name(binary.name + '.release.json')
    if deployment.is_file() and not args.allow_unqualified_binary:
        identity = json.loads(deployment.read_text())
        if identity['binary_sha256'] != hashlib.sha256(binary.read_bytes()).hexdigest():
            raise RuntimeError('deployment identity is stale; qualify the rebuilt binary')
        cache = Path(identity['build_cache'])
        if hashlib.sha256(cache.read_bytes()).hexdigest() != identity['build_cache_sha256']:
            raise RuntimeError('deployment build-cache identity differs')
    if not cache.is_file():
        raise RuntimeError('binary must have its actual CMakeCache.txt alongside')
    shutil.copyfile(cache, out / 'CMakeCache.txt')
    evalscope = ROOT / 'tools/evalscope/.venv/bin/evalscope'
    python = evalscope.with_name('python')
    (out / 'evalscope-version.txt').write_bytes(subprocess.check_output(
        [str(python), '-c', 'from importlib.metadata import version; print(version("evalscope"))']))
    prepared = out / 'inputs'
    if args.mode == 'quality':
        if args.fixtures:
            shutil.copytree(args.fixtures, prepared)
        else:
            subprocess.run([str(python), str(ROOT / 'tools/evalscope/prepare_quality.py'),
                            '--model-dir', str(model), '--output', str(prepared)], check=True)
        manifest = json.loads((prepared / 'manifest.json').read_text())
        if any(row['length'] + 32 > args.max_len for row in manifest):
            raise RuntimeError('quality input + output exceeds requested max_len')
        cases = [(out / 'quality', prepared / 'requests.jsonl', len(manifest), 1, 208896, 32, True)]
    elif args.mode == 'performance':
        prepared.mkdir()
        cases = []
        for length in lengths:
            target = prepared / f'context-{length}.jsonl'
            if args.fixtures:
                first = (args.fixtures / f'context-{length}/requests.jsonl').read_text().splitlines()[0]
            else:
                subprocess.run([str(python), str(ROOT / 'tools/evalscope/prepare_inputs.py'),
                                '--model-dir', str(model), '--length', str(length),
                                '--number', '1', '--output', str(target)], check=True)
                first = target.read_text().splitlines()[0]
            copies = 1 if causality else args.perf_repeats
            target.write_text((first + '\n') * copies)
            tokens = 256
            if args.target_total and length in extra_lengths:
                tokens = args.target_total - length
            if not args.request_policy_sequence and not causality:
                cases.append((out / f'context-{length}', target, args.perf_repeats, length, length,
                              tokens, True))
        if args.request_policy_sequence or causality:
            for request in plan['requests']:
                length = request['input_tokens']
                target = prepared / (request['case'] + '.jsonl')
                first = (prepared / f'context-{length}.jsonl').read_text().splitlines()[0]
                target.write_text(first + '\n')
                cases.append((out / request['case'], target, 1, length, length,
                              request['max_tokens'], True))
    else:
        prepared.mkdir()
        target = prepared / 'context-1024.jsonl'
        subprocess.run([str(python), str(ROOT / 'tools/evalscope/prepare_inputs.py'),
                        '--model-dir', str(model), '--length', '1024', '--number', '1',
                        '--output', str(target)], check=True)
        cases = [(out / f'{"stream" if streaming else "nonstream"}-{limit}',
                  target, 1, 1024, 1024, limit, streaming)
                 for streaming in [True, False] for limit in [1, 2, 8]]
    reference = json.loads(args.reference.read_text()) if args.reference else None
    command = [str(binary), 'serve', '--model-dir', str(model), '--port', str(args.port),
               '--max-seq', '1', '--max-prefill', '8192', '--max-len',
               str(args.max_len), '--max-tokens', '256', '--no-mtp']
    if args.moe_trace_dir:
        command += ['--moe-trace-dir', str(args.moe_trace_dir.resolve()),
                    '--moe-trace-workload', str(args.moe_trace_workload.resolve()),
                    '--moe-trace-max-mib', str(args.moe_trace_max_mib)]
    if args.moe_resident_slots > 0:
        command += ['--moe-resident-slots', str(args.moe_resident_slots)]
        if args.moe_hot_list:
            command += ['--moe-hot-list', str(args.moe_hot_list.resolve())]
    if args.request_deadline_ms > 0:
        command += ['--request-deadline-ms', str(args.request_deadline_ms)]
    effective_q4t = {key: value for key, value in env.items()
                     if key.startswith('Q4T_')}
    hot_identity = None
    if args.moe_hot_list:
        hot_identity = {'path': str(args.moe_hot_list.resolve()),
                        'sha256': hashlib.sha256(args.moe_hot_list.read_bytes()).hexdigest()}
    save(out / 'server-command.json', {'argv': command, 'removed_environment': removed,
                                       'effective_q4t_environment': effective_q4t,
                                       'phase_diagnostics': args.phase_diagnostics,
                                       'diagnostic_scope': diagnostic_scope,
                                       'hot_list': hot_identity,
                                       'isolation': {'systemd_unit': args.systemd_unit,
                                                     'host_cache_max_bytes': args.host_cache_max_bytes,
                                                     'swap_max_bytes': 0 if args.systemd_unit else None,
                                                     'is_total_physical_ram_limit': False},
                                       'startup_timeout_seconds': args.startup_timeout})
    results = []
    passed = False
    failure = None
    with (out / 'server.log').open('w') as log:
        (out / 'memory-phase.txt').write_text('startup\n')
        event(out, 'server_before_start')
        if args.systemd_unit:
            from isolated_service import IsolatedService
            server = IsolatedService(command, cwd=ROOT, env=env, log_path=out / 'server.log',
                                     unit=args.systemd_unit, memory_max=args.host_cache_max_bytes,
                                     evidence=out / 'isolation')
        else:
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        (out / 'server.pid').write_text(f'{server.pid}\n')
        event(out, 'server_started', pid=server.pid)
        try:
            for _ in range(args.startup_timeout):
                if server.poll() is not None:
                    raise RuntimeError('server exited during startup')
                if 'serving on port' in (out / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f'server startup timeout after {args.startup_timeout}s')
            capacity = capacity_evidence((out / 'server.log').read_text(), args.max_len)
            save(out / 'capacity.json', capacity)
            event(out, 'server_ready', pid=server.pid, capacity=capacity)
            if not capacity['matches_requested']:
                raise RuntimeError('effective server capacity is missing or differs from requested capacity')
            observed_response_ids = set()
            sequence_outputs = {}
            for case, inputs, count, minimum, maximum, tokens, streaming in cases:
                (out / 'memory-phase.txt').write_text(f'requests:{case.name}\n')
                case.mkdir()
                shutil.copyfile(inputs, case / 'requests.jsonl')
                parsed = run_case(out, case, inputs, count, minimum, maximum,
                                  tokens, streaming, evalscope, model, args.port,
                                  env, server, args.mode == 'performance' and
                                  (args.perf_lengths is not None or
                                   args.request_policy_sequence or causality),
                                  capacity,
                                  args.mode, args.phase_diagnostics)
                if (args.phase_diagnostics or args.request_policy_sequence or
                        causality):
                    if not all(row['response_id_valid'] for row in parsed):
                        raise RuntimeError('missing or conflicting response id')
                    ids = {row['response_id'] for row in parsed}
                    if observed_response_ids & ids:
                        raise RuntimeError('response id reused between cases')
                    observed_response_ids.update(ids)
                if not all(isinstance(r['actual_input'], int) and
                           isinstance(r['actual_output'], int) and
                           0 < r['actual_input'] + r['actual_output'] <=
                           capacity['effective']['max_len'] for r in parsed):
                    raise RuntimeError('actual request exceeds effective capacity')
                if args.mode == 'quality':
                    expected = {r['prompt_sha256']: r for r in manifest}
                    for row in parsed:
                        row.update(expected[row['prompt_sha256']])
                        row['exact_match'] = row['text'].strip() == row['expected']
                        row['length_match'] = row['actual_input'] == row['length']
                    results = parsed
                    save(out / 'results.json', results)
                    if len({r['id'] for r in parsed}) != count or not all(
                            r['exact_match'] and r['length_match'] and r['finish'] == ['stop'] for r in parsed):
                        raise RuntimeError('quality acceptance failed')
                    if reference:
                        old = {r['id']: r for r in reference}
                        if any(r['text'] != old[r['id']]['text'] or
                               r['prompt_sha256'] != old[r['id']]['prompt_sha256'] for r in parsed):
                            raise RuntimeError('quality output/prompt differs from reference')
                elif args.mode == 'limits':
                    row = parsed[0]
                    row.update({'id': case.name, 'streaming': streaming,
                                'max_tokens': tokens})
                    results.append(row)
                    save(out / 'results.json', results)
                    if (row['actual_input'] != 1024 or row['actual_output'] != tokens or
                            row['finish'] != ['length'] or row['request_stream'] != streaming):
                        raise RuntimeError('output-limit/finish/stream acceptance failed')
                    if reference:
                        old = next(r for r in reference if r['id'] == row['id'])
                        if (row['text'] != old['text'] or
                                row['prompt_sha256'] != old['prompt_sha256']):
                            raise RuntimeError('output-limit prompt/text differs from reference')
                else:
                    hashes = [hashlib.sha256(r['text'].encode()).hexdigest() for r in parsed]
                    item = {'length': minimum, 'outputs': hashes,
                            'prompt_sha256': parsed[0]['prompt_sha256'],
                            'deterministic': len(set(hashes)) == 1,
                            'performance_scope': plan['scope'],
                            'partial_performance_matrix': plan['partial'],
                            'metrics': [{'ttft': r['ttft'], 'decode_tps':
                                         (r['actual_output'] - 1) / (r['latency'] - r['ttft'])} for r in parsed]}
                    if args.request_policy_sequence:
                        request = plan['requests'][len(results)]
                        item.update({key: request[key] for key in
                                     ('case', 'round', 'position', 'input_tokens')})
                    if causality:
                        request = plan['requests'][len(results)]
                        identity = causality_request_identity(plan, request)
                        item.update(identity)
                        for row in parsed:
                            row.update(identity)
                        save(case / 'responses.json', parsed)
                    results.append(item)
                    save(out / 'results.json', results)
                    if args.request_policy_sequence or causality:
                        previous = sequence_outputs.setdefault(item['prompt_sha256'], hashes[0])
                        if any(value != previous for value in hashes):
                            raise RuntimeError('history output changed across repetitions or positions')
                    expected_output = 256
                    if args.target_total and minimum in extra_lengths:
                        expected_output = args.target_total - minimum
                    if not item['deterministic'] or len({r['prompt_sha256'] for r in parsed}) != 1 or not all(
                            r['actual_input'] == minimum and
                            r['actual_output'] == expected_output and
                            r['finish'] == ['length'] for r in parsed):
                        raise RuntimeError('performance request/determinism acceptance failed')
                    if reference:
                        old = next(r for r in reference if
                                   (r.get('case') == case.name if args.request_policy_sequence
                                    else r['length'] == minimum))
                        expected_prompt = old.get('prompt_sha256')
                        if expected_prompt is None:
                            old_prompt = json.loads((args.reference.parent / f'context-{minimum}/requests.jsonl')
                                                    .read_text().splitlines()[0])['prompt']
                            expected_prompt = hashlib.sha256(old_prompt.encode()).hexdigest()
                        reference_outputs = old['outputs']
                        if (not reference_outputs or len(set(reference_outputs)) != 1 or
                                any(value != reference_outputs[0] for value in hashes) or
                                item['prompt_sha256'] != expected_prompt):
                            raise RuntimeError('performance output/prompt differs from reference')
                print(f'{case.name}: HTTP/output checks passed', flush=True)
                event(out, 'case_completed', case=case.name, requests=len(parsed))
            passed = True
        except Exception as error:
            failure = f'{type(error).__name__}: {error}'
            raise
        finally:
            (out / 'memory-phase.txt').write_text('shutdown\n')
            event(out, 'server_before_shutdown', pid=server.pid)
            cleanup_failure = None
            try:
                try:
                    server.terminate()
                    try:
                        server.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                finally:
                    if hasattr(server, 'close'):
                        server.close()
            except Exception as error:
                cleanup_failure = f'{type(error).__name__}: {error}'
                raise
            finally:
                clean = passed and server.returncode == 0 and cleanup_failure is None
                (out / 'memory-phase.txt').write_text('stopped\n' if cleanup_failure is None else 'cleanup_failed\n')
                event(out, 'server_after_shutdown', pid=server.pid,
                      server_returncode=server.returncode, cleanup_failure=cleanup_failure)
                save(out / 'exit.json', {'server': server.returncode, 'completed': len(results),
                                          'http_output_checks_passed': clean,
                                          'performance_scope': plan['scope'] if plan else None,
                                          'partial_performance_matrix': plan['partial'] if plan else None,
                                          'full_five_tier_completed': bool(plan and plan['full_five_tier_requested'] and clean),
                                          'full_offload_matrix_completed': bool(plan and plan['full_offload_matrix_requested'] and clean),
                                          'performance_acceptance': False,
                                          'phase_diagnostics': args.phase_diagnostics,
                                          'diagnostic_scope': diagnostic_scope,
                                          'failure': failure, 'cleanup_failure': cleanup_failure})
        if server.returncode != 0:
            raise RuntimeError('server did not exit normally')


if __name__ == '__main__':
    main()
