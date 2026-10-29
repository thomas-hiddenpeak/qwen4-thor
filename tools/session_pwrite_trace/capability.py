"""One small CPU attachment gate. Failure stops before any model startup."""
import ctypes as C
import json
import mmap
import os
from pathlib import Path
import sys
import time


def main():
    obj_path, output = map(Path, sys.argv[1:])
    output.mkdir()
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
    links, obj = [], None
    result = dict(started=time.time(), status='TOOL_UNAVAILABLE',
                  fixture_writes=0, model_loads=0, http_requests=0)
    try:
        obj = opened(os.fsencode(obj_path), None)
        if not obj or error(obj):
            raise RuntimeError('open BPF object: '+str(error(obj)))
        rc = load(obj)
        if rc:
            raise RuntimeError('load BPF object: '+str(rc))
        program = None
        while True:
            program = next_program(obj, program)
            if not program:
                break
            link = attach(program)
            if not link or error(link):
                raise RuntimeError('attach: '+str(error(link)))
            links.append(link)
        result['attached_programs'] = len(links)
        key, pid = C.c_uint(0), C.c_uint(os.getpid())
        fd = map_fd(find_map(obj, b'target'))
        if map_update(fd, C.byref(key), C.byref(pid), 0):
            raise RuntimeError('target map update')
        buffer = mmap.mmap(-1, 8*1024**2)
        buffer[:] = b'\x5a' * len(buffer)
        handles = []
        try:
            for _ in range(2):
                handle = os.open(output, os.O_TMPFILE | os.O_DIRECT | os.O_RDWR, 0o600)
                handles.append(handle)
                for length, offset in [(4096, 0), (128*1024, 4096),
                                       (8*1024**2, 135168)]:
                    view = memoryview(buffer)[:length]
                    try:
                        wrote = os.pwrite(handle, view, offset)
                    finally:
                        view.release()
                    result['fixture_writes'] += wrote
                    if wrote != length:
                        raise RuntimeError('short direct write')
        finally:
            for handle in handles:
                os.close(handle)
            buffer.close()
        values = (C.c_ulonglong*13)()
        rc = map_lookup(map_fd(find_map(obj, b'counters')),
                        C.byref(key), C.byref(values))
        if rc:
            raise RuntimeError('counter lookup')
        names = ['calls','sleeps','stack_ok','stack_fail','blocks','completes',
                 'bytes','bad_device','raw_nvme','nvme','fs_enter','fs_exit',
                 'last_stack_error']
        result['counters'] = dict(zip(names, values))
        if not values[2] or not values[4] or not values[10]:
            raise RuntimeError('required stack/block/ext4 observation absent')
        result['status'] = 'CAPABILITY_ONLY_REQUIRES_FULL_CONTRACT'
    except Exception as exc:
        result['error'] = repr(exc)
    finally:
        for link in reversed(links):
            destroy(link)
        if obj and not error(obj):
            close(obj)
        result.update(finished=time.time(), detached_links=len(links))
        (output/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result))
    return 0 if result['status'].startswith('CAPABILITY_ONLY') else 2


if __name__ == '__main__':
    sys.exit(main())
