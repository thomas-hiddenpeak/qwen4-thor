"""Read-only admission of the frozen plain limits reference.

An explicit exception admits one naturally terminal reference for paired
collection. It never changes either run's output or execution-path coverage.
No database deserialization, tokenizer, server, or model execution is used.
"""
import hashlib
import json
import math
from pathlib import Path
import re

OUTPUT_FAILURE = 'RuntimeError: output-limit/finish/stream acceptance failed'
METADATA = {'id', 'streaming', 'max_tokens', 'length', 'expected_output'}
OBSERVATIONS = {
    'success', 'actual_input', 'actual_output', 'text', 'finish',
    'prompt_sha256', 'ttft', 'latency', 'decode_mode', 'request_stream',
    'response_id', 'response_id_valid', 'response_ids_observed',
    'response_id_source'}
MATRIX = [
    (f'{"stream" if stream else "nonstream"}-{limit}', 1024, limit, stream)
    for stream in (True, False) for limit in (1, 2, 3, 4, 5, 8)
] + [('context-208891-stream-8', 208891, 8, True),
     ('context-208892-nonstream-8', 208892, 8, False)]


def require(condition, message):
    if not condition:
        raise ValueError('admission limits reference: ' + message)


def validate_early_eos_option(mode, mtp, verifier, reference, enabled):
    require(not enabled or (mode == 'admission-limits' and mtp and
                            verifier == 'sequential' and reference is not None),
            '--allow-reference-early-eos requires admission-limits, --mtp, '
            '--mtp-verifier sequential and --reference')


