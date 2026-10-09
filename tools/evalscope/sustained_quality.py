"""Frozen sustained-output contracts, source-derived rubrics and pair budget.

No model, tokenizer, HTTP, or generated-code execution occurs in this module.
The output contract compares decoded HTTP text bytes and observed usage counts;
it has no access to generated token IDs. Semantic checks are separate evidence.
"""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time


CAPACITY = {'max_seq': 1, 'max_prefill': 8192, 'max_len': 208896}
SAMPLING = {'max_tokens': 512, 'temperature': 0, 'seed': 20260920,
            'stream': True, 'parallel': 1}
CASE_IDS = ['zh-reasoning', 'en-code', 'en-reasoning', 'zh-code',
            'zh-summary', 'en-summary', 'zh-excerpt', 'en-excerpt']
LENGTHS = [1024, 1024, 4096, 4096, 8196, 8196, 45056, 204800]
BUDGET = {'generation_requests': 16, 'output_tokens': 8192,
          'wall_seconds': 1800, 'evidence_bytes': 64 * 1024 * 1024}
QUESTIONNAIRE = Path(__file__).with_name('fixtures') / 'mtp_sustained_v1.json'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def text_sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False),
                          encoding='utf-8')


def questionnaire(path=QUESTIONNAIRE):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    require(data['schema_version'] == 1 and data['id'] == 'mtp-sustained-v1',
            'wrong questionnaire identity')
    require(data['frozen_before_outputs'] is True, 'rubric must precede outputs')
    require(data['budget'] == BUDGET and data['max_tokens'] == 512 and
            data['minimum_completion_tokens'] == 128, 'changed frozen budget')
    require([c['id'] for c in data['cases']] == CASE_IDS and
            [c['length'] for c in data['cases']] == LENGTHS,
            'changed frozen eight-case matrix')
    return data


def model_identity(model):
    """Bind model location and tokenizer/config metadata; never hash weights."""
    model = Path(model).resolve()
    files = ['config.json', 'tokenizer_config.json', 'tokenizer.json']
    if (model / 'generation_config.json').is_file():
        files.append('generation_config.json')
    return {'directory': str(model),
            'metadata_sha256': {name: file_sha256(model / name)
                                for name in files}}


def validate_inputs(directory, binary, model):
    directory = Path(directory)
    frozen = questionnaire()
    require(file_sha256(directory / 'questionnaire.json') ==
            file_sha256(QUESTIONNAIRE), 'fixture questionnaire differs')
    meta = json.loads((directory / 'identity.json').read_text())
    manifest = json.loads((directory / 'manifest.json').read_text())
    requests = [json.loads(line) for line in
                (directory / 'requests.jsonl').read_text().splitlines()]
    require(meta['binary_sha256'] == file_sha256(binary),
            'fixture belongs to a different binary')
    require(meta['model'] == model_identity(model), 'fixture model identity differs')
    require(meta['capacity'] == CAPACITY and meta['sampling'] == SAMPLING,
            'fixture capacity/sampling differs')
    require(meta['questionnaire_sha256'] == file_sha256(QUESTIONNAIRE) and
            meta['manifest_sha256'] == file_sha256(directory / 'manifest.json') and
            meta['requests_sha256'] == file_sha256(directory / 'requests.jsonl'),
            'fixture bytes differ from prepared identity')
    require(len(manifest) == len(requests) == 8, 'exactly eight fixtures required')
    for case, item, request in zip(frozen['cases'], manifest, requests):
        require(all(item[k] == case[k] for k in
                    ['id', 'language', 'category', 'length']),
                'fixture matrix/order differs')
        require(item['prompt_sha256'] == text_sha256(request['prompt']),
                'fixture prompt hash differs')
        require(request['max_tokens'] == 512 and request['temperature'] == 0 and
                request['seed'] == 20260920 and request['stream'] is True and
                request['stream_options'] == {'include_usage': True},
                'fixture generation parameters differ')
        require(all(block['text'] in request['prompt'] for block in case['blocks'])
                and case['question'] in request['prompt'],
                'fixture source material or question missing')
    return manifest, meta


