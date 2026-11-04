"""Freeze SRV-13 identities and run the complete bounded lifecycle under one lease."""
import argparse
import datetime
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_lifecycle import catalog, deployment
from tools.serve_protocol.stage import sdk_identity

h, a = catalog.h, catalog.shared
BASE = ROOT/'.q4t-work/serve-lifecycle-20261103'
PACKAGE, OUTPUT, CPU = BASE/'package', BASE/'execution', BASE/'cpu'
BUNDLE = PACKAGE/'releases'
SHA = '3cd3994d561ba9bcdebcb609204be1056ba16b1fc7bab103447e62bc47ee897a'
SIDECAR = 'bb620240dfbbe0437066f6827bf54d00f9e107327f244997fb437fe30727b1b2'
PREVIOUS_SHA = '84d16983e03c21c62dd249f4c7e9785bb5e62a010e21947778b26b001d84d94d'
PREVIOUS_SIDECAR = 'c520333192c04ddb499dd0188496789ec8ddc20fc5e0f00491ca01f52517aa75'
LIMITS = dict(generations=1224, negatives=8, preflights=240, starts=7,
              observations=5000, seconds=6000)
PLANNED = dict(generations=1216, negatives=4, starts=5)
CYCLES, WARMUP, INTERVAL, SOAK_SECONDS, SOAK_MAX = 240, 16, 15, 3600, 4500
ENVELOPE = dict(rss=64*1024**2, anon=128*1024**2, fds=2, threads=2)
PRIOR = ROOT/'.q4t-work/serve-observation-capacity-20261103'


def installed():
    value = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', SHA)
    h.require(h.sha(ROOT/'build/q4t.release.json') == SIDECAR, 'installed sidecar changed')
    sources = h.read(value['tested_source_manifest'])
    h.require(h.sha(value['tested_source_manifest']) == value['tested_source_manifest_sha256'] and
              len(sources) == 199, 'runtime source manifest drift')
    a.verify_hashes(ROOT, sources)
    h.require(not list((ROOT/'build').glob('*recovery*.json')), 'pending recovery')
    return value


def cpu():
    CPU.mkdir(parents=True, exist_ok=True)
    folder = CPU/'grouped';folder.mkdir()
    argv = ['/usr/bin/python3', '-B', '-m', 'unittest', '-v',
            'tools.serve_lifecycle.test_deployment', 'tools.serve_lifecycle.test_driver']
    h.save(folder/'command.json', dict(argv=argv, started=time.time()))
    with (folder/'stdout').open('xb') as stdout, (folder/'stderr').open('xb') as stderr:
        result = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=180)
    h.save(folder/'result.json', dict(passed=result.returncode == 0, returncode=result.returncode,
        ended=time.time(), http_or_model_started=False, default_pair_changed=False))
    print((folder/'stderr').read_text(), flush=True)
    h.require(result.returncode == 0, 'grouped CPU failed; preserve failure and fix affected items')