def validate_admission_reference(reference_path, binding, prepared,
                                 allow_early_eos=False):
    """Return unchanged rows and a source-bound collection-admission report."""
    from acceptance_mode import request_mode_evidence, startup_evidence
    from sustained_quality import CAPACITY

    reference_path = Path(reference_path).resolve()
    root = reference_path.parent
    prepared = Path(prepared)
    sources = []

    def read(path, parse=True):
        data = path.read_bytes()
        sources.append({'path': str(path.resolve()), 'bytes': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
        return json.loads(data) if parse else data.decode('utf-8')

    rows = read(reference_path)
    identity = read(root / 'admission-identity.json')
    mode = read(root / 'run-mode.json')
    exit_status = read(root / 'exit.json')
    binary_sha = read(root / 'binary.sha256', False).strip()
    require(identity == binding and binding.get('capacity') == CAPACITY and
            binary_sha == binding.get('binary_sha256'), 'identity differs')
    require(mode.get('acceptance_mode') == 'admission-limits' and
            mode.get('decode_mode') == 'plain' and
            mode.get('verifier') == 'none' and mode.get('capacity') == CAPACITY,
            'mode/capacity differs')
    require(type(exit_status.get('server')) is int and
            exit_status['server'] == 0 and
            type(exit_status.get('completed')) is int and
            exit_status['completed'] == 14 and
            exit_status.get('decode_mode') == 'plain' and
            exit_status.get('actual_mode_checks_passed') is True,
            'server/completion/execution mode failed')
    output_passed = exit_status.get('http_output_checks_passed')
    require(type(output_passed) is bool, 'missing output coverage result')
    require((output_passed and exit_status.get('failure') is None) or
            (allow_early_eos and not output_passed and
             exit_status.get('failure') == OUTPUT_FAILURE),
            'output checks failed without the explicit early-EOS exception')
    require(isinstance(rows, list) and len(rows) == 14 and
            all(isinstance(row, dict) for row in rows) and
            [row.get('id') for row in rows] == [case[0] for case in MATRIX],
            'matrix/order differs')

    prompts = {}
    for length in (1024, 208891, 208892):
        relative = f'context-{length}/requests.jsonl'
        old = read(root / 'inputs' / relative)
        new = read(prepared / relative)
        require(isinstance(old.get('prompt'), str) and
                old['prompt'] == new.get('prompt'), 'fixture prompt differs')
        prompts[length] = old['prompt']

    admitted = []
    response_ids = set()
    for index, (row, (case_id, length, limit, stream)) in enumerate(
            zip(rows, MATRIX)):
        raw = read(root / case_id / 'responses.json')
        case_input = read(root / case_id / 'requests.jsonl')
        require(isinstance(raw, list) and len(raw) == 1 and
                isinstance(raw[0], dict) and set(raw[0]) == OBSERVATIONS and
                set(row) == OBSERVATIONS | METADATA,
                case_id + ': missing/extra response observations')
        observation = {key: value for key, value in row.items()
                       if key not in METADATA}
        # Canonical JSON also distinguishes booleans from integer observations.
        require(json.dumps(raw[0], sort_keys=True, allow_nan=False) ==
                json.dumps(observation, sort_keys=True, allow_nan=False),
                case_id + ': results differ from original responses')
        expected = min(limit, CAPACITY['max_len'] - length)
        require(type(row['length']) is int and row['length'] == length and
                type(row['max_tokens']) is int and row['max_tokens'] == limit and
                type(row['expected_output']) is int and
                row['expected_output'] == expected and row['streaming'] is stream,
                case_id + ': frozen metadata differs')
        require(type(row['success']) is int and row['success'] == 1 and
                type(row['actual_input']) is int and row['actual_input'] == length and
                type(row['actual_output']) is int and
                1 <= row['actual_output'] <= expected and
                isinstance(row['text'], str) and row['decode_mode'] == 'plain' and
                row['request_stream'] is stream,
                case_id + ': HTTP success/usage/mode/stream invalid')
        require(case_input.get('prompt') == prompts[length] and
                row['prompt_sha256'] ==
                hashlib.sha256(prompts[length].encode('utf-8')).hexdigest(),
                case_id + ': response/case prompt differs from fixture')
        response_id = row['response_id']
        require(row['response_id_valid'] is True and
                isinstance(response_id, str) and
                re.fullmatch(r'[A-Za-z0-9_-]{1,128}', response_id) and
                response_id not in response_ids and
                isinstance(row['response_ids_observed'], list) and
                bool(row['response_ids_observed']) and
                all(value == response_id for value in row['response_ids_observed']) and
                row['response_id_source'] == 'actual HTTP/SSE response id fields',
                case_id + ': invalid/duplicate response identity')
        response_ids.add(response_id)
        require(all(type(row[key]) in (int, float) and
                    math.isfinite(row[key]) and row[key] >= 0
                    for key in ('ttft', 'latency')) and
                row['latency'] >= row['ttft'], case_id + ': invalid timing')
        covered = row['actual_output'] == expected and row['finish'] == ['length']
        early_eos = (index == 13 and case_id == 'context-208892-nonstream-8' and
                     expected == 4 and 1 <= row['actual_output'] < 4 and
                     row['finish'] == ['stop'])
        require(covered or (allow_early_eos and not output_passed and early_eos),
                case_id + ': output coverage failed outside the terminal exception')
        admitted.append({'id': case_id, 'coverage_passed': covered,
                         'reference_early_eos': not covered,
                         'expected_output': expected,
                         'actual_output': row['actual_output']})
    missing = [row['id'] for row in admitted if not row['coverage_passed']]
    require((output_passed and not missing) or
            (not output_passed and missing == [MATRIX[-1][0]]),
            'saved output result is inconsistent with observations')
    log = read(root / 'server.log', False)
    startup = startup_evidence(log, False)
    paths = request_mode_evidence(log, rows, False)
    require(startup == read(root / 'startup-mode.json') and startup['passed'] and
            paths == read(root / 'request-modes.json') and paths['passed'] and
            all(item.get('coverage') == 'terminal_request_path'
                for item in paths['bindings']),
            'original startup/response path audit differs or failed')
    return rows, {'schema_version': 1, 'reference': str(reference_path),
                  'collection_admitted': True,
                  'allow_reference_early_eos': allow_early_eos,
                  'reference_http_output_checks_passed': output_passed,
                  'reference_coverage_passed': not missing,
                  'reference_failure': exit_status.get('failure'),
                  'missing_coverage': missing, 'cases': admitted,
                  'strict_output_and_path_requirements_unchanged': True,
                  'sources': sources}