def ledger_expected(case):
    rubric = case['rubric']
    rows = re.findall(r'(A\d\d) 项目=(\S+) 状态=(\S+) 日期=(\d+) 金额=(\d+)',
                      '\n'.join(b['text'] for b in case['blocks']))
    require(len(rows) == 12, 'ledger source is incomplete')
    expected = []
    for key, project, status, day, amount in rows:
        included = (project == rubric['project'] and status == rubric['status'] and
                    rubric['first_day'] <= int(day) <= rubric['last_day'])
        expected.append((key, '纳入' if included else '排除', int(amount)))
    return expected


def join_expected(case):
    source = '\n'.join(b['text'] for b in case['blocks'])
    services = dict(re.findall(r'service=(\w+) owner=(\w+)', source))
    owners = dict(re.findall(r'owner=(\w+) zone=(\w+)', source))
    revisions = re.findall(r'zone=(\w+) revision=(\d+) status=(\w+) code=(\w+)',
                           source)
    expected = []
    for service, owner in sorted(services.items()):
        zone = owners[owner]
        eligible = [(int(rev), code) for z, rev, status, code in revisions
                    if z == zone and status == 'approved']
        revision, code = max(eligible)
        excluded = [int(rev) for z, rev, status, _ in revisions
                    if z == zone and status != 'approved' and int(rev) > revision]
        expected.append((service, owner, zone, revision, code, excluded))
    require(len(expected) == 8, 'join source is incomplete')
    return expected


def excerpt_expected(case):
    return '\n\n'.join('\n'.join(b['text'].splitlines()[1:-1])
                       for b in case['blocks'])


def code_checks(text, rubric):
    # Parse only. There is deliberately no eval/exec/subprocess for model output.
    checks = {}
    match = re.fullmatch(r'\s*```(?:python)?\n(.*?)\n```\s*', text, re.S)
    checks['single_python_fence'] = match is not None
    if match is None:
        return checks
    try:
        tree = ast.parse(match[1])
    except (SyntaxError, ValueError, RecursionError):
        checks['valid_ast'] = False
        return checks
    checks['valid_ast'] = True
    allowed = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
               ast.Assign, ast.AugAssign, ast.For, ast.If, ast.Compare,
               ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.List, ast.Tuple, ast.Dict,
               ast.Set, ast.Subscript, ast.Slice, ast.Expr, ast.Call, ast.Assert,
               ast.Name, ast.Load, ast.Store, ast.Constant, ast.keyword,
               ast.And, ast.Or, ast.Not, ast.UAdd, ast.USub, ast.Add, ast.Sub,
               ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Eq, ast.NotEq,
               ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is,
               ast.IsNot, ast.Continue, ast.Break, ast.Pass)
    nodes = list(ast.walk(tree))
    checks['static_whitelist'] = all(isinstance(n, allowed) for n in nodes)
    functions = [n for n in nodes if isinstance(n, ast.FunctionDef)]
    checks['one_expected_signature'] = (
        len(functions) == 1 and functions[0].name == rubric['function'] and
        [a.arg for a in functions[0].args.args] == rubric['arguments'] and
        not functions[0].decorator_list and not functions[0].args.defaults and
        not functions[0].args.posonlyargs and not functions[0].args.kwonlyargs and
        functions[0].args.vararg is None and functions[0].args.kwarg is None and
        functions[0].returns is None and
        all(a.annotation is None for a in functions[0].args.args))
    checks['allowed_calls_only'] = all(
        isinstance(n.func, ast.Name) and n.func.id in rubric['allowed_calls']
        for n in nodes if isinstance(n, ast.Call))
    checks['no_private_names'] = all(not n.id.startswith('_') for n in nodes
                                     if isinstance(n, ast.Name))
    checks['six_top_level_asserts'] = (
        sum(isinstance(n, ast.Assert) for n in tree.body) >=
        rubric['minimum_asserts'])
    checks['asserts_call_function'] = all(
        any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and
            n.func.id == rubric['function'] for n in ast.walk(statement.test))
        for statement in tree.body if isinstance(statement, ast.Assert))
    return checks


