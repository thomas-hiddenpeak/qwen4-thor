"""Prepare and run bounded numerical/full-model state adoption contracts.

No model runs during --build or --prepare. The run action requires the shared
GPU guard; four model executions are counted separately from HTTP services.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('qsa_adoption',
    Path(__file__).with_name('adoption.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
ROOT, ORIGINAL, BASE = a.ROOT, a.ORIGINAL, a.BASE / 'contracts'
read, save, sha, require = a.read, a.save, a.sha, a.require
ENGINE = ROOT / '.q4t-work/qsa-softmax-repair-20261106/engine'


def command(argv, directory, name):
    with (directory / (name + '.log')).open('x') as stream:
        result = subprocess.run(argv, cwd=directory, stdout=stream,
                                stderr=subprocess.STDOUT)
    record = dict(argv=argv, cwd=str(directory), returncode=result.returncode)
    save(directory / (name + '.json'), record)
    result.check_returncode()
    require(not re.search(r'\bwarning:', (directory / (name + '.log')).read_text(), re.I),
            'build warning: ' + name)


def build():
    h = read(a.PLAN.parent / 'review.json')
    require(h['decision'] == 'POSITIVE_TARGET_REQUIRES_CONTRACTS_AND_PROTECTION_REVIEW',
            'H has not supported continued acceptance')
    require(not BASE.exists(), 'fresh contract output')
    build = BASE / 'build'
    build.mkdir(parents=True)
    hp = read(a.PLAN)
    for name in ('parent', 'candidate'):
        require(sha(Path(hp[name])) == (a.DEFAULT if name == 'parent' else a.CANDIDATE),
                'accepted H binary identity')
    extract = a.module('adoption_extract', 'tools/verify/compare_sparse_attention_layout.py')
    source = ('#include <cuda_runtime.h>\n#include <cuda_bf16.h>\n'
              '#include <array>\n#include <cstdint>\n#include <cstdio>\n'
              '#include <cstring>\n#include <vector>\n')
    for name, root in [('before', ORIGINAL), ('after', ROOT)]:
        raw = (root / 'src/model/full_attention.cu').read_text()
        header = (root / 'include/q4t/model/full_attention.h').read_text()
        (build / (name + '.cu.txt')).write_text(raw)
        source += extract.kernel_source(raw, header, name)
    source += Path(__file__).with_name('adoption_numeric.cu.in').read_text()
    (build / 'numerical.cu').write_text(source)
    command(['/usr/local/cuda-13.3/bin/nvcc', '-std=c++23',
        '--expt-relaxed-constexpr', '-O3', '-arch=sm_110a', '-ccbin',
        '/usr/bin/g++-14', '-Xcompiler=-Wall,-Wextra', str(build / 'numerical.cu'),
        '-o', str(build / 'numerical')], build, 'build-numerical')
    reference = (ROOT / 'tools/session_cache/state_contract.cpp').read_text()
    reference = reference[:reference.index('#ifndef Q4T_SESSION_REFERENCE_ONLY')]
    require(reference.count('cfg.num_layers = 4;') == 1, 'reference layer binding')
    reference = reference.replace('cfg.num_layers = 4;', 'cfg.num_layers = 48;')
    reference += Path(__file__).with_name('adoption_state.cpp.in').read_text()
    (build / 'state.cpp').write_text(reference)
    parent_engine = build / 'parent-engine'
    command(['cmake', '-S', str(ORIGINAL), '-B', str(parent_engine),
        '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
        '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14', '-DQ4T_CUDA_ARCHITECTURES=110a',
        '-DQ4T_ENABLE_XGRAMMAR=ON', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
        '-DFETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR=' + str(ORIGINAL /
            '.q4t-work/structured-control-20261102/xgrammar')], build, 'configure-parent')
    command(['cmake', '--build', str(parent_engine), '--target', 'q4t_server',
             '--parallel', '12'], build, 'build-parent')
    libraries = ['server', 'model', 'text', 'ple', 'quant', 'trace', 'io',
                 'runtime', 'startup_plan']
    identities = {}
    for name, root, engine in [('parent', ORIGINAL, parent_engine),
                                ('candidate', ROOT, ENGINE)]:
        archives = [engine / ('libq4t_' + n + '.a') for n in libraries]
        archives += [engine / '_deps/q4t_xgrammar-build/libxgrammar.a']
        require(all(p.is_file() for p in archives), 'state link dependencies')
        argv = ['/usr/bin/g++-14', '-std=c++23', '-O3', '-Wall', '-Wextra', '-Werror',
            '-I' + str(root / 'include'), '-isystem', '/usr/local/cuda-13.3/include',
            str(build / 'state.cpp'), '-o', str(build / (name + '-state')),
            '-Wl,--start-group', *map(str, archives), '-Wl,--end-group',
            '-L/usr/local/cuda-13.3/targets/sbsa-linux/lib',
            '-Wl,-rpath,/usr/local/cuda-13.3/targets/sbsa-linux/lib',
            '-lcudart', '-lcublasLt', '-luring', '-licui18n', '-licuuc',
            '-licudata', '-ldl', '-lrt', '-pthread']
        command(argv, build, 'link-' + name)
        identities[name] = dict(binary=str(build / (name + '-state')),
            binary_sha256=sha(build / (name + '-state')),
            archives={str(p): sha(p) for p in archives})
    save(build / 'identities.json', identities)
    print(json.dumps(dict(built=True, model_loads=0, contracts=str(BASE))))


def prepare():
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit contract tools before freeze')
    h = read(a.PLAN)
    require(read(a.PLAN.parent / 'review.json')['decision'] ==
            'POSITIVE_TARGET_REQUIRES_CONTRACTS_AND_PROTECTION_REVIEW', 'H decision')
    build = BASE / 'build'
    ids = read(build / 'identities.json')
    for name, item in ids.items():
        require(sha(Path(item['binary'])) == item['binary_sha256'] and
                all(sha(Path(p)) == v for p, v in item['archives'].items()),
                'built contract identity')
    require(not (BASE / 'plan.json').exists(), 'fresh plan')
    steps = [dict(name='00-numerical', kind='contract', head='none',
        http_loads=0, http_requests=0, model_loads=0, timeout=120,
        executable=str(build / 'numerical'))]
    for head in ('full', 'subset'):
        for arm in ('parent', 'candidate'):
            steps.append(dict(name=f'{len(steps):02d}-{head}-{arm}',
                kind='contract', head=head, arm=arm, http_loads=0, http_requests=0,
                model_loads=1, timeout=400, executable=ids[arm]['binary']))
    for step in steps:
        step['command'] = [sys.executable, '-B', str(Path(__file__).resolve()),
                           '--plan', str(BASE / 'plan.json'), '--step', step['name']]
    plan = dict(stage='QSA_SOFTMAX_ADOPTION_CONTRACTS', output=str(BASE / 'run'),
        model=h['model'], model_file_metadata=h['model_file_metadata'],
        artifact=h['artifact'], execution_tools_root=str(ORIGINAL),
        http_loads=0, http_requests=0, model_loads=4, steps=steps,
        stage_timeout_seconds=1740, direct_contract_timeout_seconds=1740,
        approved_total_wall_seconds=1800, resource_sample_max_age_seconds=10,
        telemetry_sources=a.telemetry.discover_sources(), files={})
    files = {Path(p) for p in h['files'] if Path(p).is_file()}
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    files.update(ROOT / p for p in tracked if p.startswith(('src/', 'include/', 'tools/')))
    files.update(p for p in build.rglob('*') if p.is_file() and
                 (p.name.endswith(('.a', '.h', '.json', '.log', '.txt', '.cpp', '.cu'))
                  or p.name in ('numerical', 'parent-state', 'candidate-state')))
    files.update([a.PLAN, a.PLAN.parent / 'review.json',
                  ROOT / 'docs/QSA_SOFTMAX_ADOPTION_PLAN_2026-11-06.md',
                  ROOT / 'docs/QSA_SOFTMAX_CONTRACT_PLAN_2026-11-06.md'])
    for item in ids.values():
        files.update(Path(p) for p in item['archives'])
    plan['files'] = {str(p): sha(p) for p in sorted(files)}
    plan['task_deadline_unix'] = time.time() + 1740
    save(BASE / 'plan.json', plan)
    print(json.dumps(dict(plan=str(BASE / 'plan.json'), models=4, http=0, seconds=1800)))


def step_run(plan, name):
    a.legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    save(out / 'step.json', step)
    argv = [step['executable']]
    if step['model_loads']:
        argv += [plan['model'], str(out / 'states')]
        if step['head'] == 'subset':
            argv += [plan['artifact']]
    save(out / 'command.json', argv)
    with (out / 'contract.log').open('x') as log:
        result = subprocess.run(argv, env=a.legacy.clean_env(), stdout=log,
            stderr=subprocess.STDOUT, timeout=min(step['timeout'] - 10,
                plan['task_deadline_unix'] - time.time() - 10))
    raw = (out / 'contract.log').read_text()
    passed = result.returncode == 0 and 'FAIL ' not in raw and (
        'PASS full_layers=48 heads=1 slots=4 verify_rows=16 rounds=3 ' in raw
        if step['model_loads'] else ('PASS finite_boundaries=1 cases=60 ' in raw and
            'PASS denominator_boundaries=65536 host_separate_rounding=1' in raw))
    save(out / 'result.json', dict(passed=passed, returncode=result.returncode,
        model_loads=step['model_loads'], http_requests=0))
    require(passed, 'numerical/state contract failed')
    if step.get('arm') == 'candidate':
        parent = next(s for s in plan['steps'] if s.get('arm') == 'parent' and
                      s['head'] == step['head'])
        directory = Path(plan['output']) / parent['name'] / 'states'
        names = sorted(p.name for p in (out / 'states').glob('*.bin'))
        require(names == sorted(p.name for p in directory.glob('*.bin')) and
                len(names) == 21, 'full-state capture coverage')
        comparisons = {}
        for file in names:
            pa, ca = directory / file, out / 'states' / file
            same = pa.stat().st_size == ca.stat().st_size
            with pa.open('rb') as p, ca.open('rb') as c:
                while same:
                    pb, cb = p.read(1 << 20), c.read(1 << 20)
                    if pb != cb:
                        same = False
                    if not pb:
                        break
            comparisons[file] = dict(identical=same, bytes=pa.stat().st_size,
                parent_sha256=sha(pa), candidate_sha256=sha(ca))
        save(out / 'comparison.json', comparisons)
        require(all(c['identical'] for c in comparisons.values()),
                'full-model parent/candidate state differs')


def validate(plan, executing=True):
    require((plan['stage'], plan['http_loads'], plan['http_requests'],
             plan['model_loads'], plan['stage_timeout_seconds'],
             plan['direct_contract_timeout_seconds'],
             plan['approved_total_wall_seconds']) ==
            ('QSA_SOFTMAX_ADOPTION_CONTRACTS', 0, 0, 4, 1740, 1740, 1800),
            'frozen direct-contract budget')
    expected = [('none', None), ('full', 'parent'), ('full', 'candidate'),
                ('subset', 'parent'), ('subset', 'candidate')]
    require([(s['head'], s.get('arm')) for s in plan['steps']] == expected,
            'direct-contract coverage/order')
    for index, s in enumerate(plan['steps']):
        require((s['kind'], s['http_loads'], s['http_requests'], s['model_loads'],
                 s['timeout']) == ('contract', 0, 0, int(index != 0),
                                   400 if index else 120), 'step budget')
        name = s['arm'] + '-state' if index else 'numerical'
        require(s['executable'] == str(BASE / 'build' / name), 'fixture binding')
    if executing:
        require(time.time() < plan['task_deadline_unix'], 'contract deadline')


def review(plan, lease):
    validate(plan, executing=False)
    a.base.monitored_stage.require_complete_monitor(plan)
    a.legacy.verify_files(plan)
    require(read(lease / 'exit.json')['status'] == 'COMPLETED', 'completed lease')
    output = Path(plan['output'])
    for name, digest in read(output / 'files.json').items():
        require(sha(output / name) == digest, 'sealed contract changed')
    for step in plan['steps']:
        require(read(output / step['name'] / 'result.json')['passed'], 'contract result')
    pairs = {s['head']: read(output / s['name'] / 'comparison.json')
             for s in plan['steps'] if s.get('arm') == 'candidate'}
    require(set(pairs) == {'full', 'subset'} and
            all(x['identical'] for pair in pairs.values() for x in pair.values()),
            'full state comparisons')
    save(BASE / 'review.json', dict(passed=True, pairs=pairs, models=4, http_requests=0,
        numerical=read(output / '00-numerical/result.json'),
        full_layers=48, rounds=3, slots=4, statistical_performance_claim=False))
    print('PASS full 48-layer state and finite boundary contracts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--plan', type=Path, default=BASE / 'plan.json')
    parser.add_argument('--step')
    parser.add_argument('--review', action='store_true')
    parser.add_argument('--lease', type=Path)
    args = parser.parse_args()
    if args.build:
        build()
    elif args.prepare:
        prepare()
    else:
        plan = read(args.plan)
        if args.review:
            require(args.lease is not None, 'completed lease required')
            review(plan, args.lease)
        else:
            validate(plan)
            if args.step:
                step_run(plan, args.step)
            else:
                a.base.monitored_stage.run_stage(a.legacy, a.telemetry, plan, args.plan)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('adoption contract interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
