"""Bounded five-tier adoption assessment of the frozen repaired candidate."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


qsa = module('adoption_qsa', 'tools/qsa_softmax/stage.py')
refresh = module('adoption_tool', 'tools/reference_refresh/stage.py')
base, legacy, telemetry = qsa.base, qsa.legacy, qsa.telemetry
sha, save, require = qsa.sha, qsa.save, qsa.require
ORIGINAL = Path('/home/rm01/models/dev/qwen4-thor')
BASE = ROOT / '.q4t-work/qsa-softmax-adoption-20261106'
PLAN = BASE / 'http/plan.json'
DEFAULT = 'c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636'
CANDIDATE = 'aa528a6839ed629fcea2aed036f4c8f0bdc14617ac00fdc31156e6e58c3e18d0'
SIDECAR = '0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3'
ORDER = [('plain', 'parent'), ('plain', 'candidate'), ('full', 'candidate'),
         ('full', 'parent'), ('subset', 'parent'), ('subset', 'candidate')]
LENGTHS = [1024, 4096, 8192, 45056, 204800]


def read(path):
    return json.loads(Path(path).read_text())


def server_command(plan, step):
    command = [plan[step['arm']], 'serve', '--model-dir', plan['model'],
        '--host', '127.0.0.1', '--port', str(plan['port']), '--max-seq', '1',
        '--max-prefill', '8192', '--max-len', '208896', '--max-tokens', '256',
        '--startup-plan', 'off']
    command += (['--no-mtp'] if step['head'] == 'plain' else ['--mtp', '--mtp-k', '3'])
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    return command


def validate(plan, executing=True):
    require(plan['stage'] == 'QSA_SOFTMAX_ADOPTION_HTTP', 'stage')
    require([(s['head'], s['arm']) for s in plan['steps']] == ORDER, 'arm order')
    require((plan['http_loads'], plan['http_requests'],
             plan['stage_timeout_seconds'], plan['approved_total_wall_seconds'])
            == (6, 160, 7140, 7200), 'frozen budget')
    if executing:
        require(time.time() < plan['task_deadline_unix'], 'absolute deadline')
    for step in plan['steps']:
        tool = step['head'] == 'full'
        require((step['http_loads'], step['http_requests'], step['timeout'],
                 step['tool_loop']) == (1, 26 + 2 * tool, 1200, tool), 'step budget')
        require(step['server_command'] == server_command(plan, step), 'server config')
        expected = [(f'context-{n}', 3, 256, 1) for n in LENGTHS]
        expected += [('quality', 11, 32, 1)]
        require([(c['id'], c['count'], c['tokens'], c['parallel'])
                 for c in step['cases']] == expected, 'case scope')


def prepare():
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit tools and plan before freezing')
    old = read(ROOT / '.q4t-work/qsa-softmax-repair-20261106/plan.json')
    prior = read(ROOT / '.q4t-work/reference-refresh-acceptance-20261106/plan.json')
    release_path = ORIGINAL / 'build/q4t.release.json'
    release = read(release_path)
    require(sha(ORIGINAL / 'build/q4t') == DEFAULT and
            sha(release_path) == SIDECAR and sha(Path(old['candidate'])) == CANDIDATE,
            'frozen binaries/default changed')
    manifest_path = Path(release['tested_source_manifest'])
    require(sha(manifest_path) == release['tested_source_manifest_sha256'],
            'default manifest identity')
    default_sources = read(manifest_path)
    candidate_manifest = ROOT / '.q4t-work/qsa-softmax-repair-20261106/frozen/candidate-runtime-source.json'
    candidate_sources = read(candidate_manifest)
    require(default_sources.keys() == candidate_sources.keys() and
            all(sha(ORIGINAL / n) == h for n, h in default_sources.items()) and
            all(sha(ROOT / n) == h for n, h in candidate_sources.items()),
            'runtime source identity')
    changed = sorted(n for n in default_sources if
                     default_sources[n] != candidate_sources[n])
    require(set(changed) == {'src/model/full_attention.cu', 'src/model/model.cu',
        'src/io/model_config.cpp', 'include/q4t/io/model_config.h',
        'include/q4t/model/layer_subtiming.h'}, 'runtime delta')
    frozen = PLAN.parent / 'frozen'
    frozen.mkdir(parents=True, exist_ok=False)
    for source, name in [(ORIGINAL / 'build/q4t', 'parent'),
        (Path(old['candidate']), 'candidate'), (release_path, 'parent.release.json'),
        (manifest_path, 'parent-runtime-source.json'),
        (candidate_manifest, 'candidate-runtime-source.json')]:
        shutil.copy2(source, frozen / name)
        require(sha(source) == sha(frozen / name), 'copy identity')
    fixture_path = ORIGINAL / '.q4t-work/mtp-prefill-release-20261029/phase-a/plan.json'
    fixtures = read(fixture_path)
    cases = copy.deepcopy(fixtures['protection']) + [copy.deepcopy(fixtures['quality_case'])]
    for case in cases:
        source = Path(case['path'])
        require(sha(source) == fixtures['files'][str(source)], 'fixture identity')
        target = frozen / (case['id'] + '.jsonl')
        shutil.copy2(source, target)
        case['path'] = str(target)
    artifact = Path(prior['artifact'])
    require(sha(artifact / 'head.bf16') ==
        '21522eabb4ab4f816d9f76eefe317a17e7ba668ebe838809eb4be87d84ef75c1',
        'accepted subset head')
    plan = dict(stage='QSA_SOFTMAX_ADOPTION_HTTP', output=str(PLAN.parent / 'run'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), runtime_commit='ada8cbac9da19ceab3e87706b0eec35f594b5159',
        runtime_delta=changed, execution_tools_root=str(ORIGINAL),
        parent=str(frozen / 'parent'), candidate=str(frozen / 'candidate'),
        artifact=str(artifact), model=fixtures['model'],
        model_file_metadata=fixtures['model_file_metadata'], quality=fixtures['quality'],
        port=8762, http_loads=6, http_requests=160, stage_timeout_seconds=7140,
        approved_total_wall_seconds=7200, direct_contract_timeout_seconds=0,
        resource_sample_max_age_seconds=10, telemetry_sources=telemetry.discover_sources(),
        steps=[], files={})
    for index, (head, arm) in enumerate(ORDER):
        name = f'{index:02d}-{head}-{arm}'
        step = dict(name=name, head=head, arm=arm, kind='matrix',
            http_loads=1, http_requests=26 + 2 * (head == 'full'), timeout=1200,
            tool_loop=head == 'full', cases=copy.deepcopy(cases),
            command=[sys.executable, '-B', str(Path(__file__).resolve()),
                     '--plan', str(PLAN), '--step', name])
        step['server_command'] = server_command(plan, step)
        plan['steps'].append(step)
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    files = {ROOT / n for n in tracked if n.startswith(
        ('src/', 'include/', 'tools/', 'cmake/')) or n in ('CMakeLists.txt',
        'docs/MODEL.md', 'docs/QSA_SOFTMAX_ADOPTION_PLAN_2026-11-06.md')}
    files.update(ORIGINAL / n for n in default_sources)
    files.update(p for p in (ORIGINAL / 'tools').rglob('*.py')
        if not any(x.startswith('.') for x in p.relative_to(ORIGINAL).parts))
    files.update(p for p in frozen.rglob('*') if p.is_file())
    files.update(p for p in artifact.rglob('*') if p.is_file())
    files.update([fixture_path, manifest_path, candidate_manifest, release_path,
        ORIGINAL / 'build/q4t', Path(old['candidate']), Path(sys.executable).resolve(),
        ORIGINAL / 'tools/evalscope/.venv/bin/evalscope'])
    for directory in (ORIGINAL / 'tools/evalscope/.venv/lib').glob('python*/site-packages/evalscope'):
        files.update(directory.rglob('*.py'))
    plan['files'] = {str(p): sha(p) for p in sorted(files) if p.is_file()}
    # Deadline starts only once read-only hashing and copies have finished.
    plan['task_deadline_unix'] = time.time() + 7140
    validate(plan)
    save(PLAN, plan)
    print(json.dumps(dict(plan=str(PLAN), loads=6, requests=160, seconds=7200,
                         frozen_inputs=len(plan['files']))))


def mode_log(raw, head):
    require('[q4t][startup-plan] mode=off active=0' in raw, 'allocation mode')
    if head == 'plain':
        qsa.plain_log(raw)
    else:
        require('MTP loaded (k=3 max_seq=1)' in raw and
                'MTP disabled; plain decode' not in raw, 'MTP mode')
        for marker in ('[mtp-timing]', '[layer-timing]', '[sub-timing]',
                       '[qsa-subtiming]', '[moe-subtiming]', '[hc-mix-subtiming]'):
            require(marker not in raw, 'unexpected observer')


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    save(out / 'step.json', step)
    base.multi.ROOT = ORIGINAL
    env = {k: v for k, v in legacy.clean_env().items() if not k.startswith('CUPTI_')}
    command = step['server_command']
    save(out / 'server-command.json', dict(argv=command, environment={
        k: v for k, v in env.items() if k.startswith('Q4T_')}))
    deadline = min(time.monotonic() + step['timeout'] - 40,
                   time.monotonic() + plan['task_deadline_unix'] - time.time() - 40)
    server, failure, passed = None, None, False

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('bounded HTTP deadline')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited early')

    with (out / 'server.log').open('x') as log:
        try:
            base.http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT)
            (out / 'server.pid').write_text(str(server.pid) + '\n')
            base.http.event(out, 'server_started', pid=server.pid)
            startup = min(deadline, time.monotonic() + 300)
            while 'serving on port' not in (out / 'server.log').read_text():
                check()
                require(time.monotonic() < startup, 'startup timeout')
                time.sleep(.5)
            raw = (out / 'server.log').read_text()
            capacity = base.http.capacity_evidence(raw, 208896)
            require(capacity['matches_requested'], 'capacity reduced')
            save(out / 'capacity.json', capacity)
            mode_log(raw, step['head'])
            base.http.event(out, 'server_ready', pid=server.pid, capacity=capacity)
            results = []
            previous = [s for s in plan['steps'] if s['head'] == step['head'] and
                        s['name'] < step['name']]
            for case in step['cases']:
                result = base.multi.case(plan, step, case, out, env, check)
                results.append(result)
                save(out / 'results.json', results)
                if previous:
                    old = read(Path(plan['output']) / previous[0]['name'] /
                               case['id'] / 'result.json')
                    same = base.identity(old) == base.identity(result)
                    save(out / (case['id'] + '-comparison.json'), dict(outputs_identical=same))
                    require(same, 'same-mode output mismatch: ' + case['id'])
            if step['tool_loop']:
                refresh.tool_loop(plan, out, check)
            check()
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                base.http.event(out, 'server_before_shutdown', pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                base.http.event(out, 'server_after_shutdown', pid=server.pid,
                                server_returncode=server.returncode)
            save(out / 'exit.json', dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server is not None and
                server.returncode == 0, failure=failure))


def audit_server(out, expected):
    result = base.audit_server(out, expected)
    step = read(out / 'step.json')
    raw = (out / 'server.log').read_text()
    mode_log(raw, step['head'])
    active, ids, histograms = None, [], {}
    for line in raw.splitlines():
        if line.startswith('[q4t][request] '):
            fields = dict(re.findall(r'(\w+)=(\S+)', line))
            if fields.get('event') == 'admitted':
                require(active is None, 'overlapping single-stream request')
                active = fields['id']
            elif fields.get('event') == 'end':
                require(active == fields['id'] and fields['outcome'] == 'success',
                        'unpaired/failed request')
                ids.append(active)
                active = None
        if line.startswith('[mtp-accept]'):
            require(active is not None and active not in histograms, 'MTP histogram owner')
            fields = dict(re.findall(r'(\w+)=(\S+)', line))
            require(fields['request'] == active, 'MTP request identity')
            histograms[active] = fields['draft_counts']
    require(active is None and len(ids) == len(set(ids)) == expected, 'request ledger')
    if step['head'] == 'plain':
        require(not histograms, 'plain histogram')
    else:
        require(len(histograms) >= expected - 2 * step['tool_loop'],
                'missing MTP request histograms')
    return dict(result, request_ids=ids, histograms=histograms)


def review(plan, lease):
    validate(plan, executing=False)
    base.monitored_stage.require_complete_monitor(plan)
    legacy.verify_files(plan)
    require(read(lease / 'exit.json')['status'] == 'COMPLETED', 'outer lease')
    output = Path(plan['output'])
    for n, h in read(output / 'files.json').items():
        require(sha(output / n) == h, 'sealed raw changed')
    arms = {}
    for step in plan['steps']:
        folder = output / step['name']
        audit = audit_server(folder, step['http_requests'])
        cases = [qsa.raw_case(plan, c, folder / c['id']) for c in step['cases']]
        ids = [r['response_id'] for c in cases for r in c['rows']]
        if step['tool_loop']:
            tool = read(folder / 'project-tool/completed.json')
            require((tool['requests'], tool['tool_calls']) == (2, 1), 'real tool loop')
            ids += [read(folder / f'project-tool/{i}-response.json')['id'] for i in range(2)]
        require(len(ids) == len(set(ids)) == step['http_requests'] and
                set(ids) == set(audit['request_ids']), 'raw/server request binding')
        arms[step['head'] + '-' + step['arm']] = dict(cases=cases, audit=audit,
            metrics={c['id']: qsa.metrics(c['rows']) for c in cases[:-1]})
    comparisons = {}
    for head in ('plain', 'full', 'subset'):
        p, c = [arms[head + '-' + side] for side in ('parent', 'candidate')]
        require(all(base.identity(a) == base.identity(b) for a, b in
                    zip(p['cases'], c['cases'])), 'paired output contract')
        if head != 'plain':
            for pc, cc in zip(p['cases'], c['cases']):
                for pr, cr in zip(pc['rows'], cc['rows']):
                    require(p['audit']['histograms'][pr['response_id']] ==
                            c['audit']['histograms'][cr['response_id']], 'MTP acceptance changed')
        cells = {}
        for n in LENGTHS:
            name = f'context-{n}'
            pm, cm = p['metrics'][name], c['metrics'][name]
            cells[name] = dict(parent=pm, candidate=cm,
                ttft_gain=1 - cm['mean_ttft'] / pm['mean_ttft'],
                decode_gain=cm['aggregate_decode'] / pm['aggregate_decode'] - 1,
                e2e_change=cm['aggregate_e2e'] / pm['aggregate_e2e'] - 1)
        comparisons[head] = cells
    proceed = all(c['context-45056']['ttft_gain'] > 0 and
                  c['context-45056']['e2e_change'] < 0 for c in comparisons.values())
    result = dict(comparisons=comparisons, arms=arms, outputs_identical=True,
        quality_checks=66, tool_loops=2, performance_accepted=False,
        statistics='DESCRIPTIVE_SINGLE_START_PER_MODE_IDENTITY_NOT_NONINFERIORITY',
        decision='POSITIVE_TARGET_REQUIRES_CONTRACTS_AND_PROTECTION_REVIEW' if proceed
                 else 'NO_GO_ADOPTION_TARGET', original_3_percent_gate='UNCHANGED_NO_GO')
    save(PLAN.parent / 'review.json', result)
    print(json.dumps(dict(decision=result['decision'], comparisons=comparisons), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--plan', type=Path, default=PLAN)
    parser.add_argument('--step')
    parser.add_argument('--review', action='store_true')
    parser.add_argument('--lease', type=Path)
    args = parser.parse_args()
    if args.prepare:
        prepare()
        return
    plan = read(args.plan)
    if args.review:
        require(args.lease is not None, 'completed lease required')
        review(plan, args.lease)
        return
    validate(plan)
    if args.step:
        custom_step(plan, args.step)
    else:
        base.monitored_stage.run_stage(legacy, telemetry, plan, args.plan,
                                      audit_server=audit_server)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('adoption HTTP interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
