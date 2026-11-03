"""Fixed-size Thor allocation observations, without a physical-limit inference."""
import os
from pathlib import Path
import time

from tools.serve_startup_memory.cpu import h, a, ROOT
from tools.serve_lifecycle import observe
from tools.serve_pressure.pressure import memory
from isolated_scope import IsolatedScope

KINDS = ('anonymous', 'device', 'pinned', 'managed')
BYTES = 256 * 1024**2


def receipt(value, kind, phase):
    h.require(value['kind'] == kind and value['phase'] == phase and
              value['bytes'] == BYTES, 'probe identity or fixed allocation changed')
    h.require(type(value['cuda_free_bytes']) is int and
              type(value['cuda_total_bytes']) is int and
              0 <= value['cuda_free_bytes'] <= value['cuda_total_bytes'] and
              value['cuda_total_bytes'] > 0, 'invalid CUDA observation')
    expected = ('context_initialized' if phase == 'baseline' else
                'release_returned_success' if phase == 'freed' else
                'gpu_all_words_verified_edges' if phase == 'held' and
                kind in ('managed', 'device') else 'cpu_all_words_verified_edges')
    h.require(value['touch'] == expected, 'probe data was not committed/checked')


def values(row, native):
    counters = row['resources']['cgroup']['observations']
    result = dict(parent_rss=row['parent']['VmRSS'],
        cgroup_current=counters['memory.current']['value'],
        cgroup_anon=counters['memory.stat']['value']['anon'],
        cgroup_file=counters['memory.stat']['value']['file'],
        global_available=row['memory']['MemAvailable'],
        cuda_free=native['cuda_free_bytes'])
    h.require(all(type(v) is int and v >= 0 for v in result.values()),
              'missing/invalid allocation counter')
    return result


def summarize(phases):
    # Pair each of the two observations with its corresponding baseline. These
    # are overlapping domains; deliberately no aggregate RAM or coverage score.
    base = [values(row, phases['baseline']['native'])
            for row in phases['baseline']['observations']]
    h.require(len(base) == 2, 'fixed baseline sample count')
    result = {}
    for phase, record in phases.items():
        h.require(len(record['observations']) == 2, 'fixed phase sample count')
        deltas = []
        for before, row in zip(base, record['observations']):
            after = values(row, record['native'])
            deltas.append({k:after[k]-before[k] for k in before})
        result[phase] = dict(delta_from_baseline=deltas,
            sign='positive means counter increased; free/available decrease on allocation')
    return result


def run(plan, output, budget):
    output.mkdir()
    results = {}
    for kind in KINDS:
        budget('probes')
        a.require_idle_gpu(output/(kind+'-gpu-before.json'))
        initial = memory()
        if initial['MemAvailable'] < 16*1024**3:
            raise a.EnvironmentAbort('probe precondition: less than 16GiB available')
        folder = output/kind
        folder.mkdir()
        receipts = folder/'receipts'
        receipts.mkdir()
        binary = plan['memory_probe']
        h.check_identity(binary)
        env = {k:v for k,v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        scope = None
        phases = {}
        result = dict(passed=False, initial_memory=initial, fixed_bytes=BYTES)
        try:
            scope = IsolatedScope([binary['path'], kind, str(receipts)],
                cwd=ROOT, env=env, log_path=folder/'probe.log',
                unit=f'q4t-startup-account-{os.getpid()}-{kind}.scope',
                memory_max=2*1024**3, evidence=folder/'scope')
            start = h.read(folder/'scope/identity.json')['start_ticks']
            for phase in ('baseline', 'host', 'held', 'freed') if kind == 'managed' else ('baseline', 'held', 'freed'):
                until = time.monotonic()+20
                path = receipts/(phase+'.json')
                while not path.exists():
                    budget()
                    h.require(scope.poll() is None and time.monotonic() < until,
                              'probe failed or checkpoint timed out: '+phase)
                    time.sleep(.02)
                native = h.read(path)
                receipt(native, kind, phase)
                rows = []
                for index in range(2):
                    rows.append(observe.sample(scope, start))
                    if index == 0: time.sleep(.25)
                phases[phase] = dict(native=native, observations=rows)
                h.save(folder/(phase+'-observations.json'), phases[phase])
                h.save(receipts/(phase+'.continue'), dict(observed=True))
            h.require(scope.wait(timeout=10) == 0, 'probe did not exit successfully')
            final = h.read(receipts/'result.json')
            h.require(final == dict(passed=True, bytes=BYTES, allocations=1, releases=1),
                      'probe allocation/release result')
            device = h.read(receipts/'device.json')
            h.require(device['runtime'] == 13030 and device['major'] == 11 and
                      device['minor'] == 0 and device['integrated'] == 1,
                      'not the frozen CUDA13.3 Thor environment')
            end = scope.snapshot('payload_exited')
            observe.prior.check_oom(end['resources']['cgroup']['observations']['memory.events']['value'])
            result.update(passed=True, device=device, deltas=summarize(phases),
                physical_ram_hard_limit_proved=False, exact_physical_bytes_proved=False)
        finally:
            try:
                if scope is not None: scope.close()
                a.require_idle_gpu(folder/'gpu-after.json')
            except BaseException as error:
                result.update(passed=False, cleanup_error=repr(error))
                raise
            finally:
                h.save(folder/'result.json', result)
        results[kind] = result
    h.save(output/'result.json', dict(passed=all(v['passed'] for v in results.values()),
        kinds=results, allocation_bytes_each=BYTES, samples_each_phase=2,
        concurrent_allocations=1, no_model_concurrent=True,
        cgroup_max_is_only_controller_limit=True, physical_ram_hard_limit_proved=False))