def semantic_score(case, text):
    rubric = case['rubric']
    kind = rubric['kind']
    checks = {}
    if kind == 'ledger':
        expected = ledger_expected(case)
        found = re.findall(r'^(A\d\d) (纳入|排除) 金额=(\d+)；原因：(.+)$',
                           text, re.M)
        checks['all_classifications_and_amounts'] = (
            [(key, label, int(amount)) for key, label, amount, _ in found] == expected)
        included = [amount for _, label, amount in expected if label == '纳入']
        checks['source_derived_totals'] = bool(re.search(
            rf'^合计：条数={len(included)}，金额={sum(included)}[。]?$', text, re.M))
    elif kind == 'join':
        expected = join_expected(case)
        lines = text.splitlines()
        checks['eight_lines'] = len(lines) == 8
        for i, (service, owner, zone, revision, code, excluded) in enumerate(expected):
            prefix = f'{service} | {owner} | {zone} | {revision} | {code} |'
            line = lines[i] if i < len(lines) else ''
            checks[service + '_joined_fields'] = line.startswith(prefix)
            explanation = line[len(prefix):] if line.startswith(prefix) else ''
            checks[service + '_excluded_revision'] = all(
                re.search(rf'\b{rev}\b', explanation) is not None for rev in excluded)
    elif kind == 'excerpt_exact':
        checks['source_derived_full_excerpt_exact_utf8'] = (
            text.encode('utf-8') == excerpt_expected(case).encode('utf-8'))
    elif kind == 'anchors':
        folded = text.casefold()
        for anchor in rubric['required']:
            checks['anchor:' + anchor] = anchor.casefold() in folded
        for i, choices in enumerate(rubric['alternatives']):
            checks[f'qualified_statement:{i}'] = any(c.casefold() in folded for c in choices)
        sections = re.findall(r'^\s*([1-6])[.、）)]\s*', text, re.M)
        checks['six_numbered_sections'] = sections == list('123456')
    elif kind == 'code_ast':
        checks = code_checks(text, rubric)
    else:
        raise ValueError('unknown frozen rubric')
    return {'kind': kind, 'checks': checks,
            'automatic_rubric_passed': bool(checks) and all(checks.values()),
            'manual_review_required': rubric['manual'],
            'manual_review_status': 'pending' if rubric['manual'] else 'not_required',
            'generated_code_executed': False,
            'functionality_proven': False if kind == 'code_ast' else None}


def analyze_responses(responses, manifest):
    frozen = questionnaire()
    require(len(responses) == len(manifest) == 8, 'eight responses required')
    results = []
    for row, item, case in zip(responses, manifest, frozen['cases']):
        require(row['success'] and row['response_id_valid'], 'HTTP/identity failure')
        require(row['prompt_sha256'] == item['prompt_sha256'], 'prompt/order differs')
        require(not isinstance(row['actual_input'], bool) and
                isinstance(row['actual_input'], int) and
                row['actual_input'] == case['length'], 'observed input length differs')
        require(not isinstance(row['actual_output'], bool) and
                isinstance(row['actual_output'], int) and
                1 <= row['actual_output'] <= 512, 'observed output length invalid')
        require(row['finish'] in (['stop'], ['length']), 'invalid finish')
        require(row['finish'] != ['length'] or row['actual_output'] == 512,
                'length finish below output/context cap')
        require(row['request_stream'] is True, 'sustained request must stream')
        item = {**row, **item}
        item['text_utf8_sha256'] = text_sha256(row['text'])
        item['derived_total_tokens'] = row['actual_input'] + row['actual_output']
        item['usage_source'] = 'evalscope observed prompt_tokens/completion_tokens'
        item['token_ids_available'] = False
        item['coverage_met'] = row['actual_output'] >= 128
        item['early_eos'] = row['finish'] == ['stop'] and row['actual_output'] < 128
        item['semantic'] = semantic_score(case, row['text'])
        results.append(item)
    return results


def compare_pair(plain, sequential):
    require([r['id'] for r in plain] == [r['id'] for r in sequential] == CASE_IDS,
            'pair must contain the frozen ordered eight cases')
    cases = []
    fields = ['prompt_sha256', 'actual_input', 'actual_output', 'finish']
    for left, right in zip(plain, sequential):
        require(left['decode_mode'] == 'plain' and right['decode_mode'] == 'mtp',
                'pair must compare plain with sequential MTP observations')
        checks = {field: left[field] == right[field] for field in fields}
        checks['original_utf8_text'] = (left['text'].encode('utf-8') ==
                                        right['text'].encode('utf-8'))
        cases.append({'id': left['id'], 'checks': checks,
                      'exact_http_pair': all(checks.values()),
                      'coverage_met': left['coverage_met'] and right['coverage_met'],
                      'plain_early_eos': left['early_eos'],
                      'sequential_early_eos': right['early_eos']})
    return {'cases': cases,
            'exact_http_pair_passed': all(c['exact_http_pair'] for c in cases),
            'sustained_coverage_passed': all(c['coverage_met'] for c in cases),
            'compared_token_ids': False,
            'semantic_results_are_independent': True}


