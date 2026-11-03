"""Freeze one installed-binary pressure/mixed-workload acceptance stage."""
import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_memory import stage as previous

h, a = previous.h, previous.a
BASE = ROOT / '.q4t-work/serve-pressure-20261103'
PACKAGE, OUTPUT = BASE / 'package', BASE / 'execution'
SHA = '84d16983e03c21c62dd249f4c7e9785bb5e62a010e21947778b26b001d84d94d'
SIDECAR = 'c520333192c04ddb499dd0188496789ec8ddc20fc5e0f00491ca01f52517aa75'
LIMITS = dict(generations=330, negatives=6, starts=2, seconds=1800)
PLANNED = dict(generations=326, negatives=4, starts=1)
CYCLES, FLOOR = 64, 30720


def installed():
    release = h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json', SHA)
    h.require(h.sha(ROOT / 'build/q4t.release.json') == SIDECAR, 'installed sidecar changed')
    a.verify_hashes(ROOT, h.read(release['tested_source_manifest']))
    h.require(not list((ROOT / 'build').glob('*recovery*.json')), 'pending recovery')
    return release


def prepare():
    h.require(not PACKAGE.exists(), 'preserve original package')
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed implementation before freezing')
    installed()
    cpu = h.read(BASE / 'cpu/result.json')
    h.require(cpu['passed'] and h.read(BASE/'cpu/oom-gate.json')['passed'], 'CPU gate')
    inherited = h.read(previous.PACKAGE / 'plan.json')
    a.verify_hashes(inherited['model_dir'], inherited['model_inputs'])
    sdk = previous.previous.prior.sdk_identity()
    PACKAGE.mkdir()
    shutil.copytree(BASE / 'cpu', PACKAGE / 'cpu')
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / 'build' / name), PACKAGE / name)
    paths = subprocess.check_output(['git', 'ls-files', 'tools'], cwd=ROOT, text=True).splitlines()
    paths += ['docs/SERVE_PRESSURE_2026-11-03.md', 'docs/MODEL.md']
    paths += [str(p.relative_to(ROOT)) for p in (ROOT / 'docs').glob('serve-*.json')]
    sources = {p: h.sha(ROOT / p) for p in sorted(set(paths)) if (ROOT / p).is_file()}
    h.save(PACKAGE / 'sources.json', sources)
    h.save(PACKAGE / 'sdk.json', sdk)
    h.save(PACKAGE / 'runtime-reuse.json', dict(
        runtime_sources=h.identity(previous.PACKAGE/'candidate/runtime-source.json'),
        build_result=h.identity(previous.BASE/'cpu/build-result.json'),
        host_required=h.identity(previous.BASE/'cpu/host-gate/result.json'),
        actual_business=h.identity(previous.OUTPUT/'business/result.json'),
        same_198_runtime_sources=True, new_build_needed=False,
        inherited_zero_warning_build=True, numerical_and_template_changes=False))
    plan = dict(limits=LIMITS, planned=PLANNED, cycles=CYCLES, floor_mib=FLOOR,
        binary=h.identity(ROOT / 'build/q4t'), sidecar=h.identity(ROOT / 'build/q4t.release.json'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        protocol_sha256=inherited['protocol_sha256'], model_dir=inherited['model_dir'],
        model_inputs=inherited['model_inputs'], sdk_directory=sdk['sdk_directory'], port=18106,
        runtime_evidence=h.identity(previous.OUTPUT / 'business/result.json'),
        runtime_changed=False, performance_acceptance=False, deployment_changed=False)
    h.save(PACKAGE / 'plan.json', plan)
    h.seal(PACKAGE)
    print('PREPARED', SHA, flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE / 'plan.json')
    h.require(plan['limits'] == LIMITS and plan['planned'] == PLANNED and
              plan['cycles'] == CYCLES and plan['floor_mib'] == FLOOR, 'frozen contract changed')
    installed()
    a.verify_hashes(ROOT, h.read(PACKAGE / 'sources.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    h.require(previous.previous.prior.sdk_identity() == h.read(PACKAGE / 'sdk.json'), 'SDK changed')
    return plan


def execute():
    h.require_guard(OUTPUT)
    plan = check()
    events = [json.loads(line) for line in (OUTPUT / 'lease/events.jsonl').read_text().splitlines()]
    plan['started'] = datetime.datetime.fromisoformat(next(r['utc'] for r in events
        if r['event'] == 'lease_acquired')).timestamp()
    h.save(OUTPUT / 'begin.json', dict(started=plan['started'], limits=LIMITS))
    from tools.serve_pressure.verify import Run
    Run(plan, OUTPUT / 'business').run()


def finalize():
    check()
    guard = h.read(OUTPUT / 'lease/exit.json')
    result = h.read(OUTPUT / 'business/result.json')
    elapsed = time.time() - h.read(OUTPUT / 'begin.json')['started']
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0 and
              result['status'] == 'PASS_BOUNDED_PRESSURE_AND_MIXED' and
              elapsed <= LIMITS['seconds'], 'bounded acceptance/guard gate')
    h.save(OUTPUT / 'finalized.json', dict(passed=True, elapsed_seconds=elapsed,
        installed_binary_unchanged=SHA, sidecar_unchanged=SIDECAR,
        performance_acceptance=False, physical_ram_hard_limit_proved=False,
        long_duration_soak_proved=False))
    h.seal(OUTPUT)


if __name__ == '__main__':
    if sys.argv[1:] == ['prepare']:
        prepare()
    elif sys.argv[1:] == ['execute']:
        execute()
    elif sys.argv[1:] == ['finalize']:
        finalize()
    else:
        raise SystemExit('prepare | execute | finalize')
