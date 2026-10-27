"""Freeze D only after C's fixed-cap transaction gate passes."""
import copy
import hashlib
import json
from pathlib import Path
import sys

from run_stage import ROOT, save
from oracle import short_answer


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    from transformers import AutoTokenizer
    base = ROOT / '.q4t-work/session-cache-20261027'
    gate = base / 'performance-gate.json'
    if not json.loads(gate.read_text())['passed']:
        raise RuntimeError('C transaction gate has not passed')
    source = base / 'phase-c-subset/plan.json'
    plan = copy.deepcopy(json.loads(source.read_text()))
    plan['short_expected'] = short_answer(plan)
    target = base / 'phase-d'
    target.mkdir()
    fixtures = target / 'fixtures'
    fixtures.mkdir()
    records = [json.loads(line) for line in
               Path(plan['quality_case']['path']).read_text().splitlines()]
    candidates = [(row['prompt'], plan['quality'][hashlib.sha256(
        row['prompt'].encode()).hexdigest()]) for row in records]
    prompt, quality = next((p, q) for p, q in candidates if q['length'] == 45056)
    question = ('Question: What is the audit_code in the authoritative audit '
                'record? Return only the six digits, with no explanation.')
    if prompt.count(question) != 1:
        raise ValueError('frozen recall question not unique')
    first = prompt.replace(question, 'Reply with the single digit 7.')
    first_path = fixtures / 'memory-first.txt'
    first_path.write_text(first)
    tokenizer = AutoTokenizer.from_pretrained(plan['model'], local_files_only=True)
    first_tokens = len(tokenizer.encode(first, add_special_tokens=False))
    if not 40000 < first_tokens < 46000:
        raise ValueError('recall prompt out of scope')
    memory = dict(id='session-memory', first=str(first_path),
        first_tokens=first_tokens, expected=quality['expected'],
        source_sha256=quality['prompt_sha256'],
        append='<|im_end|>\n<|im_start|>user\n'
        'What is the audit_code in the authoritative audit record earlier? '
        'Return only the six digits, with no explanation.<|im_end|>\n'
        '<|im_start|>assistant\n<think>\n\n</think>\n\n')
    plan['protection'].append(memory)
    plan.update(stage='SESSION_D', output=str(target / 'run'),
        http_requests=94, http_loads=6, stage_timeout_seconds=9000,
        required_gates=[str(base / 'correctness-gate.json'), str(gate)],
        design='Fixed five-tier single-concurrency parent/candidate protection; '
        'AB/BA ordering for full/subset. Three requests per tier, descriptive '
        'only. Candidate cache is enabled but protection requests have no ID. '
        'Two-turn recall runs once per candidate head with an explicit ID.',
        order=['full parent', 'full candidate', 'subset candidate',
               'subset parent', 'plain parent', 'plain candidate'], steps=[])
    path = target / 'plan.json'
    arms = [('full', False), ('full', True), ('subset', True),
            ('subset', False), ('plain', False), ('plain', True)]
    for number, (head, candidate) in enumerate(arms):
        cached = candidate and head != 'plain'
        name = f'{number:02d}-{head}-' + ('candidate' if candidate else 'parent')
        plan['steps'].append(dict(name=name, head=head, cache=cached,
            binary=plan['candidate'] if candidate else plan['parent'],
            kind='block', http_requests=17 if cached else 15, http_loads=1,
            timeout=1500, command=[sys.executable, '-B',
                str(ROOT / 'tools/session_cache/run_protection.py'),
                '--plan', str(path), '--custom-step', name]))
    # Revalidate existing bindings instead of silently refreshing identities.
    for name, expected in plan['files'].items():
        if sha(name) != expected:
            raise ValueError('C input changed before D: ' + name)
    additions = [first_path, source, gate, Path(plan['parent']),
        ROOT / 'tools/session_cache/prepare_protection.py',
        ROOT / 'tools/session_cache/run_protection.py',
        ROOT / 'tools/session_cache/analyze_performance.py',
        ROOT / 'tools/session_cache/analyze_protection.py',
        ROOT / 'tools/session_cache/analyze_resources.py',
        ROOT / 'tools/session_cache/audit_evidence.py',
        ROOT / 'tools/mtp_suffix_agent/analyze.py',
        ROOT / 'tools/mtp_suffix_agent/resources.py']
    additions += [Path(c['path']) for c in plan['protection'] if 'path' in c]
    for item in additions:
        plan['files'][str(item)] = sha(item)
    save(path, plan)
    print(json.dumps(dict(plan=str(path), sha256=sha(path), requests=94,
                          starts=6, memory=memory), indent=2))


if __name__ == '__main__':
    main()
