"""One bounded CPU group; retains all attempts and leaves frozen SRV-02h alone."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-media-plan-core-20261103'
sys.path.insert(0, str(ROOT))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def sources():
    paths = [ROOT/'include/q4t/vision/media_plan.h',
             ROOT/'src/vision/media_plan.cpp',
             ROOT/'include/q4t/vision/processor.h',
             ROOT/'src/vision/processor.cpp',
             ROOT/'third_party/stb/stb_image.h',
             ROOT/'docs/SERVE_MEDIA_PLAN_CORE_2026-11-03.md',
             *sorted((ROOT/'tools/serve_media_resource').glob('*'))]
    return {str(p.relative_to(ROOT)): sha(p) for p in paths if p.is_file()}


def command(folder, name, argv, timeout=180):
    before = time.monotonic()
    env = dict(os.environ, ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',
               UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1',
               PYTHONDONTWRITEBYTECODE='1')
    error = None
    with (folder/(name+'.stdout')).open('wb') as out, \
            (folder/(name+'.stderr')).open('wb') as err:
        try:
            result = subprocess.run(argv, cwd=ROOT, stdout=out, stderr=err,
                                    env=env, timeout=timeout, check=False)
            code = result.returncode
        except subprocess.TimeoutExpired as failure:
            code, error = None, str(failure)
    save(folder/(name+'.json'), dict(argv=argv, exit_code=code, error=error,
         seconds=time.monotonic()-before))
    print(name, 'PASS' if code == 0 else 'FAIL', flush=True)
    return code == 0


def run(folder):
    folder = folder.resolve()
    if not folder.is_relative_to(BASE):
        raise ValueError('output must be below the stage artifact directory')
    folder.mkdir(parents=True, exist_ok=False)
    initial = sources()
    save(folder/'sources.json', initial)
    defaults = {p: sha(ROOT/p) for p in ('build/q4t', 'build/q4t.release.json')}
    save(folder/'begin.json', dict(utc=datetime.datetime.now(
        datetime.timezone.utc).isoformat(), defaults=defaults,
        git_head=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                         cwd=ROOT, text=True).strip()))
    from tools.serve_media_failure import stage as frozen
    plan = frozen.check()
    frozen.cpu_gate()
    save(folder/'frozen-before.json', dict(passed=True,
         candidate=plan['candidate_sha256'], sources_unchanged=True))
    checks = {}
    checks['compiler'] = command(folder, 'compiler', ['g++-14', '--version'])
    checks['cmake-version'] = command(folder, 'cmake-version', ['cmake', '--version'])
    checks['configure'] = command(folder, 'configure', ['cmake', '-S',
        str(ROOT/'tools/serve_media_resource'), '-B', str(folder/'build'),
        '-DCMAKE_BUILD_TYPE=RelWithDebInfo', '-DCMAKE_CXX_COMPILER=g++-14',
        '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'])
    if checks['configure']:
        checks['build'] = command(folder, 'build', ['cmake', '--build',
            str(folder/'build'), '--parallel', '2'])
    warnings = [p.name for p in folder.glob('*.std*')
                if re.search(r'\bwarning:', p.read_text(), re.IGNORECASE)]
    checks['zero-warnings'] = not warnings
    if all(checks.values()):
        checks['contract'] = command(folder, 'contract',
            [str(folder/'build/media_plan_contract')])
        checks['fixtures'] = command(folder, 'fixtures',
            [str(ROOT/'.q4t-work/refenv/bin/python'), '-B',
             str(ROOT/'tools/serve_media_resource/fixtures.py'),
             str(folder/'fixtures')])
        if checks['fixtures']:
            fixture = json.loads((folder/'fixtures/manifest.json').read_text())
            checks['processor'] = command(folder, 'processor',
                [str(folder/'build/media_plan_processor'),
                 *[r['path'] for r in fixture['fixtures']]])
    frozen.check()
    frozen.cpu_gate()
    unchanged = sources() == initial
    default_unchanged = all(sha(ROOT/p) == h for p, h in defaults.items())
    binaries = {p.name: dict(path=str(p), sha256=sha(p), bytes=p.stat().st_size)
                for p in (folder/'build/media_plan_contract',
                          folder/'build/media_plan_processor') if p.is_file()}
    save(folder/'result.json', dict(passed=all(checks.values()) and unchanged and
         default_unchanged, checks=checks, zero_warnings=not warnings,
         sources_unchanged=unchanged, default_pair_unchanged=default_unchanged,
         frozen_02h_gate_passed=True, binaries=binaries,
         sanitizers=['address', 'undefined'], model_or_http_started=False,
         stage_gpu_tests=0))
    if not all(checks.values()) or not unchanged or not default_unchanged:
        raise RuntimeError('CPU group failed; preserve and repair affected checks')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.output)