def validate_reference(reference, binding, pair_root):
    reference = Path(reference).resolve()
    require(reference == Path(pair_root).resolve() / 'plain/results.json',
            'reference must be this pair plain/results.json')
    root = reference.parent
    identity = json.loads((root / 'sustained-identity.json').read_text())
    require(identity == {'binding': binding, 'decode_mode': 'plain', 'verifier': 'none'},
            'reference binary/model/capacity/fixture/mode identity differs')
    mode = json.loads((root / 'run-mode.json').read_text())
    require(mode['acceptance_mode'] == 'sustained-quality' and
            mode['decode_mode'] == 'plain' and mode['verifier'] == 'none' and
            mode['capacity'] == CAPACITY, 'reference mode record differs')
    require((root / 'binary.sha256').read_text().strip() == binding['binary_sha256'],
            'reference binary digest differs')
    exit_record = json.loads((root / 'exit.json').read_text())
    paths = json.loads((root / 'request-modes.json').read_text())
    require(exit_record['server'] == 0 and exit_record['completed'] == 8 and
            exit_record['actual_mode_checks_passed'] and
            exit_record['http_output_checks_passed'] and paths['passed'],
            'reference HTTP or actual-mode audit incomplete')
    raw = json.loads((root / 'sustained-quality/responses.json').read_text())
    require(file_sha256(root / 'inputs/manifest.json') == binding['manifest_sha256'] and
            file_sha256(root / 'inputs/requests.jsonl') == binding['requests_sha256'] and
            file_sha256(root / 'inputs/questionnaire.json') == binding['questionnaire_sha256'],
            'reference input evidence differs from bound fixture')
    manifest = json.loads((root / 'inputs/manifest.json').read_text())
    require(all(row['decode_mode'] == 'plain' for row in raw),
            'reference observations are not ordinary decode')
    extracted = analyze_responses(raw, manifest)
    saved = json.loads(reference.read_text())
    require(saved == extracted, 'reference extraction differs from preserved observations')
    return extracted


class PairBudget:
    """Exclusive eight-request phase claims and one shared deadline/byte budget.

    Bounds are polled while child processes run; crossing the byte limit aborts
    and preserves the first failure. Files already written are never deleted.
    """
    def __init__(self, root, output, binding, sequential):
        self.root = Path(root).resolve()
        phase = 'sequential' if sequential else 'plain'
        require(Path(output).resolve() == self.root / phase,
                'sustained output must be pair-root/plain or pair-root/sequential')
        self.root.mkdir(parents=True, exist_ok=True)
        state_path = self.root / 'pair-budget.json'
        if not sequential:
            with state_path.open('x') as stream:
                json.dump({'binding': binding, 'budget': BUDGET,
                           'started_unix': time.time(),
                           'deadline_unix': time.time() + BUDGET['wall_seconds']}, stream)
        state = json.loads(state_path.read_text())
        require(state['binding'] == binding and state['budget'] == BUDGET,
                'shared pair identity/budget differs')
        self.deadline = time.monotonic() + state['deadline_unix'] - time.time()
        self.phase = phase
        with (self.root / f'{phase}-claim.json').open('x') as stream:
            json.dump({'phase': phase, 'authorized_generation_requests': 8,
                       'max_output_tokens': 4096, 'claimed_unix': time.time()}, stream)
        self.check()

    def snapshot(self):
        size = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        return {'phase': self.phase, 'evidence_bytes': size,
                'seconds_remaining': self.deadline - time.monotonic()}

    def check(self):
        observed = self.snapshot()
        require(observed['seconds_remaining'] > 0, 'shared 30-minute deadline exceeded')
        require(observed['evidence_bytes'] <= BUDGET['evidence_bytes'],
                'shared 64-MiB evidence budget exceeded')
        return observed

    def run(self, command, **kwargs):
        self.check()
        process = subprocess.Popen(command, **kwargs)
        try:
            while process.poll() is None:
                self.check()
                time.sleep(0.25)
            self.check()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
