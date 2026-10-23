"""Freeze the numeric-suffix candidate's single bounded HTTP experiment.

Only CPU fixture construction and identity capture occur here. No inference.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/'
             'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
LENGTHS = [1024, 4096, 8192, 45056, 204800]
SEED = 202610230731


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def fixture(directory, name, prompt, tokenizer, role, count=1, nominal=None):
    path = directory / (name + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((json.dumps({'prompt': prompt}, ensure_ascii=False) + '\n') * count)
    actual = len(tokenizer.encode(prompt, add_special_tokens=False))
    return dict(id=name, path=str(path), input_tokens=actual,
                input_min=nominal - 48 if nominal else actual,
                input_max=nominal if nominal else actual,
                nominal_input=nominal, output_tokens=256, repeats=count,
                role=role, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())


def fit_records(records, render, tokenizer, target):
    """Select a complete-record prefix; never consult generated output."""
    low, high = 0, len(records)
    while low < high:
        middle = (low + high + 1) // 2
        size = len(tokenizer.encode(render(records[:middle]), add_special_tokens=False))
        if size <= target:
            low = middle
        else:
            high = middle - 1
    prompt = render(records[:low])
    size = len(tokenizer.encode(prompt, add_special_tokens=False))
    if not low or not target - 48 <= size <= target:
        raise RuntimeError(f'complete-record fixture outside frozen range: {target}, {size}')
    return prompt, low


def make_fixtures(directory, model, old_stage):
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
    directory.mkdir()
    shutil.copytree(old_stage / 'fixtures/quality', directory / 'quality')
    if len(json.loads((directory / 'quality/manifest.json').read_text())) != 11:
        raise RuntimeError('frozen quality gate requires exactly eleven cases')
    notes = [
        'The library team photographs each repaired binding before returning the book. '
        'The original damage description remains attached to the catalog entry.',
        'The greenhouse controller compares humidity readings from separate probes. '
        'A delayed sample is marked explicitly before the irrigation schedule changes.',
        'The ferry workshop records each replaced component and its inspection date. '
        'A repair note identifies the technician responsible for the final check.',
        'The museum stores fragile specimens in labelled drawers. '
        'An access ledger distinguishes temporary loans from permanent transfers.',
        'The bakery weighs flour before mixing each batch. '
        'A calibration record explains when the scale was adjusted and who verified it.',
        'The mapping team keeps the original survey points beside corrected coordinates. '
        'Reviewers can compare both versions before approving a revised route.',
        'The radio station archives the original recording before editing an interview. '
        'A transcript identifies uncertain words without silently replacing them.',
        'The orchard crew separates observations from proposed treatments. '
        'A later inspection checks whether the recorded changes persisted.',
    ]
    rng = random.Random(SEED)
    records = [f'Entry {i}: {rng.choice(notes)}\n' for i in range(1, 10001)]
    prefix = 'Operational review archive. Read all numbered entries carefully.\n'
    suffix = ('\nTask: Write a detailed operational review in at least 600 words. '
              'Discuss traceability, measurements, custody, maintenance, and review decisions.\nAnswer:')
    render = lambda rows: prefix + ''.join(rows) + suffix
    performance = []
    for length in LENGTHS:
        prompt, entries = fit_records(records, render, tokenizer, length)
        row = fixture(directory / 'performance' / f'context-{length}', 'requests',
                      prompt, tokenizer, 'held_out_acceptance', 3, length)
        row.update(id=f'context-{length}', record_count=entries, seed=SEED)
        performance.append(row)
    save(directory / 'performance/manifest.json', performance)

    def chat(body):
        return tokenizer.apply_chat_template(
            [{'role': 'system', 'content': 'Follow the writing instruction.'},
             {'role': 'user', 'content': body}], tokenize=False,
            add_generation_prompt=True, enable_thinking=False)

    code = '''def smooth_readings(samples, window_size=8):
    clipped = [min(1024, max(0, value)) for value in samples]
    running = []
    for index in range(0, len(clipped), 2):
        window = clipped[max(0, index - window_size + 1):index + 1]
        running.append(sum(window) / max(1, len(window)))
    return {"revision": 3, "interval_ms": 250, "readings": running}
'''
    prose = ('In the old printing workshop, a conservator examines the paper under '
             'soft light before choosing a repair. A narrow strip of matching tissue '
             'supports the damaged edge without covering the original lettering. '
             'The treatment notebook records the adhesive, the drying conditions, '
             'and the reason for each decision. After the sheet rests overnight, '
             'another conservator checks whether the repair bends naturally. '
             'The completed page returns to its folder with the earlier photographs '
             'so that a future examination can trace every intervention.\n')
    bodies = [
        ('repeat-code', 'Repeat the following Python block verbatim at least twenty '
         'times. Output only the repeated code, without fences or commentary. '
         'Continue repeating until the output limit.\n\n' + code * 4),
        ('repeat-text', 'Repeat the following paragraph verbatim at least twenty '
         'times. Output only repeated paragraphs, with no numbering or introduction. '
         'Continue repeating until the output limit.\n\n' + prose * 4),
        ('nonrepeat', 'Write an original 1200-word practical essay about converting '
         'an abandoned railway depot into a community music school. Discuss acoustic '
         'treatment, accessible entrances, instrument storage, evening transport, '
         'teaching schedules, volunteer training, heritage preservation, ventilation, '
         'and funding tradeoffs. Develop each paragraph with new concrete examples. '
         'Avoid repeating phrases or quoting these instructions. Start the essay '
         'immediately without an outline or summary.'),
    ]
    scenarios = [fixture(directory / 'scenarios', name, chat(body), tokenizer,
                         'held_out_acceptance') for name, body in bodies]
    numeric_rng = random.Random(SEED + 1)
    categories = ['amber', 'violet', 'silver']
    numeric_rows = [f'Entry {i}: category={numeric_rng.choice(categories)}; '
                    'inspection completed; the receiving clerk checked the label '
                    'against the signed dispatch note before filing the record.\n'
                    for i in range(1, 601)]
    numeric_task = ('\nList every amber entry number in increasing order, separated '
                    'by a comma and one space. Output only the number list, without '
                    'labels, explanations, or code fences. After finishing the '
                    'complete list, repeat that same list from its beginning. '
                    'Continue listing until the output limit.')
    numeric_render = lambda rows: chat('Dispatch inspection ledger:\n' + ''.join(rows) + numeric_task)
    numeric_prompt, entries = fit_records(numeric_rows, numeric_render, tokenizer, 8192)
    numeric = fixture(directory / 'scenarios', 'numeric-enum', numeric_prompt,
                      tokenizer, 'held_out_acceptance', nominal=8192)
    numeric.update(record_count=entries, seed=SEED + 1,
                   note='Requested numeric enumeration; neither zero suffix hits nor semantic accuracy is assumed.')
    scenarios.append(numeric)
    save(directory / 'scenarios/manifest.json', scenarios)

    legacy = []
    for name, source in [
        ('old-8k', old_stage / 'fixtures/performance/context-8192/requests.jsonl'),
        ('old-repeat-code', old_stage / 'scenarios/repeat-code.jsonl'),
        ('old-repeat-text', old_stage / 'scenarios/repeat-text.jsonl'),
    ]:
        prompt = json.loads(source.read_text().splitlines()[0])['prompt']
        row = fixture(directory / 'development', name, prompt, tokenizer,
                      'development_regression')
        row.update(source_path=str(source), source_sha256=sha(source))
        legacy.append(row)
    save(directory / 'development/manifest.json', legacy)
    return performance, scenarios, legacy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--contracts', type=Path, required=True)
    parser.add_argument('--reuse-record', type=Path, required=True)
    parser.add_argument('--parent', type=Path, default=ROOT / 'build/q4t')
    parser.add_argument('--artifact', type=Path, default=ROOT /
                        '.q4t-work/sha2-startup-20261023/delivery-package/artifact')
    parser.add_argument('--old-stage', type=Path, default=ROOT /
                        '.q4t-work/suffix-k3-20261023/acceptance-v1')
    parser.add_argument('--model', type=Path, default=MODEL)
    parser.add_argument('--port', type=int, default=8754)
    parser.add_argument('--extra-file', type=Path, action='append', default=[])
    args = parser.parse_args()
    stage, candidate, installed, artifact, old_stage, model = [p.resolve() for p in
        (args.stage, args.candidate, args.parent, args.artifact, args.old_stage, args.model)]
    if candidate == installed or not stage.is_relative_to(ROOT / '.q4t-work'):
        parser.error('use a separate candidate and a new stage under .q4t-work')
    contracts = json.loads(args.contracts.read_text())
    if (not isinstance(contracts, list) or not contracts or
            any(not s.get('command') or not 0 < s.get('timeout', 0) <= 1200 for s in contracts)
            or sum(s['timeout'] for s in contracts) > 1200):
        parser.error('nonempty contracts must share <=1200 seconds')
    stage.mkdir(parents=True, exist_ok=False)
    output = stage / 'run'
    parent_dir = stage / 'parent'
    parent_dir.mkdir()
    release_path = installed.with_name(installed.name + '.release.json')
    release = json.loads(release_path.read_text())
    cache = Path(release['build_cache'])
    if sha(installed) != release['binary_sha256'] or sha(cache) != release['build_cache_sha256']:
        raise RuntimeError('installed binary/release/build-cache identity mismatch')
    shutil.copy2(installed, parent_dir / 'q4t')
    shutil.copy2(cache, parent_dir / 'CMakeCache.txt')
    shutil.copy2(release_path, parent_dir / 'q4t.release.json')
    parent = parent_dir / 'q4t'
    if not (candidate.parent / 'CMakeCache.txt').is_file():
        raise RuntimeError('candidate requires adjacent actual CMakeCache.txt')
    performance, scenarios, development = make_fixtures(stage / 'fixtures', model, old_stage)
    reuse = json.loads(args.reuse_record.read_text())
    save(stage / 'reused-evidence.json', reuse)
    plan = dict(version=1, classification='performance optimization: fixed-k3 numeric suffix admission',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_note='commit plus frozen files and source snapshot; dirty source is captured',
        candidate=str(candidate), candidate_sha256=sha(candidate),
        parent=str(parent), parent_sha256=sha(parent), parent_release=release,
        artifact=str(artifact), subset_manifest_sha256=sha(artifact / 'manifest.json'),
        model=str(model), port=args.port, output=str(output), lease=str(stage / 'lease'),
        stage_timeout_seconds=14400, direct_contract_timeout_seconds=1200,
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=5,
        http_requests=238, http_loads=18, deployment_separate_max_requests=3,
        deployment_separate_max_loads=3, total_authorized_max_http=241,
        retries=0, warmups=0,
        scope='single sequence, greedy, MTP k3; full head default; subset explicit',
        goals={'repeat_gain': 'each head, each new/old code/text scenario: geomean(ON/OFF paired decode ratios)>=1.05 AND each block ratio>1',
               'old_eight_k': 'both paired blocks individually no decode decline, aggregate nonnegative, and whole-generation verify plus ordinary-tail counts do not increase relative to same-block suffix OFF; no additional restore-count gate; development regression only',
               'aggregation': 'geometric mean of the two ON/OFF paired ratios; retain each block and do not offset a credible protection regression'},
        protections=['direct target/draft/index/token-table contracts and lifecycle',
                     '22/22 full/subset ON exact retrieval answers; plain quality reuse only with unchanged dependencies',
                     'new five-tier decode/TTFT per head and block',
                     'new nonrepeat/numeric-enum and contemporary parent/candidate plain five-tier',
                     'output identity, 256-token finish, bounded index/table resources and unified RAM'],
        statistics='two startup-level paired blocks per head with AB/BA; three within-start requests per tier; one per scenario/start; no reliable population noninferiority claim',
        cache='natural inherited page cache; startup auto; no drop_caches/manual prewarming/extra warmup/SSD-cold claim',
        order_note='block1 full OFF->ON, subset ON->OFF; block2 full ON->OFF, subset OFF->ON',
        stop='first HTTP/quality/contract/identity failure, timeout or ENV_ABORT; no fix/retry/additional sampling within this candidate',
        diagnostics='four separate old-8K timing requests after regular samples; never acceptance throughput',
        reused_evidence=reuse, performance=performance, scenarios=scenarios,
        development=development, steps=[], files={}, model_file_metadata={})
    runner = ROOT / 'tools/evalscope/run_acceptance.py'
    custom = ROOT / 'tools/mtp_suffix_numeric/run_stage.py'

    def standard(name, mode, head, suffix=False, old=False, reference=None):
        command = [sys.executable, '-B', str(runner), '--mode', mode, '--output', str(output/name),
                   '--binary', str(parent if old else candidate), '--model-dir', str(model),
                   '--fixtures', str(stage/'fixtures'/mode), '--startup-timeout', '900',
                   '--port', str(args.port), '--max-len', '208896', '--allow-unqualified-binary']
        if head != 'plain': command += ['--mtp', '--mtp-k', '3']
        if head == 'subset': command += ['--mtp-subset-head', str(artifact)]
        if suffix: command += ['--mtp-suffix']
        if reference: command += ['--reference', str(reference)]
        plan['steps'].append(dict(name=name, command=command, timeout=2400,
            http_requests=11 if mode=='quality' else 15, http_loads=1,
            kind=mode, head=head, suffix=suffix))

    def special(name, head, suffix, kind, cases=None, block=None):
        cases = cases or []
        count = 3 if kind == 'lifecycle' else sum(c['repeats'] for c in cases)
        plan['steps'].append(dict(name=name, timeout=2400 if kind=='block' else 900,
            http_requests=count, http_loads=1, kind=kind, head=head, suffix=suffix,
            block=block, cases=cases, command=[sys.executable, '-B', str(custom),
            '--plan', str(stage/'frozen-plan.json'), '--custom-step', name]))

    standard('quality-full-on', 'quality', 'full', True)
    for contract in contracts:
        plan['steps'].append(dict(contract, kind='contract', http_requests=0, http_loads=0))
    standard('quality-subset-on', 'quality', 'subset', True)
    for block in [1, 2]:
        for head in ['full', 'subset']:
            order = [False, True] if (block == 1) == (head == 'full') else [True, False]
            for enabled in order:
                special(f'block-{block}-{head}-' + ('on' if enabled else 'off'), head, enabled,
                        'block', performance + scenarios + development, block)
    standard('performance-parent-plain', 'performance', 'plain', old=True)
    standard('performance-candidate-plain', 'performance', 'plain',
             reference=output/'performance-parent-plain/results.json')
    for head in ['full', 'subset']:
        special('lifecycle-'+head, head, True, 'lifecycle')
    for head, order in [('full', [False, True]), ('subset', [True, False])]:
        for enabled in order:
            special('timing-'+head+('-on' if enabled else '-off'), head, enabled,
                    'timing', [development[0]])
    if (sum(s['http_requests'] for s in plan['steps']) != 238 or
            sum(s['http_loads'] for s in plan['steps']) != 18 or
            len({s['name'] for s in plan['steps']}) != len(plan['steps'])):
        raise RuntimeError('frozen step/budget accounting mismatch')

    paths = [candidate, candidate.parent/'CMakeCache.txt', parent, parent_dir/'CMakeCache.txt',
             parent_dir/'q4t.release.json', installed, release_path, cache,
             args.contracts.resolve(), args.reuse_record.resolve(), ROOT/'CMakeLists.txt', ROOT/'AGENTS.md']
    paths += [ROOT/'docs'/name for name in ['EVALUATION.md',
              'PERFORMANCE_DECISION_PROTOCOL.md', 'MTP_SUFFIX_NUMERIC_GATE_2026-10-23.md']]
    paths += [p.resolve() for p in args.extra_file]
    for contract in contracts:
        paths += [Path(p).resolve() for p in contract.get('files', [])]
        executable = Path(contract['command'][0])
        if executable.is_absolute() and executable.is_file(): paths.append(executable)
    for folder in ['src','include','tests','cmake','tools/evalscope','tools/mtp_suffix','tools/mtp_suffix_numeric']:
        paths += [p for p in (ROOT/folder).rglob('*') if p.is_file()
                  and not any(part in ('.venv','__pycache__','.git') for part in p.parts)]
    for folder in [stage/'fixtures', artifact]:
        paths += [p for p in folder.rglob('*') if p.is_file()]
    paths += [Path(row['source_path']) for row in development]
    manifest = json.loads((artifact/'manifest.json').read_text())
    paths += [model/name for name in ['config.json','generation_config.json','tokenizer.json',
                                    'tokenizer_config.json',manifest['source_shard']]]
    for path in sorted(model.glob('*.safetensors')):
        stat = path.stat()
        plan['model_file_metadata'][str(path)] = dict(size=stat.st_size,mtime_ns=stat.st_mtime_ns,
                                                    device=stat.st_dev,inode=stat.st_ino)
    snapshot = stage/'source-snapshot'
    snapshot.mkdir()
    for path in sorted(set(paths)):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target = snapshot/path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path,target)
    (snapshot/'worktree.patch').write_bytes(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT))
    plan['source_snapshot'] = str(snapshot)
    save(stage/'frozen-plan.json',plan)
    print(json.dumps({k:plan[k] for k in ['output','lease','http_requests','http_loads',
          'stage_timeout_seconds','candidate_sha256']},indent=2))


if __name__ == '__main__':
    main()
