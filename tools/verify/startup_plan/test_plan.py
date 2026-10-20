#!/usr/bin/env python3
"""Bounded host-only plan checks against real, read-only tensor metadata.

Plan generation is not GPU configuration acceptance. The historical allocation
trace is used only as an independent regression oracle by this test tool.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/startup-plan-20261020'
MODEL = (Path.home() / 'models/dev/llm/garnermccloud/'
         'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
HISTORICAL = ROOT / '.q4t-work/device-reserve-20261020/plan/plan.json'
EXPERT_FIELDS = ('gu_packed', 'gu_sf', 'dn_packed', 'dn_sf', 'scale_block')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, data):
    with path.open('x') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')


def allocations(plan):
    return {entry['name']: entry['bytes'] for entry in plan['allocations']}


class Runner:
    def __init__(self, binary, model, output, timeout):
        self.binary = binary
        self.model = model
        self.output = output
        self.timeout = timeout
        self.results = {}
        self.logs = {}

    def case(self, name, *, expect=True, mtp=False, prefill=8192, length=262144,
             slots=256, extra=(), env_extra=None, reason=None):
        directory = self.output / name
        directory.mkdir(exist_ok=False)
        path = directory / 'plan.json'
        command = [str(self.binary), 'serve', '--model-dir', str(self.model),
                   '--max-seq', '1', '--max-len', str(length),
                   '--moe-resident-slots', str(slots)]
        if prefill is not None:
            command += ['--max-prefill', str(prefill)]
        if not mtp:
            command += ['--no-mtp']
        command += list(extra)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}
        settings = {'Q4T_MOE_LOAD_THREADS': '16', 'Q4T_MOE_L2_SLOTS': '16',
                    'Q4T_MOE_MIRROR_K': '8', 'Q4T_MOE_MAX_OPEN_SHARDS': '200'}
        settings.update(env_extra or {})
        settings.update(Q4T_STARTUP_PLAN_ONLY='1',
                        Q4T_STARTUP_PLAN_OUTPUT=str(path))
        environment.update(settings)
        save(directory / 'command.json', command)
        save(directory / 'environment.json', settings)
        started = time.monotonic()
        completed = None
        timed_out = False
        with (directory / 'stdout.log').open('x') as stdout:
            with (directory / 'stderr.log').open('x') as stderr:
                try:
                    completed = subprocess.run(
                        command, env=environment, stdout=stdout, stderr=stderr,
                        timeout=self.timeout, check=False)
                except subprocess.TimeoutExpired:
                    timed_out = True
        log = ((directory / 'stdout.log').read_text() +
               (directory / 'stderr.log').read_text())
        self.logs[name] = log
        exit_record = {'returncode': completed.returncode if completed else None,
                       'timeout': timed_out,
                       'elapsed_seconds': time.monotonic() - started}
        save(directory / 'exit.json', exit_record)
        require(not timed_out, name + ': metadata timeout')
        require('[memory-trace]' not in log and '[reserve-trace]' not in log,
                name + ': CUDA operation or reservation trace in host test')
        require('serving on port' not in log, name + ': unexpectedly served')
        if not expect:
            require(completed.returncode > 0, name + ': rejection was not clean')
            require(not path.exists(), name + ': rejected request emitted plan')
            require('[startup-plan]' not in log,
                    name + ': rejected request configured a reservation')
            if reason:
                require(reason in log, name + ': wrong rejection reason')
            result = {'status': 'PASS', 'expected': 'REJECTED', **exit_record,
                      'observed_cuda_operation_logs': 0}
            self.results[name] = result
            save(directory / 'result.json', result)
            return None
        require(completed.returncode == 0 and path.is_file(),
                name + ': plan generation failed')
        plan = json.loads(path.read_text())
        require(plan['schema'] == 1 and
                plan['derivation'] == 'configuration_tensor_metadata',
                name + ': unexpected plan derivation')
        entries = plan['allocations']
        require(plan['count'] == len(entries) and len(allocations(plan)) ==
                len(entries), name + ': count or names are not unique')
        require(all(isinstance(entry['bytes'], int) and entry['bytes'] > 0
                    for entry in entries), name + ': invalid byte count')
        require(sum(entry['bytes'] for entry in entries) == plan['device_bytes'],
                name + ': total byte count differs')
        records = [json.loads(line.split(' ', 1)[1]) for line in log.splitlines()
                   if line.startswith('[startup-plan] ')]
        require(len(records) == 1 and records[0]['event'] == 'configured',
                name + ': missing/duplicate configured record')
        require(records[0]['plan_sha256'] == sha(path) and
                records[0]['effective'] == plan['effective'] and
                records[0]['count'] == plan['count'] and
                records[0]['device_bytes'] == plan['device_bytes'],
                name + ': log and output identity differ')
        result = {'status': 'PASS', 'expected': 'PLAN_GENERATED', **exit_record,
                  'count': plan['count'], 'device_bytes': plan['device_bytes'],
                  'effective': plan['effective'], 'plan_sha256': sha(path),
                  'observed_cuda_operation_logs': 0,
                  'gpu_configuration_accepted': False}
        self.results[name] = result
        save(directory / 'result.json', result)
        return plan


def checks(runner, historical):
    base = runner.case('baseline_no_mtp')
    require(base['count'] == 1397, 'baseline count must be 1397')
    require(base['effective']['max_len'] == 262144 and
            base['effective']['max_prefill'] == 8192 and
            base['effective']['max_seq'] == 1, 'baseline capacity was capped')
    old = json.loads(historical.read_text())
    sizes = [entry['bytes'] for entry in base['allocations']]
    old_sizes = [entry['bytes'] for entry in old['startup_device_allocations']]
    require(sizes == old_sizes, 'shape-derived plan differs from captured sizes')
    by_name = allocations(base)
    mtp = runner.case('default_mtp_k3', mtp=True)
    require(mtp['count'] == 1462 and mtp['effective']['mtp_k'] == 3 and
            mtp['effective']['initial_scratch_capacity'] == 4,
            'default MTP count/capacity')
    mtp_sizes = allocations(mtp)
    for k in (1, 16):
        plan = runner.case(f'mtp_k{k}', mtp=True, extra=('--mtp-k', str(k)))
        require(plan['count'] == 1462 and plan['effective']['mtp_k'] == k and
                plan['effective']['initial_scratch_capacity'] == k + 1,
                f'MTP k={k} count/capacity')
        current = allocations(plan)
        require(current['mtp.scratch.ids'] == (k + 1) * 4,
                'MTP scratch must reserve k+1 rows')
        for name in ('model.verify.ssm', 'model.verify.conv',
                     'model.verify.ple_conv'):
            require(current[name] * 3 == mtp_sizes[name] * k,
                    f'MTP checkpoint must reserve k rows: {name}')
    pages = {length: runner.case(f'page_len_{length}', length=length,
                                 extra=('--no-budget',))
             for length in (15, 16, 17)}
    for length, plan in pages.items():
        values = allocations(plan)
        for layer in range(3, 48, 4):
            prefix = f'model.language_model.layers.{layer}.state.'
            require(values[prefix + 'kv_cache'] ==
                    ((length + 15) // 16) * 16 * 2 * 2 * 256 * 2,
                    'KV page rounding differs')
            require(values[prefix + 'page_table'] == length * 4 and
                    values[prefix + 'idx_raw'] == length * 128 * 2 and
                    values[prefix + 'idx_comp'] == length * 128 * 2,
                    'non-paged state must use exact length')
        require(values['model.d_rope_pos'] == 3 * length * 4, 'rope length')
    require(allocations(pages[15])[
        'model.language_model.layers.3.state.kv_cache'] == allocations(pages[16])[
            'model.language_model.layers.3.state.kv_cache'], '15/16 KV equality')
    default_prefill = runner.case('default_prefill', prefill=None)
    require(default_prefill['effective']['max_prefill'] == 2048 and
            allocations(default_prefill)['model.d_trunk'] * 4 ==
            by_name['model.d_trunk'], 'default prefill shape')
    runner.case('prefill_8193_rejected', prefill=8193, expect=False,
                reason='max-prefill must be in [0,8192]')
    runner.case('mtp_k16_prefill4_rejected', mtp=True, prefill=4,
                extra=('--mtp-k', '16'), expect=False,
                reason='unsupported MTP capacity')
    resident = {}
    for slots in (0, 512):
        resident[slots] = runner.case(f'resident_{slots}', slots=slots,
                                      length=8192, prefill=2048,
                                      extra=('--no-budget',))
    require(resident[0]['count'] == 1541 and resident[512]['count'] == 1397,
            'full resident uses four scale blocks; slot mode uses one')
    require(resident[0]['device_bytes'] == resident[512]['device_bytes'],
            'C512 and full resident payload totals differ')
    hot = runner.output / 'nonuniform-hot.json'
    save(hot, {'0': list(range(10)), '1': list(range(32)), '2': []})
    nonuniform = runner.case('nonuniform_hot',
                             extra=('--moe-hot-list', str(hot)))
    values = allocations(nonuniform)
    require(nonuniform['count'] == 1397, 'nonuniform changed block count')
    for layer in range(48):
        count = 10 if layer == 0 else 32 if layer == 1 else 256
        for field in EXPERT_FIELDS:
            name = f'model.language_model.layers.{layer}.mlp.{field}'
            require(values[name] * 256 == by_name[name] * count,
                    'nonuniform/missing/empty layer semantics: ' + name)
    floor = runner.case('host_l2_floor', env_extra={'Q4T_MOE_L2_SLOTS': '1'})
    require(floor['effective']['l2_slots'] == 16 and
            floor['effective']['load_threads'] == 16 and
            allocations(floor) == by_name, 'host L2 floor alters device plan')
    minimal = runner.case('host_minimal', env_extra={
        'Q4T_MOE_LOAD_THREADS': '1', 'Q4T_MOE_L2_SLOTS': '1',
        'Q4T_MOE_MIRROR_K': '0'})
    require(minimal['effective']['l2_slots'] == 1 and
            minimal['effective']['load_threads'] == 1 and
            minimal['effective']['mirror_k'] == 0 and
            allocations(minimal) == by_name, 'minimal host cache normalization')
    # No-budget here inspects normalization only. 512 pinned L2 slots are not
    # claimed to fit on the device, and this case performs no actual allocation.
    capped = runner.case('host_normalization_caps', extra=('--no-budget',),
                          env_extra={'Q4T_MOE_LOAD_THREADS': '999',
                                     'Q4T_MOE_L2_SLOTS': '999',
                                     'Q4T_MOE_MIRROR_K': '999'})
    require(capped['effective']['load_threads'] == 16 and
            capped['effective']['l2_slots'] == 512 and
            capped['effective']['mirror_k'] == 32 and
            allocations(capped) == by_name, 'host cache upper normalization')
    log = runner.logs['baseline_no_mtp']
    fixed = re.search(r'weights=[\d.]+ GB\s+fixed=([\d.]+) GB', log)
    variable = re.search(r'per_request=([\d.]+) GB.*state_pool=([\d.]+) GB', log)
    require(fixed and variable, 'baseline budget report missing')
    meminfo = Path('/proc/meminfo').read_text()
    total = int(re.search(r'^MemTotal:\s+(\d+) kB$', meminfo, re.M)[1]) * 1024
    target = (float(fixed[1]) +
              0.5 * (float(variable[1]) + float(variable[2]))) * 1e9
    fraction = target / total
    require(0 < fraction < 0.9, 'invalid single bounded budget selection')
    save(runner.output / 'budget-selection.json', {
        'method': 'baseline fixed + half of per-request and state estimate',
        'report_fixed_gb': float(fixed[1]),
        'report_per_request_gb': float(variable[1]),
        'report_state_pool_gb': float(variable[2]), 'mem_total': total,
        'mem_fraction': fraction, 'attempt_budget': 1})
    reduced = runner.case('budget_reduced',
                           extra=('--mem-fraction', format(fraction, '.12f')))
    require(0 < reduced['effective']['max_len'] < 262144 and
            reduced['effective']['max_seq'] == 1 and
            reduced['device_bytes'] < base['device_bytes'],
            'effective budget capacity did not shrink')
    require(allocations(reduced)['model.d_rope_pos'] ==
            reduced['effective']['max_len'] * 3 * 4,
            'plan used requested capacity instead of effective capacity')
    for env in ('Q4T_FP8_PROJ', 'Q4T_FP8_ATTN', 'Q4T_FP8_GDN',
                'Q4T_FP8_LMHEAD', 'Q4T_FP8_SHARED', 'Q4T_FP8_HC'):
        runner.case('reject_' + env.lower(), env_extra={env: '1'},
                    expect=False, reason='FP8')
    runner.case('media_rejected', extra=('--allow-media',), expect=False,
                reason='media and router capture unsupported')
    runner.case('router_capture_rejected', extra=(
        '--moe-trace-dir', str(runner.output / 'unused-router'),
        '--moe-trace-workload', 'host-plan-contract'), expect=False,
        reason='media and router capture unsupported')
    runner.case('oversized_length_rejected', length=2147483647,
                extra=('--no-budget',), expect=False,
                reason='unsupported dimensions or capacity')
    runner.case('oversized_sequences_rejected',
                extra=('--no-budget', '--max-seq', '268435455'), expect=False,
                reason='unsupported dimensions or capacity')
    return {'historical_size_array_matches': True,
            'historical_plan_sha256': sha(historical),
            'baseline_count': 1397, 'default_mtp_count': 1462,
            'page_lengths_checked': [15, 16, 17], 'budget_attempts': 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=BASE)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--model', type=Path, default=MODEL)
    parser.add_argument('--historical-plan', type=Path, default=HISTORICAL)
    parser.add_argument('--case-timeout', type=float, default=120)
    args = parser.parse_args()
    base = args.base.resolve()
    output = (args.output or base / 'host-plan-v1').resolve()
    if not output.is_relative_to(ROOT / '.q4t-work'):
        parser.error('output must be in this repository .q4t-work')
    if args.case_timeout <= 0:
        parser.error('case timeout must be positive')
    binary = base / 'build/q4t'
    build = json.loads((base / 'build.json').read_text())
    require(build['status'] == 'COMPLETED' and
            build['candidate_sha256'] == sha(binary), 'candidate build identity')
    default = ROOT / 'build/q4t'
    require(sha(default) == build['default_sha256'], 'default changed')
    output.mkdir(parents=True, exist_ok=False)
    identity = {'candidate': str(binary), 'candidate_sha256': sha(binary),
                'default_sha256': sha(default), 'script_sha256': sha(Path(__file__)),
                'build_manifest_sha256': sha(base / 'build.json'),
                'historical_plan': str(args.historical_plan.resolve()),
                'historical_plan_sha256': sha(args.historical_plan)}
    save(output / 'inputs.json', identity)
    runner = Runner(binary, args.model.resolve(), output, args.case_timeout)
    result = {'status': 'FAILED', 'gpu_execution': False,
              'gpu_configuration_accepted': False,
              'scope': 'host metadata/configuration contracts only'}
    try:
        result.update(checks(runner, args.historical_plan))
        require(sha(binary) == identity['candidate_sha256'] and
                sha(default) == identity['default_sha256'],
                'binary identity changed during host tests')
        result['status'] = 'PASS'
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        result['cases'] = runner.results
        result['completed_cases'] = len(runner.results)
        save(output / 'result.json', result)
        manifest = {str(path.relative_to(output)): sha(path)
                    for path in sorted(output.rglob('*')) if path.is_file()}
        save(output / 'sealed-files.json', manifest)
    print(json.dumps({'status': 'PASS', 'host_plan_cases': len(runner.results),
                      'gpu_configuration_accepted': False, 'output': str(output)}))


if __name__ == '__main__':
    main()
