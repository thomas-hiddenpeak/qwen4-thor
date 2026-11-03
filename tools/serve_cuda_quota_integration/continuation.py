"""One bounded continuation after the native flags oracle rejected its first case."""
import argparse
import copy
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota_integration import common as c, cpu, stage, verify

BASE = c.BASE
ORIGINAL = BASE / 'package'
ORIGINAL_OUTPUT = BASE / 'execution'
PACKAGE = BASE / 'package-v2'
OUTPUT = BASE / 'execution-v2'
BUILD = BASE / 'flags-correction-build'
DOC = 'docs/SERVE_CUDA_QUOTA_FLAGS_CORRECTION_2026-11-04.md'
SOURCE = 'tools/serve_cuda_quota_integration/continuation.py'
PROBE = 'tools/serve_cuda_quota_integration/probe.cpp'
base_parse, base_check, base_verifier = verify.parse_probe, stage.check, stage.verifier


def flags_contract(rows):
    flags = [row for row in rows if row['event'] == 'host_flags']
    c.require(len(flags) == 1 and flags[0]['requested'] == 1 and
        flags[0]['control_bytes'] == 4096 and
        isinstance(flags[0]['wrapped'], int) and
        flags[0]['wrapped'] == flags[0]['direct'], 'native flags differ from control')
    return flags[0]


def parse_probe(mode, text):
    result = base_parse(mode, text)
    if mode == 'mixed':
        flags_contract([json.loads(line) for line in text.splitlines()])
    return result


class FlagsContracts(unittest.TestCase):
    def test_equal_driver_properties_need_not_echo_input_bits(self):
        for value in (0, 1, 3):
            self.assertEqual(flags_contract([dict(event='host_flags', requested=1,
                control_bytes=4096, wrapped=value, direct=value)])['direct'], value)

    def test_different_or_missing_control_rejected(self):
        for values in ([], [dict(event='host_flags', requested=1, control_bytes=4096,
                                 wrapped=3, direct=1)]):
            with self.assertRaises(RuntimeError):
                flags_contract(values)

    def test_duplicate_or_wrong_control_rejected(self):
        valid = dict(event='host_flags', requested=1, control_bytes=4096,
                     wrapped=3, direct=3)
        for rows in ([valid, valid], [dict(valid, requested=0)],
                     [dict(valid, control_bytes=0)]):
            with self.assertRaises(RuntimeError):
                flags_contract(rows)


def build():
    BUILD.mkdir()
    sources = {name: c.sha(ROOT / name) for name in (SOURCE, PROBE, DOC)}
    c.save(BUILD / 'sources.json', sources)
    ok = c.command(BUILD, 'build-probe', ['cmake', '--build', str(cpu.WRAPPERS),
        '--target', 'quota_wrapper_probe', '--parallel', '2'], timeout=180)
    zero = not any(re.search(r'\bwarning:', path.read_text(), re.I)
                   for path in BUILD.glob('*.std*'))
    log = io.StringIO()
    tests = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FlagsContracts))
    (BUILD / 'tests.log').write_text(log.getvalue())
    stage.h.copy_checked(stage.h.identity(cpu.WRAPPERS / 'quota_wrapper_probe'),
                         BUILD / 'native')
    passed = ok and zero and tests.wasSuccessful() and tests.testsRun == 3
    c.save(BUILD / 'result.json', dict(passed=passed, zero_warnings=zero,
        tests=tests.testsRun, sources=sources, native=c.identity(BUILD / 'native'),
        production_unchanged=c.sources() == c.read(ORIGINAL / 'candidate/runtime-source.json'),
        old_driver_tests_reused=c.identity(BASE / 'driver-cpu/result.json')))
    c.require(passed, 'flags correction build/tests failed')
    print(log.getvalue(), end='')


