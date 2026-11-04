"""Independent heartbeat receipt/state adapter; no HTTP listener or sender."""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_watchdog import core as c
from tools.serve_watchdog.storage import Directory, attributes, encoded, sha

FILES = ('tools/serve_watchdog/core.py', 'tools/serve_watchdog/storage.py',
         'tools/serve_watchdog/watchdog.py')


def now():
    # BOOTTIME includes suspend; monotonic alone would hide suspended time.
    return c.clock(dict(boot_id=Path('/proc/sys/kernel/random/boot_id').
        read_text().strip(), boottime=time.clock_gettime(time.CLOCK_BOOTTIME),
        unix_seconds=time.time()))


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def token_file(path):
    path = Path(path).absolute()
    c.require(path == path.resolve(strict=True), 'credential_symlink')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        attributes(os.fstat(fd))
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            token = stream.read(514)
        c.require(re.fullmatch(rb'[A-Za-z0-9_~+/.=-]{32,512}\n?', token),
                  'credential_format')
        return token.strip()
    finally:
        os.close(fd)


def initialize(path, deployment, credential):
    c.require(isinstance(deployment, str) and re.fullmatch(
        r'[a-zA-Z0-9_-]{1,64}', deployment), 'deployment')
    token = token_file(credential)
    path = Path(path).absolute()
    c.require(path == path.resolve(), 'state_symlink')
    # Never creates parent directories or overwrites any historical state.
    path.mkdir(mode=0o700)
    for name in FILES:
        target = path/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (ROOT/name).read_bytes()
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o400)
    python = Path(sys.executable).resolve()
    policy = dict(version=1, deployment=deployment, timeout_seconds=c.TIMEOUT,
        history_entries=c.HISTORY, token_sha256=sha(token),
        sources={name:file_sha(path/'source'/name) for name in FILES},
        python=dict(path=str(python), sha256=file_sha(python)))
    digest = sha(encoded(policy))
    with Directory(path) as directory:
        directory.write('token-private', token, replace=False)
        directory.write('policy.json', encoded(policy), replace=False)
        directory.write('ledger.json', encoded(c.observe(
            c.new_state(digest, now()), now())), replace=False)
    return dict(state=str(path), policy_sha256=digest,
                automatic_start=False, external_receiver_accepted=False)


def verify(directory, digest):
    c.require(isinstance(digest, str) and
              re.fullmatch(r'[0-9a-f]{64}', digest), 'policy_sha256')
    raw = directory.read('policy.json')
    c.require(sha(raw) == digest, 'policy_changed')
    policy = c.parse(raw)
    c.require(isinstance(policy, dict) and set(policy) == {
        'version', 'deployment', 'timeout_seconds', 'history_entries',
        'token_sha256', 'sources', 'python'}, 'policy_fields')
    c.require(policy['version'] == 1 and type(policy['version']) is int and
        policy['timeout_seconds'] == c.TIMEOUT and
        policy['history_entries'] == c.HISTORY and
        isinstance(policy['deployment'], str) and re.fullmatch(
            r'[a-zA-Z0-9_-]{1,64}', policy['deployment']) and
        set(policy['sources']) == set(FILES), 'policy_contract')
    for name, wanted in policy['sources'].items():
        c.require(file_sha(ROOT/name) == wanted and
            file_sha(directory.path/'source'/name) == wanted,
            'use_pinned_source')
    python = policy['python']
    c.require(Path(python['path']) == Path(sys.executable).resolve() and
              file_sha(python['path']) == python['sha256'], 'python_changed')
    token = directory.read('token-private', maximum=512)
    c.require(sha(token) == policy['token_sha256'], 'credential_changed')
    return policy, token


