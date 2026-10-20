"""One explicitly selected node compaction; no persistent kernel setting."""
import argparse
import json
import os
from pathlib import Path
import time


def snapshot():
    return dict(monotonic=time.monotonic(), wall=time.time(),
                files={str(p): p.read_text() for p in map(Path, (
                    '/proc/meminfo', '/proc/vmstat', '/proc/buddyinfo',
                    '/sys/devices/system/node/online',
                    '/sys/devices/system/node/has_memory'))})


def main(output):
    if os.geteuid() != 0:
        raise RuntimeError('node compaction requires root')
    output.mkdir(parents=True, exist_ok=False)
    before = snapshot()
    if before['files']['/sys/devices/system/node/online'].strip() != '0':
        raise RuntimeError('unexpected NUMA topology')
    (output / 'before.json').write_text(json.dumps(before, indent=2) + '\n')
    state = dict(status='RUNNING', pid=os.getpid(), before=before['monotonic'],
                 operation='/sys/devices/system/node/node0/compact', writes=1,
                 scope='one memory node on this machine: all physical RAM',
                 persistent_configuration_changed=False,
                 exact_physical_layout_rollback_possible=False)
    target = output / 'state.json'
    target.write_text(json.dumps(state, indent=2) + '\n')
    try:
        with Path(state['operation']).open('w') as stream:
            if stream.write('1\n') != 2:
                raise OSError('short node compaction write')
        state.update(status='COMPLETED', finished_monotonic=time.monotonic())
    except BaseException as error:
        state.update(status='FAILED', error=repr(error))
        raise
    finally:
        after = snapshot()
        (output / 'after.json').write_text(json.dumps(after, indent=2) + '\n')
        state['finished'] = after['monotonic']
        target.write_text(json.dumps(state, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args().output)
