"""Exactly three queries around one minimal CUDA context; use GPU guard."""
import json
from pathlib import Path
import selectors
import subprocess
import time

from nvml_process_query import query

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / '.q4t-work/nvml-monitor-20261020'
OUT = STAGE / 'gpu-v1'


def main():
    state = dict(status='RUNNING', queries=0, model_loads=0, cuda_alloc_bytes=4)
    fixture = None
    try:
        def sample(name):
            result = query(5)
            state['queries'] += 1
            (OUT / (name + '.json')).write_text(json.dumps(result, indent=2) + '\n')
            if result['status'] != 'OK':
                raise RuntimeError(name + ': ' + result['status'])
            return set(result['pids'])

        sample('before')
        with (OUT / 'fixture.stderr').open('w') as stderr:
            fixture = subprocess.Popen([str(STAGE / 'context-fixture')],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=stderr, text=True)
            state['fixture_pid'] = fixture.pid
            with selectors.DefaultSelector() as selector:
                selector.register(fixture.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=10):
                    raise RuntimeError('fixture readiness timeout')
                if fixture.stdout.readline().strip() != 'READY':
                    raise RuntimeError('fixture not ready')
            if fixture.pid not in sample('active'):
                raise RuntimeError('known compute PID not detected')
            fixture.stdin.write('q')
            fixture.stdin.flush()
            if fixture.wait(timeout=5) != 0:
                raise RuntimeError('fixture exit failure')
            if fixture.pid in sample('after'):
                raise RuntimeError('exited compute PID still reported')
        state['status'] = 'PASS_MINIMAL_CONTEXT_ONLY'
    except BaseException as error:
        state.update(status='FAIL', error=repr(error))
        raise
    finally:
        if fixture is not None:
            if fixture.poll() is None:
                fixture.kill()
                try:
                    fixture.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    state.update(status='CLEANUP_FAILED')
            state['fixture_exit'] = fixture.returncode
            fixture.stdin.close()
            fixture.stdout.close()
        state['finished'] = time.time()
        (OUT / 'result.json').write_text(json.dumps(state, indent=2) + '\n')


if __name__ == '__main__':
    main()