def prepare():
    h.require(not PACKAGE.exists(), 'preserve frozen package')
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed stage before freezing')
    release = installed()
    h.require(h.read(CPU/'grouped/result.json')['passed'], 'CPU gate')
    inherited = h.read(PRIOR/'package/plan.json')
    a.verify_hashes(inherited['model_dir'], inherited['model_inputs'])
    sdk = sdk_identity()
    PACKAGE.mkdir(mode=0o700)
    releases = deployment.freeze(ROOT, BUNDLE)
    h.require(releases['releases']['previous']['hashes'] ==
              {'q4t':PREVIOUS_SHA, 'q4t.release.json':PREVIOUS_SIDECAR}, 'direct previous changed')
    h.save(PACKAGE/'release-history.json', catalog.history(ROOT/'build/q4t', ROOT/'build/q4t.release.json'))
    shutil.copytree(CPU/'grouped', PACKAGE/'cpu')
    # The old program has a v3 configuration shape. Its exact checked-in
    # schema remains separate from the current v4 contract.
    old_schema = subprocess.check_output(['git', 'show',
        '5174ab76:docs/serve-config-v1.schema.json'], cwd=ROOT)
    with (PACKAGE/'config-v3.schema.json').open('xb') as stream:stream.write(old_schema)
    h.copy_checked(h.identity(ROOT/'docs/serve-config-v1.schema.json'), PACKAGE/'config-v4.schema.json')
    paths = subprocess.check_output(['git', 'ls-files', 'tools', 'deploy/serve-ops'],
                                   cwd=ROOT, text=True).splitlines()
    paths += ['docs/SERVE_LIFECYCLE_2026-11-03.md', 'docs/MODEL.md']
    paths += [str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    h.save(PACKAGE/'sources.json', {p:h.sha(ROOT/p) for p in sorted(set(paths)) if (ROOT/p).is_file()})
    h.copy_checked(h.identity(release['tested_source_manifest']), PACKAGE/'runtime-sources.json')
    h.save(PACKAGE/'sdk.json', sdk)
    h.save(PACKAGE/'runtime-reuse.json', dict(
        build=h.identity(PRIOR/'cpu/build-result.json'),
        cpu=h.identity(PRIOR/'cpu/tests/result.json'),
        numerical=h.identity(PRIOR/'package/numerical-reuse.json'),
        native=h.identity(PRIOR/'execution/native/result.json'),
        business=h.identity(PRIOR/'execution/business/result.json'),
        runtime_sources=199, runtime_changed=False, new_build=False))
    plan = dict(root=str(ROOT), limits=LIMITS, planned=PLANNED,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        cycles=CYCLES, warmup=WARMUP, interval_seconds=INTERVAL, soak_seconds=SOAK_SECONDS,
        soak_max_seconds=SOAK_MAX, envelope=ENVELOPE,
        binary=h.identity(ROOT/'build/q4t'), sidecar=h.identity(ROOT/'build/q4t.release.json'),
        protocol_sha256=releases['releases']['current']['pair']['protocol_sha256'],
        model_dir=inherited['model_dir'], model_inputs=inherited['model_inputs'],
        sdk_directory=sdk['sdk_directory'], port=18109,
        release_bundle=str(BUNDLE), lease_root=str(OUTPUT), performance_acceptance=False,
        runtime_changed=False, final_default_changed=False)
    h.save(PACKAGE/'plan.json', plan)
    subprocess.run(['git', 'bundle', 'create', str(PACKAGE/'source.bundle'), '87639e97..HEAD'],
                   cwd=ROOT, check=True, timeout=60)
    h.seal(PACKAGE)
    print('PREPARED', SHA, flush=True)


def check(*, require_current=True):
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE/'plan.json')
    h.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and
        plan['planned'] == PLANNED and plan['cycles'] == CYCLES and
        plan['warmup'] == WARMUP and plan['envelope'] == ENVELOPE and
        plan['interval_seconds'] == INTERVAL and plan['soak_seconds'] == SOAK_SECONDS and
        plan['soak_max_seconds'] == SOAK_MAX, 'frozen contract changed')
    deployment.verify_bundle(BUNDLE, ROOT)
    if require_current:installed()
    a.verify_hashes(ROOT, h.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT, h.read(PACKAGE/'runtime-sources.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    h.require(sdk_identity() == h.read(PACKAGE/'sdk.json'), 'SDK changed')
    for record in h.identities(h.read(PACKAGE/'runtime-reuse.json')):h.check_identity(record)
    return plan


def execute():
    h.require_guard(OUTPUT)
    plan = check()
    events = [json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    plan['started'] = datetime.datetime.fromisoformat(next(row['utc'] for row in events
        if row['event'] == 'lease_acquired')).timestamp()
    h.save(OUTPUT/'begin.json', dict(started=plan['started'], limits=LIMITS))
    from tools.serve_lifecycle.verify import Run
    def interrupted(signum, frame):
        raise a.EnvironmentAbort('lease controller interrupted: '+str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    def expired(signum, frame):
        raise RuntimeError('original lifecycle deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM, expired)
    remaining = plan['started']+LIMITS['seconds']-90-time.time()
    h.require(remaining > 0, 'stage deadline already exhausted')
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:Run(plan, OUTPUT/'business').run()
    finally:signal.setitimer(signal.ITIMER_REAL, 0)


def finalize():
    check()
    guard = h.read(OUTPUT/'lease/exit.json')
    result = h.read(OUTPUT/'business/result.json')
    elapsed = time.time()-h.read(OUTPUT/'begin.json')['started']
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0 and
              result['status'] == 'PASS_BOUNDED_LIFECYCLE' and
              elapsed <= LIMITS['seconds'], 'bounded lifecycle/guard gate')
    h.save(OUTPUT/'finalized.json', dict(passed=True, elapsed_seconds=elapsed,
        installed_binary_unchanged=SHA, sidecar_unchanged=SIDECAR,
        performance_acceptance=False, physical_ram_hard_limit_proved=False,
        day_scale_stability_proved=False, daemon_supervisor_deployed=False))
    h.seal(OUTPUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'prepare', 'launch', 'execute', 'finalize'))
    args = parser.parse_args()
    if args.action == 'launch':
        check();OUTPUT.mkdir(mode=0o700)
        argv = ['/usr/bin/python3', '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
            '--output', str(OUTPUT/'lease'), '--', '/usr/bin/python3', '-B', str(Path(__file__)), 'execute']
        raise SystemExit(subprocess.call(argv, cwd=ROOT))
    globals()[args.action]()


if __name__ == '__main__':main()
