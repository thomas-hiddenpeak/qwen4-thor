"""Own BPF links, bound buffers and drop root before ongoing observation."""
import ctypes as C
import json
import os
from pathlib import Path
import signal
import sys
import time


def main():
    program, out = map(Path, sys.argv[1:3])
    deadline = float(sys.argv[3])
    lib = C.CDLL('libbpf.so.1', use_errno=True)
    def api(name, args, result):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
        return fn
    ptr, integer = C.c_void_p, C.c_int
    opened = api('bpf_object__open_file', [C.c_char_p, ptr], ptr)
    error = api('libbpf_get_error', [ptr], C.c_long)
    load = api('bpf_object__load', [ptr], integer)
    next_program = api('bpf_object__next_program', [ptr, ptr], ptr)
    attach = api('bpf_program__attach', [ptr], ptr)
    destroy = api('bpf_link__destroy', [ptr], integer)
    close = api('bpf_object__close', [ptr], None)
    find_map = api('bpf_object__find_map_by_name', [ptr, C.c_char_p], ptr)
    map_fd = api('bpf_map__fd', [ptr], integer)
    map_update = api('bpf_map_update_elem', [integer, ptr, ptr, C.c_ulonglong], integer)
    map_lookup = api('bpf_map_lookup_elem', [integer, ptr, ptr], integer)
    callback_type = C.CFUNCTYPE(integer, ptr, ptr, C.c_size_t)
    ring_new = api('ring_buffer__new', [integer, callback_type, ptr, ptr], ptr)
    ring_poll = api('ring_buffer__poll', [ptr, integer], integer)
    ring_free = api('ring_buffer__free', [ptr], None)
    links, obj, ring = [], None, None
    pending = bytearray()
    stopped = False
    emitted = 0
    def handle_signal(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    @callback_type
    def receive(ctx, data, size):
        nonlocal stopped, emitted
        if size != 128 or len(pending)+size > 160*1024**2:
            stopped = True
            return -1
        pending.extend(C.string_at(data, size))
        emitted += 1
        return 0
    result = dict(started=time.time(), status='FAILED', events=0)
    try:
        obj = opened(os.fsencode(program), None)
        if not obj or error(obj): raise RuntimeError('BPF open')
        if load(obj): raise RuntimeError('BPF load')
        p = None
        while True:
            p = next_program(obj, p)
            if not p: break
            link = attach(p)
            if not link or error(link): raise RuntimeError('BPF attach')
            links.append(link)
        target_fd = map_fd(find_map(obj, b'target'))
        count_fd = map_fd(find_map(obj, b'counters'))
        stack_fd = map_fd(find_map(obj, b'stacks'))
        ring = ring_new(map_fd(find_map(obj, b'events')), receive, None, None)
        if not ring: raise RuntimeError('ring buffer')
        symbols = Path('/proc/kallsyms').read_bytes()
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(int(os.environ['SUDO_GID']))
            os.setuid(int(os.environ['SUDO_UID']))
            if C.CDLL(None).prctl(4, 1, 0, 0, 0):
                raise RuntimeError('PR_SET_DUMPABLE')
        (out/'kallsyms.txt').write_bytes(symbols)
        del symbols
        (out/'ready.json').write_text(json.dumps(dict(pid=os.getpid(),
            uid=os.getuid(), attached=len(links)))+'\n')
        z, previous = C.c_uint(0), -1
        counts = (C.c_ulonglong*5)()
        idle = time.monotonic()
        with (out/'events.bin').open('xb', buffering=0) as stream:
            while not stopped:
                if time.time() >= deadline: raise TimeoutError('collector deadline')
                if (out/'stop').exists(): break
                pid_path = out/'target.json'
                if pid_path.exists():
                    target = json.loads(pid_path.read_text())['pid']
                    if target != previous:
                        value = C.c_uint(target)
                        if map_update(target_fd, C.byref(z), C.byref(value), 0):
                            raise RuntimeError('target update')
                        previous = target
                        (out/'target-ready.json').write_text(json.dumps({'pid':target})+'\n')
                rc = ring_poll(ring, 10)
                if rc < 0 and rc != -4: raise RuntimeError('ring poll '+str(rc))
                if map_lookup(count_fd, C.byref(z), C.byref(counts)):
                    raise RuntimeError('counter read')
                if counts[0]: idle = time.monotonic()
                if counts[2] or counts[3] or counts[4]:
                    raise RuntimeError('lost/map/stack error '+str(list(counts)))
                if emitted > 10000000: raise RuntimeError('event budget')
                if pending and time.monotonic()-idle > .1:
                    stream.write(pending); pending.clear()
            value = C.c_uint(0)
            map_update(target_fd, C.byref(z), C.byref(value), 0)
            for _ in range(5): ring_poll(ring, 10)
            stream.write(pending); pending.clear()
        map_lookup(count_fd, C.byref(z), C.byref(counts))
        stack_rows = {}
        addresses = (C.c_ulonglong*32)()
        for i in range(4096):
            key = C.c_uint(i)
            if map_lookup(stack_fd, C.byref(key), C.byref(addresses)) == 0:
                stack_rows[str(i)] = [v for v in addresses if v]
        (out/'stacks.json').write_text(json.dumps(stack_rows)+'\n')
        result.update(status='COLLECTED_REQUIRES_AUDIT', counters=list(counts),
                      events=emitted, stack_ids=len(stack_rows))
    except BaseException as exc:
        result['error'] = repr(exc)
    finally:
        if ring: ring_free(ring)
        for link in reversed(links): destroy(link)
        if obj and not error(obj): close(obj)
        result.update(finished=time.time(), detached_links=len(links))
        (out/'exit.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result))
    return 0 if result['status'] == 'COLLECTED_REQUIRES_AUDIT' else 2


if __name__ == '__main__':
    sys.exit(main())
