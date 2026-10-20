"""Bounded, same-uid owner of temporary file-cache locks; no CUDA imports."""
import argparse
import ctypes
import hashlib
import json
import mmap
import math
import os
from pathlib import Path
import resource
import selectors
import signal
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
MODEL = (Path.home() / 'models/dev/llm/garnermccloud/'
         'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
PAGE = os.sysconf('SC_PAGE_SIZE')
HOT_BYTES = 67987791872


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def identity(pid):
    """Read a birth identity; root launcher limits may be unreadable."""
    base = Path('/proc') / str(pid)
    fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in
                  (base / 'status').read_text().splitlines() if ':' in line)
    try:
        memlock = list(resource.prlimit(pid, resource.RLIMIT_MEMLOCK))
        limit_error = None
    except OSError as error:
        memlock, limit_error = None, repr(error)
    return dict(pid=pid, ppid=int(fields[1]), start_ticks=int(fields[19]),
                state=fields[0], uid=[int(x) for x in status['Uid'].split()],
                gid=[int(x) for x in status['Gid'].split()],
                capabilities={k: status[k].strip() for k in
                              ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')},
                no_new_privs=int(status['NoNewPrivs']),
                cgroup=(base / 'cgroup').read_text(),
                vmlck_bytes=(int(status['VmLck'].split()[0]) * 1024
                             if 'VmLck' in status else None),
                memlock=memlock, memlock_error=limit_error)


def make_manifest(paths, fixture=False):
    """Freeze file identity, never payload contents; fixture mode is <=8 MiB."""
    paths = [Path(path).absolute() for path in paths]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('empty or duplicate file selection')
    if fixture:
        if len(paths) > 2 or any(not p.resolve().is_relative_to(
                ROOT / '.q4t-work') for p in paths):
            raise ValueError('fixtures require <=2 files inside .q4t-work')
    else:
        expected = [MODEL / f'layer-{layer:05d}-experts-{lo:04d}-{lo+127:04d}.safetensors'
                    for layer in range(48) for lo in range(0, 512, 128)]
        if paths != expected:
            raise ValueError('production requires the exact ordered 192 shards')
    files = []
    for path in paths:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            raise ValueError('only nonempty regular files, no symlinks')
        files.append(dict(path=str(path), dev=st.st_dev, inode=st.st_ino,
                          size=st.st_size, mtime_ns=st.st_mtime_ns,
                          lock_bytes=((st.st_size + PAGE - 1) // PAGE) * PAGE))
    total = sum(row['lock_bytes'] for row in files)
    if not fixture and total != HOT_BYTES:
        raise ValueError('production lock total differs from frozen HOT_BYTES')
    if fixture and total > 8 << 20:
        raise ValueError('fixture total exceeds 8 MiB')
    return dict(schema=1, fixture=fixture, page_size=PAGE, files=files,
                total_lock_bytes=total)


class LockedWarm:
    """Self-limited launcher -> unprivileged READY -> LIMIT/HELD -> release.

    Privileged startup changes only the launcher's own limit, then drops all
    capabilities/UID/GID before Python starts. There is no privileged actor
    that can subsequently mutate a numeric target PID.
    """
    def __init__(self, output, manifest, timeout=180, use_sudo=False):
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError('helper phase timeout must be in (0, 300] seconds')
        if os.getuid() == 0 or os.getuid() != os.geteuid():
            raise ValueError('the controlling owner must be unprivileged')
        self.output = Path(output).resolve()
        if not self.output.is_relative_to(ROOT / '.q4t-work'):
            raise ValueError('output must be a new .q4t-work directory')
        self.output.mkdir(parents=True, exist_ok=False)
        self.manifest = json.loads(json.dumps(manifest))
        self.timeout = timeout
        self.events = []
        self.ready = self.held = self.summary = None
        self.granted = False
        self.exit_record = None
        self._buffer = b''
        self._parent_seq = 0
        self._logging_errors = []
        self._helper_pidfd = None
        self.identity = None
        self.use_sudo = use_sudo
        self.owner_identity = identity(os.getpid())
        save(self.output / 'manifest.json', self.manifest)
        self._log = (self.output / 'parent-events.jsonl').open('x')
        self._stderr = (self.output / 'helper.stderr').open('xb')
        command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                   '--helper', str(self.output), '--owner-pid', str(os.getpid()),
                   '--owner-start-ticks', str(self.owner_identity['start_ticks']),
                   '--timeout', str(timeout)]
        if use_sudo:
            total = self.manifest['total_lock_bytes']
            command = ['/usr/bin/sudo', '-n', '/usr/bin/prlimit',
                       '--memlock=' + str(total) + ':' + str(total), '--',
                       '/usr/bin/setpriv', '--reuid=' + str(os.getuid()),
                       '--regid=' + str(os.getgid()), '--init-groups', '--reset-env',
                       '--bounding-set=-all', '--inh-caps=-all',
                       '--ambient-caps=-all', '--no-new-privs', *command]
        save(self.output / 'command.json', command)
        save(self.output / 'owner-before.json', self.owner_identity)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and
               k not in ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME')}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE,
                                        stderr=self._stderr, env=env)
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.ready = self._wait('ready', min(timeout, 15))
            actual = identity(self.ready['identity']['pid'])
            if actual['start_ticks'] != self.ready['identity']['start_ticks']:
                raise RuntimeError('READY helper birth differs')
            chain, current = [], actual
            for _ in range(16):
                chain.append(current)
                if current['pid'] == os.getpid():
                    break
                current = identity(current['ppid'])
            if (chain[-1]['pid'] != os.getpid() or
                    self.process.pid not in [x['pid'] for x in chain]):
                raise RuntimeError('READY helper is outside owned launch chain')
            self.identity = actual
            self._helper_pidfd = os.pidfd_open(actual['pid'])
            self._check_identity()
            if actual['vmlck_bytes'] != 0:
                raise RuntimeError('helper has unexpected initial locks')
            if use_sudo and (actual['memlock'] != [
                    self.manifest['total_lock_bytes']] * 2 or
                    actual['no_new_privs'] != 1 or
                    int(actual['capabilities']['CapBnd'], 16) != 0):
                raise RuntimeError('privileged chain did not drop privilege/limit')
            self._record('ready', identity=actual, launch_chain=chain)
        except BaseException as error:
            self._record('failure', phase='ready', error=repr(error))
            self.close()
            raise

    def _record(self, event, **data):
        row = dict(seq=self._parent_seq, event=event,
                   mono=time.monotonic(), wall=time.time(), **data)
        self._parent_seq += 1
        try:
            self._log.write(json.dumps(row) + '\n')
            self._log.flush()
        except (OSError, ValueError) as error:
            self._logging_errors.append(repr(error))

    def _check_identity(self):
        current = identity(self.identity['pid'])
        if (current['start_ticks'] != self.identity['start_ticks'] or
                current['uid'] != [os.getuid()] * 4 or
                current['gid'][:2] != [os.getgid(), os.getegid()] or
                current['state'] == 'Z' or any(int(current['capabilities'][k], 16)
                    for k in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb'))):
            raise RuntimeError('helper birth/uid/capability/liveness changed')
        return current

    def _consume(self, line):
        row = json.loads(line)
        if row.get('seq') != len(self.events):
            raise RuntimeError('helper event sequence differs')
        self.events.append(row)
        if row['event'] == 'summary':
            self.summary = row
        return row

    def _wait(self, wanted, timeout):
        deadline = time.monotonic() + timeout
        while True:
            while b'\n' in self._buffer:
                line, self._buffer = self._buffer.split(b'\n', 1)
                row = self._consume(line)
                if row['event'] == wanted:
                    return row
                if row['event'] == 'summary':
                    raise RuntimeError('helper stopped before ' + wanted +
                                       ': ' + json.dumps(row))
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise TimeoutError('helper timeout waiting for ' + wanted)
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError('helper EOF before ' + wanted)
            self._buffer += data

    def _send(self, command):
        self.process.stdin.write((command + '\n').encode())
        self.process.stdin.flush()
        self._record('command', command=command)

    def grant(self, use_sudo=False, limit_bytes=None):
        """Ask the unprivileged helper to set its own already-permitted limit.

        use_sudo is retained for caller compatibility; privileged startup must
        be selected at construction. This method never runs sudo or prlimit.
        """
        if self.granted or self.held is not None:
            raise RuntimeError('grant is only allowed once before START')
        target = self.manifest['total_lock_bytes']
        if limit_bytes is not None:
            if not self.manifest['fixture'] or not 0 <= limit_bytes <= target:
                raise ValueError('only fixtures may request a smaller quota')
            target = limit_bytes
        before = self._check_identity()
        hard = before['memlock'][1]
        if hard != resource.RLIM_INFINITY and target > hard:
            self._record('failure', phase='grant', reason='hard_limit_too_small')
            raise PermissionError('construct with use_sudo=True for higher limit')
        self._record('grant_begin', identity=before, limit_bytes=target)
        self._send('LIMIT ' + str(target))
        granted = self._wait('granted', 5)
        after = self._check_identity()
        if (after['memlock'] != [target, target] or
                granted['identity']['memlock'] != [target, target]):
            raise RuntimeError('helper self-limit verification failed')
        self.granted = True
        self._record('grant_end', identity=after)
        return after

    def acquire(self):
        if not self.granted or self.held is not None:
            raise RuntimeError('START requires one completed grant')
        self._send('START')
        self.held = self._wait('held', self.timeout)
        current = self._check_identity()
        if (self.held['locked_bytes'] != self.manifest['total_lock_bytes'] or
                current['vmlck_bytes'] != self.manifest['total_lock_bytes']):
            raise RuntimeError('HELD lock accounting differs from manifest')
        self._record('held', identity=current)
        return self.held

    def release(self):
        if self.held is None or self.exit_record is not None:
            raise RuntimeError('RELEASE requires live HELD owner')
        self._send('RELEASE')
        summary = self._wait('summary', 15)
        code = self.process.wait(timeout=5)
        okay = (code == 0 and summary['status'] == 'PASS' and
                summary['reason'] == 'released' and
                summary['remaining_mappings'] == 0 and
                summary['final_identity']['vmlck_bytes'] == 0)
        self._finish(code, okay)
        if not okay or not self.exit_record['release_success']:
            raise RuntimeError('helper release/cleanup or persistence failed')
        return self.exit_record

    def _finish(self, code, released):
        self.exit_record = dict(returncode=code, reaped=code is not None,
                                release_success=released,
                                logging_errors=list(self._logging_errors),
                                recovery_required=code is None,
                                summary=self.summary,
                                finished_mono=time.monotonic())
        # The exit log itself and closing buffered streams can fail. Freeze
        # the authoritative acceptance only after all of these operations.
        self._record('exit', **self.exit_record)
        self._selector.close()
        if self._helper_pidfd is not None:
            os.close(self._helper_pidfd)
            self._helper_pidfd = None
        for stream in (self.process.stdin, self.process.stdout,
                       self._stderr, self._log):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except (OSError, ValueError) as error:
                    self._logging_errors.append(repr(error))
        self.exit_record['logging_errors'] = list(self._logging_errors)
        self.exit_record['release_success'] = released and not self._logging_errors
        try:
            save(self.output / 'exit.json', self.exit_record)
        except (OSError, ValueError) as error:
            self._logging_errors.append(repr(error))
            self.exit_record['logging_errors'] = list(self._logging_errors)
            self.exit_record['release_success'] = False

    def _stop(self, sig):
        # A pidfd pins the identity of the actual same-uid mapper, even when
        # it is a grandchild supervised by sudo. Never target a bare PID.
        if self._helper_pidfd is not None:
            try:
                signal.pidfd_send_signal(self._helper_pidfd, sig)
            except ProcessLookupError:
                pass
        else:
            # Before READY only the owned launcher is known. Popen retains
            # its child identity; refusal to signal root is reported, not
            # worked around by privileged cross-PID mutation.
            try:
                self.process.send_signal(sig)
            except PermissionError as error:
                self._record('launcher_signal_refused', error=repr(error))

    def close(self):
        """Bounded cleanup; persistence failure cannot suppress termination."""
        if self.exit_record is not None:
            return self.exit_record
        self._record('abort_begin')
        self._stop(signal.SIGTERM)
        try:
            tail, _ = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self._record('kill_after_cleanup_timeout')
            self._stop(signal.SIGKILL)
            try:
                tail, _ = self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self._finish(None, False)
                raise RuntimeError('owned launch tree needs recovery')
        for line in (self._buffer + tail).splitlines():
            try:
                self._consume(line)
            except (ValueError, RuntimeError) as error:
                self._record('event_parse_error', error=repr(error))
        self._buffer = b''
        self._finish(self.process.returncode, False)
        return self.exit_record

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def helper_main(output, owner_pid, owner_start_ticks, timeout=180):
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError('invalid helper timeout')
    out = Path(output)
    log = (out / 'child-events.jsonl').open('x')
    seq = 0
    pipe_alive = True
    mappings = []
    errors = []
    reason = 'failed'
    held = False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mmap.restype = ctypes.c_void_p
    for name in ('mlock', 'munlock', 'munmap'):
        getattr(libc, name).argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        getattr(libc, name).restype = ctypes.c_int
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                            ctypes.POINTER(ctypes.c_ubyte)]
    libc.mincore.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int

    def emit(event, **fields):
        nonlocal seq, pipe_alive
        row = dict(seq=seq, event=event, mono=time.monotonic(),
                   wall=time.time(), pid=os.getpid(), **fields)
        seq += 1
        line = json.dumps(row) + '\n'
        log.write(line)
        log.flush()
        if pipe_alive:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except BrokenPipeError:
                pipe_alive = False
                # Avoid a second BrokenPipe during interpreter final flush.
                # The independent child log remains the authoritative record.
                null_fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(null_fd, sys.stdout.fileno())
                finally:
                    os.close(null_fd)
        return row

    def term(signum, _):
        raise InterruptedError('signal ' + str(signum))

    def checked(name, pointer, length):
        ctypes.set_errno(0)
        rc = getattr(libc, name)(pointer, length)
        err = ctypes.get_errno() if rc else 0
        emit(name, ptr=hex(pointer), bytes=length, rc=rc, errno=err)
        if rc:
            raise OSError(err, name + ': ' + os.strerror(err))

    try:
        signal.signal(signal.SIGTERM, term)
        signal.signal(signal.SIGINT, term)
        signal.signal(signal.SIGALRM, term)
        signal.setitimer(signal.ITIMER_REAL, min(timeout, 30))
        direct_parent = identity(os.getppid())
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0):  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG')
        if os.getppid() != direct_parent['pid']:
            raise RuntimeError('direct parent exited before death-signal setup')
        owner = identity(owner_pid)
        if owner['start_ticks'] != owner_start_ticks:
            raise RuntimeError('original controlling owner changed')
        manifest = json.loads((out / 'manifest.json').read_text())
        expected = make_manifest([r['path'] for r in manifest['files']],
                                 fixture=manifest['fixture'])
        if manifest != expected:
            raise RuntimeError('manifest changed or has unsupported fields')
        initial = identity(os.getpid())
        if (initial['vmlck_bytes'] != 0 or initial['uid'] != owner['uid'] or
                os.getuid() == 0 or any(int(initial['capabilities'][k], 16)
                    for k in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb'))):
            raise RuntimeError('initial lock/uid/capabilities outside contract')
        emit('ready', identity=initial, owner=owner,
             direct_parent=direct_parent, manifest_sha256=hashlib.sha256(
             (out / 'manifest.json').read_bytes()).hexdigest())
        request = sys.stdin.readline()
        if not request.startswith('LIMIT '):
            raise EOFError('LIMIT absent or control EOF')
        target = int(request.split()[1])
        if not (0 <= target <= manifest['total_lock_bytes']) or (
                not manifest['fixture'] and target != manifest['total_lock_bytes']):
            raise ValueError('requested limit outside manifest')
        resource.setrlimit(resource.RLIMIT_MEMLOCK, (target, target))
        emit('granted', identity=identity(os.getpid()))
        if sys.stdin.readline() != 'START\n':
            raise EOFError('START absent or control EOF')
        signal.setitimer(signal.ITIMER_REAL, timeout)
        emit('start', identity=identity(os.getpid()))
        for index, row in enumerate(manifest['files']):
            fd = os.open(row['path'], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                st = os.fstat(fd)
                if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (
                        row['dev'], row['inode'], row['size'], row['mtime_ns']):
                    raise RuntimeError('opened file identity differs')
                ctypes.set_errno(0)
                ptr = libc.mmap(None, row['size'], mmap.PROT_READ,
                                mmap.MAP_SHARED, fd, 0)
                if ptr == ctypes.c_void_p(-1).value:
                    raise OSError(ctypes.get_errno(), 'mmap')
                mappings.append((ptr, row['size'], index))
                emit('mmap', index=index, ptr=hex(ptr), bytes=row['size'],
                     lock_bytes=row['lock_bytes'], path=row['path'], rc=0)
                checked('mlock', ptr, row['size'])
                vector = (ctypes.c_ubyte * (row['lock_bytes'] // PAGE))()
                rc = libc.mincore(ptr, row['size'], vector)
                resident = sum(bool(v & 1) for v in vector) if rc == 0 else None
                emit('mincore', index=index, ptr=hex(ptr), rc=rc,
                     pages=len(vector), resident_pages=resident)
                if rc or resident != len(vector):
                    raise RuntimeError('locked mapping not fully resident')
            finally:
                os.close(fd)
        now = identity(os.getpid())
        if now['vmlck_bytes'] != manifest['total_lock_bytes']:
            raise RuntimeError('aggregate VmLck differs')
        held = True
        signal.setitimer(signal.ITIMER_REAL, timeout)
        emit('held', identity=now, files=len(mappings),
             locked_bytes=manifest['total_lock_bytes'])
        if sys.stdin.readline() != 'RELEASE\n':
            raise EOFError('RELEASE absent or control EOF')
        reason = 'released'
    except BaseException as error:
        errors.append(repr(error))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        remaining = len(mappings)
        for ptr, length, index in reversed(mappings):
            # Even failed mlock may have changed a VMA: unmap every mapping.
            for name in ('munlock', 'munmap'):
                try:
                    checked(name, ptr, length)
                    if name == 'munmap':
                        remaining -= 1
                except BaseException as error:
                    errors.append(repr(error))
        final = identity(os.getpid())
        okay = (held and reason == 'released' and not errors and
                remaining == 0 and final['vmlck_bytes'] == 0)
        emit('summary', status='PASS' if okay else 'FAILED', reason=reason,
             errors=errors, held=held, remaining_mappings=remaining,
             final_identity=final)
        log.close()
    return 0 if okay else 125


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', type=Path, required=True)
    parser.add_argument('--owner-pid', type=int, required=True)
    parser.add_argument('--owner-start-ticks', type=int, required=True)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    raise SystemExit(helper_main(args.helper, args.owner_pid,
                                 args.owner_start_ticks, args.timeout))
