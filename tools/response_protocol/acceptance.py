"""Freeze identities or explicitly execute one bounded chat acceptance stage.

freeze only reads inputs and writes a plan. execute launches the existing GPU
guard for the whole stage, including host checks. No deploy, retries, or service
stops. This tool itself is development-only until its contracts are executed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
LIMITS = dict(generations=24, planned_generations=23, negatives=10, starts=3,
              http_seconds=1800, stage_seconds=2400)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def install_interrupt_handlers():
    interrupted = False

    def interrupt(signum, _frame):
        nonlocal interrupted
        if interrupted:
            return  # Repeated signals must not interrupt bounded cleanup.
        interrupted = True
        raise KeyboardInterrupt('acceptance interrupted by ' +
                                signal.Signals(signum).name)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupt)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def source_paths():
    # Include new files during development; exclude ignored build/model data.
    raw = subprocess.check_output(['git', 'ls-files', '-z', '--cached',
        '--others', '--exclude-standard', '--', 'src', 'include', 'cmake',
        'tests', 'tools', 'CMakeLists.txt'], cwd=ROOT)
    return sorted(set(raw.decode().rstrip('\0').split('\0')))


def artifact(path):
    path = Path(path).resolve(strict=True)
    return dict(path=str(path), sha256=digest(path))


def validate_plan(plan):
    require(type(plan.get('schema_version')) is int and
            plan['schema_version'] == 1 and plan.get('limits') == LIMITS,
            'unsupported plan or changed frozen budget')
    require(plan.get('root') == str(ROOT), 'plan belongs to another worktree')
    require(set(plan.get('artifacts', {})) == {'candidate', 'baseline',
            'host_tests', 'template_checker', 'identity_header',
            'identity_sources', 'python'},
            'incomplete artifact plan')
    for value in plan['artifacts'].values():
        require(artifact(value['path']) == value, 'artifact changed: ' + value['path'])
    require(set(plan['sources']) == set(source_paths()), 'source inventory changed')
    for path, expected in plan['sources'].items():
        require(digest(ROOT / path) == expected, 'source changed: ' + path)
    for path, expected in plan['model_inputs'].items():
        require(digest(Path(plan['model_dir']) / path) == expected,
                'model/tokenizer identity changed: ' + path)
    header = Path(plan['artifacts']['identity_header']['path']).read_text()
    match = re.search(r'kProtocol\[\] = "([0-9a-f]{64})";', header)
    require(match and match[1] == plan['protocol_sha256'], 'protocol header mismatch')
    sources = json.loads(Path(plan['artifacts']['identity_sources']['path']).read_text())
    require(sources and all(plan['sources'].get(path) == expected
            for path, expected in sources.items()), 'stale protocol build inputs')
    material = ''.join(f'{path}:{sources[path]}\n' for path in sorted(sources))
    require(hashlib.sha256(material.encode()).hexdigest() == plan['protocol_sha256'],
            'generated header/source manifest mismatch')
    require(type(plan.get('port')) is int and 1024 <= plan['port'] <= 65535,
            'invalid loopback port')


def freeze(args):
    output = args.output.resolve()
    require(output.is_relative_to(ROOT / '.q4t-work'), 'plan output location')
    model_dir = args.model_dir.resolve(strict=True)
    inputs = ['config.json', 'tokenizer.json', 'tokenizer_config.json',
              'chat_template.jinja']
    files = {key: artifact(getattr(args, key)) for key in
             ('candidate', 'baseline', 'host_tests', 'template_checker',
              'identity_header', 'python')}
    files['identity_sources'] = artifact(Path(files['identity_header']['path'])
                                         .with_name('chat_protocol_sources.json'))
    header = Path(files['identity_header']['path']).read_text()
    match = re.search(r'kProtocol\[\] = "([0-9a-f]{64})";', header)
    require(match, 'missing generated protocol identity')
    plan = dict(schema_version=1, root=str(ROOT), limits=LIMITS,
        artifacts=files, protocol_sha256=match[1], model_dir=str(model_dir),
        model_inputs={name: digest(model_dir / name) for name in inputs},
        sources={name: digest(ROOT / name) for name in source_paths()},
        port=args.port, status='FROZEN_NOT_EXECUTED',
        limitation='Source/artifact association, not reproducible-build or '
                   'weight-content attestation; no release approval.')
    validate_plan(plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, plan)


def commands(plan, output):
    a = {key: value['path'] for key, value in plan['artifacts'].items()}
    python = [a['python'], '-B']
    return [
        ('host', 200, python + [str(ROOT / 'tools/release/check_host_contracts.py'),
            '--test-binary', a['host_tests'], '--output', str(output / 'host')]),
        ('client', 100, python + [str(ROOT / 'tools/examples/test_chat_client.py')]),
        ('recovery', 100, python + [str(ROOT / 'tools/examples/test_tool_recovery.py')]),
        ('orchestration', 100, python + [str(ROOT / 'tools/response_protocol/test_acceptance.py')]),
        ('template', 60, [a['template_checker'], str(ROOT /
            'tools/evalscope/fixtures/chat_template_cases.jsonl')]),
        ('http', 1860, python + [str(ROOT / 'tools/response_protocol/verify.py'),
            '--candidate', a['candidate'], '--baseline', a['baseline'],
            '--model-dir', plan['model_dir'], '--output', str(output / 'http'),
            '--port', str(plan['port']), '--protocol-sha256', plan['protocol_sha256']])]


def worker(plan, output):
    sys.path.insert(0, str(ROOT / 'tools/verify/startup_delivery'))
    from deploy import require_guard
    require_guard(output)
    deadline = time.monotonic() + LIMITS['stage_seconds']
    status, error = 'FAIL', None
    steps = []
    try:
        for name, timeout, command in commands(plan, output):
            validate_plan(plan)
            require_guard(output)
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'stage deadline exhausted')
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith('Q4T_') and key not in
                   ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME')}
            temp = output / (name + '-temporary')
            temp.mkdir()
            env.update(Q4T_TEST_OUTPUT_DIR=str(temp), TMPDIR=str(temp),
                       PYTHONDONTWRITEBYTECODE='1')
            record = dict(name=name, argv=command, timeout=min(timeout, remaining))
            save(output / (name + '-command.json'), record)
            started = time.monotonic()
            try:
                with (output / (name + '.log')).open('x') as log:
                    result = subprocess.run(command, cwd=ROOT, env=env,
                        stdout=log, stderr=subprocess.STDOUT,
                        timeout=record['timeout'])
                record['returncode'] = result.returncode
                require(result.returncode == 0, 'step failed: ' + name)
            finally:
                record['elapsed_seconds'] = time.monotonic() - started
                steps.append(record)
                save(output / (name + '-exit.json'), record)
        validate_plan(plan)
        status = 'CHECKS_PASSED_PENDING_GUARD_EXIT'
    except BaseException as exception:
        error = repr(exception)
        raise
    finally:
        save(output / 'worker-result.json', dict(status=status, error=error, steps=steps))


def combined_status(worker_result, guard, returncode):
    if guard.get('status') in ('ENV_ABORT', 'LEASE_BUSY', 'RECOVERY_REQUIRED') or \
            guard.get('cleanup_error'):
        return 'ENV_ABORT'
    if (returncode == 0 and guard.get('status') == 'COMPLETED' and
            guard.get('child_returncode') == 0 and guard.get('finished') and
            worker_result.get('status') == 'CHECKS_PASSED_PENDING_GUARD_EXIT'):
        return 'PASS'
    return 'FAIL'


def execute(plan, output):
    validate_plan(plan)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    save(output / 'plan.json', plan)
    python = plan['artifacts']['python']['path']
    command = [python, '-B', str(ROOT / 'tools/evalscope/gpu_exclusive.py'),
        '--output', str(output / 'lease'), '--', python, '-B', str(Path(__file__).resolve()),
        'worker', '--plan', str(output / 'plan.json'), '--output', str(output)]
    save(output / 'guard-command.json', dict(argv=command))
    guard = None
    status, error = 'FAIL', None
    try:
        with (output / 'guard.log').open('x') as log:
            guard = subprocess.Popen(command, cwd=ROOT, stdout=log,
                                     stderr=subprocess.STDOUT)
            guard.wait(timeout=LIMITS['stage_seconds'] + 120)
        status = 'ENV_ABORT'  # Missing monitor evidence cannot establish a run.
        exit_record = json.loads((output / 'lease/exit.json').read_text())
        worker_path = output / 'worker-result.json'
        worker_result = json.loads(worker_path.read_text()) if worker_path.exists() else {}
        status = combined_status(worker_result, exit_record, guard.returncode)
        if status == 'PASS':
            # Do not retain PASS if final identity checks themselves fail.
            status = 'FAIL'
            validate_plan(plan)
            status = 'PASS'
        require(status == 'PASS', 'stage did not pass: ' + status)
    except BaseException as exception:
        if status != 'ENV_ABORT':
            status = 'FAIL'
        error = repr(exception)
        raise
    finally:
        try:
            if guard is not None and guard.poll() is None:
                guard.terminate()  # Guard drains only its own descendants.
                guard.wait(timeout=90)
        except BaseException as cleanup_error:
            status = 'ENV_ABORT'
            error = str(error) + '; cleanup: ' + repr(cleanup_error)
            raise
        finally:
            save(output / 'result.json', dict(status=status, error=error,
                performance_acceptance=False, deployed=False,
                scope='Bounded chat integration only; no deployment authorization.'))


def main():
    install_interrupt_handlers()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    f = sub.add_parser('freeze', help='read-only identities; no tests/probes')
    for name in ('candidate', 'baseline', 'host-tests', 'template-checker',
                 'identity-header', 'model-dir'):
        f.add_argument('--' + name, type=Path, required=True)
    f.add_argument('--python', type=Path, default=Path('/usr/bin/python3'))
    f.add_argument('--port', type=int, default=18096)
    f.add_argument('--output', type=Path, required=True)
    for name in ('execute', 'worker'):
        command = sub.add_parser(name)
        command.add_argument('--plan', type=Path, required=True)
        command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'freeze':
        freeze(args)
        return
    output = args.output.resolve()
    require(output.is_relative_to(ROOT / '.q4t-work'), 'stage output location')
    plan = json.loads(args.plan.read_text())
    (worker if args.command == 'worker' else execute)(plan, output)


if __name__ == '__main__':
    main()