def ingest(path, digest, raw, authorization):
    """HTTPS adapters call this before acknowledging a heartbeat delivery.

    Authorization comes from the received header, not argv or environment.
    Exceptions have fixed messages; no credential or webhook body is logged.
    Ordinary alerts need their own durable receiver and MUST NOT be swallowed.
    """
    with Directory(path) as directory:
        policy, token = verify(directory, digest)
        c.require(isinstance(authorization, str) and
            authorization.isascii() and len(authorization) <= 1024 and
            secrets.compare_digest(authorization, 'Bearer '+token.decode()),
            'unauthorized')
        alert = c.heartbeat(raw, policy['deployment'])
        with directory.claim():
            state = c.validate(directory.json('ledger.json'), digest)
            updated, disposition = c.receive(state, alert, now())
            directory.write('ledger.json', encoded(updated))
            return dict(disposition=disposition,
                status=c.status(updated, now()), durable=True)


def inspect(path, digest, persist=False):
    with Directory(path) as directory:
        policy, _ = verify(directory, digest)
        # The ledger is replaced atomically. Readers can use a complete old
        # or new snapshot without creating locks or mutating historical state.
        with directory.claim() if persist else nullcontext():
            state = c.validate(directory.json('ledger.json'), digest)
            observed = now()
            if persist:
                updated = c.observe(state, observed)
                if updated != state:
                    directory.write('ledger.json', encoded(updated))
                state = updated
            value = c.status(state, observed)
            value['deployment'] = policy['deployment']
            if persist:
                # Fixed output names and ring history bound disk use across
                # watchdog restarts. Never acknowledge via this projection.
                directory.write('status.json', encoded(value))
                directory.write('metrics.prom', c.metrics(
                    value, policy['deployment']).encode())
            return value


def watch(path, digest):
    stopping = threading.Event()
    previous = {sig:signal.signal(sig, lambda *_: stopping.set())
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with Directory(path) as directory, directory.claim('watch.lock', 0):
            while not stopping.is_set():
                inspect(path, digest, persist=True)
                stopping.wait(1)
        return 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def unit(path, digest):
    with Directory(path) as directory:
        policy, _ = verify(directory, digest)
    entry = Path(path).resolve()/'source/tools/serve_watchdog/watchdog.py'
    argv = [policy['python']['path'], '-I', '-B', str(entry), 'watch',
            '--state', str(Path(path).resolve()), '--policy-sha256', digest]
    c.require(all(re.fullmatch(r'[A-Za-z0-9_./=-]+', a) for a in argv),
              'unit_path_characters')
    return ('[Unit]\nDescription=Q4T independent heartbeat watchdog\n\n'
        '[Service]\nType=simple\nExecStart=' + ' '.join(argv) + '\n'
        'Restart=no\nTimeoutStopSec=5\nKillMode=control-group\n'
        'UMask=0077\nNoNewPrivileges=yes\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=(
        'init', 'ingest', 'status', 'metrics', 'watch', 'unit'))
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--policy-sha256')
    parser.add_argument('--deployment')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--authorization-fd', type=int)
    args = parser.parse_args()
    try:
        if args.action == 'init':
            c.require(args.token_file is not None, 'token_file_required')
            value = initialize(args.state, args.deployment, args.token_file)
        elif args.action == 'ingest':
            c.require(args.authorization_fd is not None and
                args.authorization_fd >= 3, 'authorization_fd_required')
            with os.fdopen(os.dup(args.authorization_fd), 'rb') as stream:
                auth = stream.read(1025).decode('ascii').rstrip('\n')
            value = ingest(args.state, args.policy_sha256,
                sys.stdin.buffer.read(c.MAXIMUM+1), auth)
        elif args.action == 'watch':
            return watch(args.state, args.policy_sha256)
        elif args.action == 'unit':
            print(unit(args.state, args.policy_sha256), end='')
            return 0
        else:
            value = inspect(args.state, args.policy_sha256)
            if args.action == 'metrics':
                print(c.metrics(value, value['deployment']), end='')
                return 0
        print(json.dumps(value, sort_keys=True))
        return 0
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as error:
        # Disk/metadata failures must not refresh an old healthy .prom file.
        # Independent collection must detect its missing/stale timestamp.
        message = str(error) if isinstance(error, c.ContractError) else 'io_or_state_failure'
        print(json.dumps(dict(state='UNKNOWN', healthy=False, error=message)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