def original_failure():
    stage.h.verify_seal(ORIGINAL)
    c.require(c.read(ORIGINAL_OUTPUT / 'lease/exit.json')['status'] == 'COMMAND_FAILED',
              'original guard failure changed')
    failed = c.read(ORIGINAL_OUTPUT / 'business/failed.json')
    c.require(failed['counts'] == dict(native=1, starts=0, controls=0, generations=0),
              'unexpected original attempts')
    c.require('FAIL: host flags preserved' in
        (ORIGINAL_OUTPUT / 'business/native-mixed/stderr.log').read_text(),
        'different original failure')
    c.require(c.sources() == c.read(ORIGINAL / 'candidate/runtime-source.json'),
              'production candidate changed')
    old_sources = c.read(ORIGINAL / 'sources.json')
    changed = {name for name, digest in old_sources.items() if c.sha(ROOT / name) != digest}
    c.require(changed == {PROBE}, 'continuation changes exceed native oracle')
    return failed


def prepare():
    original_failure()
    c.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit corrected probe and continuation first')
    built = c.read(BUILD / 'result.json')
    c.require(built['passed'] and built['production_unchanged'], 'correction CPU gate')
    stage.a.verify_hashes(ROOT, built['sources'])
    stage.h.check_identity(built['native'])
    c.require(not PACKAGE.exists() and not OUTPUT.exists(), 'no retry/overwrite')
    PACKAGE.mkdir()
    # Copy the frozen inputs into a NEW package. The original seal, source map
    # and failed plan remain untouched; this package gets its own identities.
    skipped = {'sealed-files.json', 'plan.json', 'sources.json',
               'candidate/quota_wrapper_probe'}
    for path in ORIGINAL.rglob('*'):
        relative = path.relative_to(ORIGINAL)
        if path.is_dir():
            (PACKAGE / relative).mkdir(exist_ok=True)
        elif relative.as_posix() not in skipped:
            stage.h.copy_checked(stage.h.identity(path), PACKAGE / relative)
    stage.h.copy_checked(built['native'], PACKAGE / 'candidate/quota_wrapper_probe')
    sources = c.read(ORIGINAL / 'sources.json')
    sources.update({name: c.sha(ROOT / name) for name in (PROBE, SOURCE, DOC)})
    c.save(PACKAGE / 'sources.json', sources)
    plan = copy.deepcopy(c.read(ORIGINAL / 'plan.json'))
    for key, name in [('binary', 'q4t'), ('native_probe', 'quota_wrapper_probe'),
                       ('lifecycle_probe', 'q4t_http_lifecycle_tests')]:
        plan[key] = c.identity(PACKAGE / 'candidate' / name)
    plan['candidate'] = plan['binary']['path']
    plan['source_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                   cwd=ROOT, text=True).strip()
    plan['continuation'] = dict(original_plan=c.identity(ORIGINAL / 'plan.json'),
        original_guard=c.identity(ORIGINAL_OUTPUT / 'lease/exit.json'),
        original_failure=c.identity(ORIGINAL_OUTPUT / 'business/failed.json'),
        original_attempts=dict(native=1, model=0, generation=0),
        additional_attempts=dict(native=6, starts=3, generations=7),
        correction_build=c.identity(BUILD / 'result.json'),
        production_binary_unchanged=True, no_automatic_retry=True)
    c.save(PACKAGE / 'plan.json', plan)
    stage.h.seal(PACKAGE)


def check():
    plan = base_check()
    original_failure()
    for key in ('original_plan', 'original_guard', 'original_failure', 'correction_build'):
        record = plan['continuation'][key]
        c.require(c.identity(record['path']) == record, 'original evidence changed')
    return plan


def verifier(plan, installed=False):
    if installed:
        return base_verifier(plan, installed=True)
    verify.parse_probe = parse_probe
    verify.Run(plan, OUTPUT / 'business').run()


def bind():
    stage.PACKAGE, stage.OUTPUT = PACKAGE, OUTPUT
    stage.check, stage.verifier = check, verifier
    stage.bind()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'prepare', 'launch', 'execute', 'finalize'])
    args = parser.parse_args()
    if args.action == 'build':
        build()
    elif args.action == 'prepare':
        prepare()
    elif args.action == 'launch':
        bind()
        check()
        OUTPUT.mkdir()
        raise SystemExit(subprocess.call(['/usr/bin/python3', '-B',
            str(ROOT / 'tools/evalscope/gpu_exclusive.py'), '--output', str(OUTPUT / 'lease'),
            '--', '/usr/bin/python3', '-B', str(Path(__file__).resolve()), 'execute'], cwd=ROOT))
    else:
        bind()
        getattr(stage, args.action)()
