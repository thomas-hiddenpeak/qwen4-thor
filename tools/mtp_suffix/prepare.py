"""Freeze one suffix acceptance stage. Preparation performs no inference."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/'
             'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
LENGTHS = [1024, 4096, 8192, 45056, 204800]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def make_scenarios(directory, model):
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
    code = '''def reconcile_inventory(entries):
    available = {}
    for entry in entries:
        name = entry["name"]
        quantity = entry["quantity"]
        available[name] = available.get(name, 0) + quantity
    return sorted(available.items())

def summarize_inventory(entries):
    rows = reconcile_inventory(entries)
    total = sum(quantity for name, quantity in rows)
    return {"items": rows, "total": total}
'''
    prose = ('At the coastal observatory, the evening team checks each sensor '
             'before recording the temperature. A handwritten notebook stores '
             'the original reading, the instrument identifier, and the time of '
             'observation. The morning team compares the notebook with the '
             'digital archive. When the two records disagree, both versions '
             'remain visible until a second measurement explains the difference. '
             'The final report separates observations from interpretations and '
             'lists the questions that still require an answer.\n')
    requests = [
        ('repeat-code', 'Repeat the following Python block verbatim at least '
         'twenty times. Output only the code, with no fences, introduction, or '
         'conclusion. Keep repeating until your output limit.\n\n' + code * 4),
        ('repeat-text', 'Repeat the following paragraph verbatim at least '
         'twenty times. Output only the paragraph repetitions, with no numbering, '
         'introduction, or conclusion. Keep repeating until your output limit.\n\n'
         + prose * 4),
        ('nonrepeat', 'Write an original report of at least 1200 words about '
         'planning an interdisciplinary mountain research expedition. Discuss '
         'route selection, snow chemistry, acoustic wildlife surveys, solar '
         'power storage, satellite communication, food preservation, equipment '
         'repair, archival photographs, and teaching local students. Include '
         'specific examples and tradeoffs. Each paragraph must contribute new '
         'information. Avoid repeating phrases or quoting this instruction. '
         'Start the report immediately, without an outline or summary.'),
    ]
    directory.mkdir()
    manifest = []
    for name, body in requests:
        prompt = tokenizer.apply_chat_template(
            [{'role': 'system', 'content': 'Follow the writing instruction.'},
             {'role': 'user', 'content': body}], tokenize=False,
            add_generation_prompt=True, enable_thinking=False)
        length = len(tokenizer.encode(prompt, add_special_tokens=False))
        path = directory / (name + '.jsonl')
        path.write_text((json.dumps({'prompt': prompt}, ensure_ascii=False)
                         + '\n') * 3)
        manifest.append(dict(id=name, path=str(path), input_tokens=length,
                             output_tokens=256, repeats=3,
                             prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                             role='held-out acceptance; never used for tuning'))
    save(directory / 'manifest.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--contracts', type=Path, required=True,
                        help='JSON list of {name, command, timeout}; total <=1200s')
    parser.add_argument('--parent', type=Path, default=ROOT / 'build/q4t')
    parser.add_argument('--artifact', type=Path, default=ROOT /
                        '.q4t-work/sha2-startup-20261023/delivery-package/artifact')
    parser.add_argument('--fixtures', type=Path, default=ROOT /
                        '.q4t-work/mtp-subset-head-20261023/fixtures')
    parser.add_argument('--model', type=Path, default=MODEL)
    parser.add_argument('--port', type=int, default=8754)
    parser.add_argument('--reuse-record', type=Path, required=True,
                        help='Reviewed JSON recording each reused evidence scope and dependencies')
    parser.add_argument('--extra-file', type=Path, action='append', default=[],
                        help='Additional build log/identity/input to freeze (repeatable)')
    args = parser.parse_args()
    stage, candidate, parent, artifact, model = [p.resolve() for p in
        (args.stage, args.candidate, args.parent, args.artifact, args.model)]
    installed_parent = parent
    if candidate == parent:
        parser.error('candidate must not replace the installed parent before acceptance')
    if not stage.is_relative_to(ROOT / '.q4t-work'):
        parser.error('stage must be under this checkout .q4t-work')
    contracts = json.loads(args.contracts.read_text())
    if (not isinstance(contracts, list) or not contracts or
            any(not s.get('command') or not 0 < s.get('timeout', 0) <= 1200
                for s in contracts) or sum(s['timeout'] for s in contracts) > 1200):
        parser.error('nonempty direct contracts must share a <=1200-second budget')
    # A new directory prevents mixing evidence with a failed or sealed stage.
    stage.mkdir(parents=True, exist_ok=False)
    output = stage / 'run'
    parent_dir = stage / 'parent'
    parent_dir.mkdir()
    shutil.copy2(parent, parent_dir / 'q4t')
    release_path = parent.with_name(parent.name + '.release.json')
    release = json.loads(release_path.read_text())
    if sha(parent) != release['binary_sha256']:
        raise RuntimeError('parent binary/release identity mismatch')
    cache = Path(release['build_cache'])
    if sha(cache) != release['build_cache_sha256']:
        raise RuntimeError('parent actual build-cache identity mismatch')
    shutil.copy2(cache, parent_dir / 'CMakeCache.txt')
    shutil.copy2(release_path, parent_dir / 'q4t.release.json')
    parent = parent_dir / 'q4t'
    if not (candidate.parent / 'CMakeCache.txt').is_file():
        raise RuntimeError('candidate requires actual adjacent CMakeCache.txt')
    fixtures = stage / 'fixtures'
    shutil.copytree(args.fixtures, fixtures)
    scenarios = make_scenarios(stage / 'scenarios', model)
    reuse = json.loads(args.reuse_record.read_text())
    save(stage / 'reused-evidence.json', reuse)
    plan = dict(
        version=1, classification='performance optimization: fixed-k3 suffix',
        source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_note='commit plus frozen files and source snapshot; includes dirty source',
        candidate=str(candidate), candidate_sha256=sha(candidate),
        parent=str(parent), parent_sha256=sha(parent),
        parent_release=release, artifact=str(artifact),
        subset_manifest_sha256=sha(artifact / 'manifest.json'),
        model=str(model), port=args.port, output=str(output),
        lease=str(stage / 'lease'), stage_timeout_seconds=14400,
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=5,
        direct_contract_timeout_seconds=1200, http_requests=177,
        http_loads=19, deployment_separate_max_requests=3,
        deployment_separate_max_loads=3, retries=0, warmups=0,
        scope='single sequence, greedy, MTP k3; full head default; subset explicit',
        goal='each head separately: >=5% median final decode gain on BOTH repeated code and text',
        goal_note='descriptive investment threshold; not a tolerated protection-item loss',
        protections=['all direct target/draft/history/lifecycle contracts',
                     '33/33 exact retrieval answers',
                     'six five-tier matrices including concurrent-stage parent/candidate plain',
                     'per-tier TTFT/plain decode, deterministic output and full 256-token finish',
                     'nonrepeat scenario, index initialization/growth/free and unified RAM'],
        statistics='one startup per arm/group; no cross-start noninferiority or fixed noise band',
        cache='natural inherited warm page cache; no drop_caches, no warmups, no SSD cold claim',
        order_note='full OFF->ON and subset ON->OFF; not independent repeated startup blocks',
        stop='first HTTP/quality/contract failure, identity change, timeout or ENV_ABORT; no extra samples',
        diagnostics='12 separate timing requests after regular matrix, not acceptance timing',
        reused_evidence=reuse, scenarios=scenarios, steps=[], files={},
        model_file_metadata={})
    runner = ROOT / 'tools/evalscope/run_acceptance.py'
    custom = ROOT / 'tools/mtp_suffix/run_stage.py'

    def http(name, mode, head, suffix=False, old=False, reference=None):
        command = [sys.executable, '-B', str(runner), '--mode', mode,
                   '--output', str(output / name), '--binary',
                   str(parent if old else candidate), '--model-dir', str(model),
                   '--fixtures', str(fixtures / mode), '--startup-timeout', '900',
                   '--port', str(args.port), '--max-len', '208896',
                   '--allow-unqualified-binary']
        if head != 'plain':
            command += ['--mtp', '--mtp-k', '3']
        if head == 'subset':
            command += ['--mtp-subset-head', str(artifact)]
        if suffix:
            command += ['--mtp-suffix']
        if reference:
            command += ['--reference', str(reference)]
        plan['steps'].append(dict(name=name, command=command, timeout=2400,
                                  http_requests=11 if mode == 'quality' else 15,
                                  http_loads=1, kind=mode))

    def special(name, head, suffix, kind):
        count = 9 if kind == 'scenarios' else 3
        plan['steps'].append(dict(
            name=name, timeout=900, http_requests=count, http_loads=1,
            kind=kind, head=head, suffix=suffix,
            command=[sys.executable, '-B', str(custom), '--plan',
                     str(stage / 'frozen-plan.json'), '--custom-step', name]))

    http('quality-full-on', 'quality', 'full', True)
    for contract in contracts:
        plan['steps'].append(dict(contract, kind='contract', http_requests=0,
                                  http_loads=0))
    http('quality-subset-on', 'quality', 'subset', True)
    http('quality-plain', 'quality', 'plain')
    for head, orders in [('full', [False, True]), ('subset', [True, False])]:
        for suffix in orders:
            tag = head + ('-on' if suffix else '-off')
            http('performance-' + tag, 'performance', head, suffix)
            special('scenarios-' + tag, head, suffix, 'scenarios')
    http('performance-parent-plain', 'performance', 'plain', old=True)
    http('performance-candidate-plain', 'performance', 'plain',
         reference=output / 'performance-parent-plain/results.json')
    for head in ['full', 'subset']:
        special('lifecycle-' + head, head, True, 'lifecycle')
    for head, orders in [('full', [False, True]), ('subset', [True, False])]:
        for suffix in orders:
            special('timing-' + head + ('-on' if suffix else '-off'),
                    head, suffix, 'timing')
    if (sum(s['http_requests'] for s in plan['steps']) != 177 or
            sum(s['http_loads'] for s in plan['steps']) != 19):
        raise RuntimeError('frozen HTTP budget accounting error')
    if len({s['name'] for s in plan['steps']}) != len(plan['steps']):
        raise RuntimeError('duplicate step name')
    paths = [candidate, candidate.parent / 'CMakeCache.txt',
             parent, parent_dir / 'CMakeCache.txt', parent_dir / 'q4t.release.json',
             installed_parent, release_path, cache,
             args.contracts.resolve(), args.reuse_record.resolve(),
             ROOT / 'CMakeLists.txt', ROOT / 'AGENTS.md']
    paths += [path.resolve() for path in args.extra_file]
    for contract in contracts:
        paths += [Path(p).resolve() for p in contract.get('files', [])]
        executable = Path(contract['command'][0])
        if executable.is_absolute() and executable.is_file():
            paths.append(executable)
    for folder in ['src', 'include', 'tests', 'cmake', 'tools/evalscope', 'tools/mtp_suffix']:
        paths += [p for p in (ROOT / folder).rglob('*') if p.is_file()
                  and not any(part in ('.venv', '__pycache__', '.git')
                              for part in p.parts)]
    for folder in [fixtures, stage / 'scenarios', artifact]:
        paths += [p for p in folder.rglob('*') if p.is_file()]
    manifest = json.loads((artifact / 'manifest.json').read_text())
    paths += [model / n for n in ['config.json', 'generation_config.json',
              'tokenizer.json', 'tokenizer_config.json', manifest['source_shard']]]
    for path in sorted(model.glob('*.safetensors')):
        stat = path.stat()
        plan['model_file_metadata'][str(path)] = dict(
            size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            device=stat.st_dev, inode=stat.st_ino)
    snapshot = stage / 'source-snapshot'
    snapshot.mkdir()
    for path in sorted(set(paths)):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target = snapshot / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (snapshot / 'worktree.patch').write_bytes(subprocess.check_output(
        ['git', 'diff', 'HEAD'], cwd=ROOT))
    plan['source_snapshot'] = str(snapshot)
    save(stage / 'frozen-plan.json', plan)
    print(json.dumps({k: plan[k] for k in ['output', 'lease', 'http_requests',
          'http_loads', 'stage_timeout_seconds', 'candidate_sha256']}, indent=2))


if __name__ == '__main__':
    main()
