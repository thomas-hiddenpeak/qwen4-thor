"""Known-work Nsight fixture audit; diagnostics never grant model acceptance."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3

ITERATIONS, ELEMENTS, COPY_BYTES = 32, 4096, 16384
LAUNCH = re.compile(r'^cudaLaunchKernel(?:_v\d+)?$')
COPY = re.compile(r'^cudaMemcpyAsync(?:_v\d+)?$')
MEMSET = re.compile(r'^cudaMemsetAsync(?:_v\d+)?$')
UNSUPPORTED_UM = re.compile(
    r'^CUDA device \d+: Unified Memory trace is not supported by the current '
    r'driver version or configuration\.$')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(4 * 1024 ** 2), b''):
            h.update(part)
    return h.hexdigest()


def pid(value):
    return (int(value) >> 24) & 0xffffff


def tid(value):
    return int(value) & 0xffffff


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    return db


def audit(database, receipt, expected_pid=None):
    """Read originals only. No runtime/model/GPU imports or live observations."""
    errors, records, mapping, diagnostics = [], {}, [], {}
    memory_coverage = dict(status='UNKNOWN_CAPABILITY', gpu_memory_coverage_complete=False,
                          scope='No missing memset activity is silently called a dropped record.')

    def check(condition, message):
        if not condition:
            errors.append(message)

    try:
        for line_number, line in enumerate(Path(receipt).read_text().splitlines(), 1):
            if not line.startswith('[fixture-'):
                continue
            marker, payload = line.split('] ', 1)
            row = json.loads(payload)
            row['receipt_line'] = line_number
            records.setdefault(marker[1:], []).append(row)
        for name in ('fixture-config', 'fixture-ready', 'fixture-exit'):
            check(len(records.get(name, [])) == 1, name + ' must occur exactly once')
        check(not records.get('fixture-error'), 'fixture reported an execution error')
        check(set(records) <= {'fixture-config', 'fixture-step', 'fixture-ready',
                              'fixture-exit', 'fixture-error'}, 'unknown fixture receipt')
        config, ready, exited = (records[k][0] for k in
                                 ('fixture-config', 'fixture-ready', 'fixture-exit'))
        target_pid, target_tid = config['pid'], config['tid']
        check(isinstance(target_pid, int) and isinstance(target_tid, int) and
              0 < target_pid < 2 ** 24 and 0 < target_tid < 2 ** 24,
              'receipt PID/TID domain')
        check(expected_pid is None or target_pid == expected_pid, 'external target PID mismatch')
        fixed = dict(schema=1, iterations=32, elements=4096, bytes_per_copy=16384,
                     expected_kernels=32, expected_h2d=32, expected_d2h=32,
                     expected_memset_apis=32, expected_nvtx=33, release_timeout_seconds=60)
        check(all(config.get(k) == v for k, v in fixed.items()), 'fixed fixture contract mismatch')
        steps = records.get('fixture-step', [])
        check([r['id'] for r in steps] == list(range(ITERATIONS)), 'receipt step IDs/order incomplete')
        for step in steps:
            index = step['id']
            expected_memset_bytes = 512 if index % 2 == 0 else COPY_BYTES
            cleared_elements = expected_memset_bytes // 4
            wanted_input = ELEMENTS * (ELEMENTS - 1) // 2 + ELEMENTS * index * ELEMENTS
            cleared_input = cleared_elements * (cleared_elements - 1) // 2 + cleared_elements * index * ELEMENTS
            wanted_output = wanted_input - cleared_input + ELEMENTS * (7 + index)
            check(step['pid'] == target_pid and step['tid'] == target_tid and
                  step['passed'] is True and step['mismatches'] == 0 and
                  step['input_checksum'] == wanted_input and
                  step['output_checksum'] == step['expected_checksum'] == wanted_output and
                  step['memset_bytes'] == expected_memset_bytes and step['memset_error'] == 0 and
                  step['h2d_error'] == step['launch_error'] == step['d2h_error'] == 0,
                  'independent output/CUDA receipt failed at step ' + str(index))
            check(config['receipt_line'] < step['receipt_line'] < ready['receipt_line'],
                  'receipt ordering failed')
        check(ready['pid'] == exited['pid'] == target_pid and
              ready['tid'] == exited['tid'] == target_tid and ready['passed'] is True and
              ready['completed_steps'] == ITERATIONS and
              ready['profiler_start_error'] == ready['profiler_stop_error'] == ready['final_sync_error'] == 0 and
              exited['released'] is True and exited['returncode'] == 0 and
              ready['receipt_line'] < exited['receipt_line'], 'ready/release/exit receipt failed')
        with connect(database) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'StringIds', 'CUPTI_ACTIVITY_KIND_RUNTIME', 'CUPTI_ACTIVITY_KIND_KERNEL',
                        'CUPTI_ACTIVITY_KIND_MEMCPY', 'NVTX_EVENTS', 'ENUM_CUDA_MEMCPY_OPER',
                        'DIAGNOSTIC_EVENT', 'ENUM_DIAGNOSTIC_SEVERITY_LEVEL'}
            if not required <= tables:
                raise ValueError('missing required tables: ' + str(sorted(required - tables)))
            strings = {r['id']: r['value'] for r in db.execute('SELECT * FROM StringIds')}
            apis = [dict(r, name=strings[r['nameId']]) for r in
                    db.execute('SELECT rowid AS source_rowid,* FROM CUPTI_ACTIVITY_KIND_RUNTIME')]
            kernels = [dict(r, name=strings[r['demangledName']]) for r in
                       db.execute('SELECT rowid AS source_rowid,* FROM CUPTI_ACTIVITY_KIND_KERNEL')]
            kinds = {r['id']: r['name'] for r in db.execute('SELECT * FROM ENUM_CUDA_MEMCPY_OPER')}
            copies = [dict(r, direction=kinds[r['copyKind']]) for r in
                      db.execute('SELECT rowid AS source_rowid,* FROM CUPTI_ACTIVITY_KIND_MEMCPY')]
            memsets = [dict(r) for r in db.execute('SELECT rowid AS source_rowid,* FROM CUPTI_ACTIVITY_KIND_MEMSET')] if 'CUPTI_ACTIVITY_KIND_MEMSET' in tables else []
            nvtx = []
            for r in db.execute('SELECT rowid AS source_rowid,* FROM NVTX_EVENTS'):
                row = dict(r)
                row['name'] = row.get('text') or strings.get(row.get('textId'), '')
                if row['name'].startswith('k3fixture|'):
                    nvtx.append(row)
            main_apis = [r for r in apis if pid(r['globalTid']) == target_pid and tid(r['globalTid']) == target_tid]
            target_kernels = [r for r in kernels if pid(r['globalPid']) == target_pid]
            target_copies = [r for r in copies if pid(r['globalPid']) == target_pid]
            target_memsets = [r for r in memsets if pid(r['globalPid']) == target_pid]
            launches = [r for r in main_apis if LAUNCH.fullmatch(r['name'])]
            copy_apis = [r for r in main_apis if COPY.fullmatch(r['name'])]
            memset_apis = [r for r in main_apis if MEMSET.fullmatch(r['name'])]
            check(len(launches) == len(target_kernels) == ITERATIONS,
                  'expected exactly 32 application launches and kernels')
            check(len(copy_apis) == len(target_copies) == ITERATIONS * 2,
                  'expected exactly 64 copy APIs and activities')
            check(len(memset_apis) == ITERATIONS, 'expected exactly 32 successful nonzero memset APIs')
            check(not [r for r in main_apis if 'Launch' in r['name'] and not LAUNCH.fullmatch(r['name'])],
                  'unexpected target launch API')
            check(not [r for r in main_apis if 'Memcpy' in r['name'] and not COPY.fullmatch(r['name'])],
                  'unexpected target copy API')
            check(not [r for r in main_apis if 'Memset' in r['name'] and not MEMSET.fullmatch(r['name'])],
                  'unexpected target memset API')
            check(all('K3FixtureKernel' in r['name'] for r in target_kernels),
                  'unexpected target kernel name')
            check(not [r for r in kernels if 'K3FixtureKernel' in r['name'] and pid(r['globalPid']) != target_pid],
                  'fixture kernel belongs to wrong PID')
            check(all(r['returnValue'] == 0 for r in main_apis), 'nonzero target CUDA API result')
            for r in main_apis + target_kernels + target_copies + target_memsets + nvtx:
                check(r['end'] is not None and 0 <= r['start'] <= r['end'],
                      'unclosed or invalid trace interval')
            expected_names = ['k3fixture|root'] + ['k3fixture|step=' + str(i) for i in range(ITERATIONS)]
            check(Counter(r['name'] for r in nvtx) == Counter(expected_names),
                  '33 fixed NVTX IDs missing/duplicated/unexpected')
            check(all(pid(r['globalTid']) == target_pid and tid(r['globalTid']) == target_tid and
                      (r.get('endGlobalTid') is None or r['endGlobalTid'] == r['globalTid']) for r in nvtx),
                  'NVTX wrong PID/TID')
            by_name = {r['name']: r for r in nvtx}
            root = by_name['k3fixture|root']
            api_by_correlation = {}
            for api in launches + copy_apis:
                api_by_correlation.setdefault(api['correlationId'], []).append(api)
            activities_by_correlation = {}
            for row in target_kernels + target_copies:
                activities_by_correlation.setdefault(row['correlationId'], []).append(row)
            check(set(api_by_correlation) == set(activities_by_correlation) and
                  all(len(r) == 1 for r in api_by_correlation.values()) and
                  all(len(r) == 1 for r in activities_by_correlation.values()),
                  'bidirectional API/activity correlation is not bijective')
            memset_api_by_correlation, memset_by_correlation = {}, {}
            for row in memset_apis:
                memset_api_by_correlation.setdefault(row['correlationId'], []).append(row)
            for row in target_memsets:
                memset_by_correlation.setdefault(row['correlationId'], []).append(row)
            check(all(len(rows) == 1 for rows in memset_api_by_correlation.values()) and
                  all(len(rows) == 1 for rows in memset_by_correlation.values()) and
                  set(memset_by_correlation) <= set(memset_api_by_correlation),
                  'memset activity has duplicate or unmatched API correlation')
            check(not (set(memset_api_by_correlation) & set(api_by_correlation)),
                  'memset and kernel/copy API correlation domains overlap')
            check(not [r for r in memsets if r['correlationId'] in memset_api_by_correlation
                       and pid(r['globalPid']) != target_pid],
                  'fixture-correlated memset activity belongs to wrong PID')
            check(Counter(r['direction'] for r in target_copies) ==
                  Counter(CUDA_MEMCPY_KIND_HTOD=32, CUDA_MEMCPY_KIND_DTOH=32),
                  'copy directions differ from fixed 32 H2D/32 D2H')
            check(all(r['bytes'] == COPY_BYTES and r.get('copyCount', 1) == 1 for r in target_copies),
                  'copy bytes or batching changed')
            check(len({(r['deviceId'], r['contextId'], r['streamId']) for r in target_kernels + target_copies + target_memsets}) == 1,
                  'fixture activity stream/context/device changed')
            check(all((r['gridX'], r['gridY'], r['gridZ'], r['blockX'], r['blockY'], r['blockZ']) ==
                      (16, 1, 1, 256, 1, 1) for r in target_kernels), 'kernel grid/block changed')
            previous = root['start']
            unmatched_memsets = []
            for index in range(ITERATIONS):
                name = 'k3fixture|step=' + str(index)
                leaf = by_name[name]
                check(root['start'] <= previous <= leaf['start'] <= leaf['end'] <= root['end'],
                      'ordered closed NVTX nesting failed at step ' + str(index))
                previous = leaf['end']
                calls = sorted([r for r in launches + copy_apis + memset_apis if leaf['start'] <= r['start'] and r['end'] <= leaf['end']],
                               key=lambda r: r['start'])
                check(len(calls) == 4 and COPY.fullmatch(calls[0]['name']) and
                      MEMSET.fullmatch(calls[1]['name']) and LAUNCH.fullmatch(calls[2]['name']) and
                      COPY.fullmatch(calls[3]['name']),
                      'step must contain H2D/memset/launch/D2H APIs: ' + str(index))
                if len(calls) != 4:
                    continue
                core_calls = [calls[0], calls[2], calls[3]]
                linked = [activities_by_correlation.get(r['correlationId'], []) for r in core_calls]
                if not all(len(r) == 1 for r in linked):
                    continue
                h2d, kernel, d2h = [r[0] for r in linked]
                check(h2d.get('direction') == 'CUDA_MEMCPY_KIND_HTOD' and
                      'K3FixtureKernel' in kernel.get('name', '') and
                      d2h.get('direction') == 'CUDA_MEMCPY_KIND_DTOH',
                      'step API/activity type mismatch: ' + str(index))
                check(root['start'] <= h2d['start'] <= h2d['end'] <= kernel['start'] <=
                      kernel['end'] <= d2h['start'] <= d2h['end'] <= root['end'],
                      'single-stream activity order/last completion failed: ' + str(index))
                check(all(activity[0]['start'] >= call['start'] for call, activity in zip(core_calls, linked)),
                      'GPU activity precedes submission')
                memory = memset_by_correlation.get(calls[1]['correlationId'], [])
                memset_bytes = 512 if index % 2 == 0 else COPY_BYTES
                if not memory:
                    unmatched_memsets.append(dict(id=index, bytes=memset_bytes,
                        api_rowid=calls[1]['source_rowid'], correlation_id=calls[1]['correlationId']))
                elif len(memory) == 1:
                    item = memory[0]
                    check(item['bytes'] == memset_bytes and item['value'] == 0 and
                          calls[1]['start'] <= item['start'] and
                          h2d['end'] <= item['start'] <= item['end'] <= kernel['start'],
                          'known memset value/bytes/submission/stream order failed: ' + str(index))
                mapping.append(dict(id=index, nvtx_rowid=leaf['source_rowid'],
                    launch_api_rowid=calls[2]['source_rowid'], kernel_rowid=kernel['source_rowid'],
                    correlation_ids=[r['correlationId'] for r in calls],
                    memset_api_rowid=calls[1]['source_rowid'], memset_bytes=memset_bytes,
                    memset_activity_rowid=memory[0]['source_rowid'] if len(memory) == 1 else None,
                    h2d_rowid=h2d['source_rowid'], d2h_rowid=d2h['source_rowid']))
            syncs = [r for r in main_apis if r['name'].startswith('cudaStreamSynchronize') and
                     root['start'] <= r['start'] <= r['end'] <= root['end']]
            check(len(syncs) == 1 and all(r['end'] <= syncs[0]['end'] for r in target_kernels + target_copies + target_memsets),
                  'single final stream synchronization does not cover all work')
            memory_complete = (len(memset_apis) == len(target_memsets) == ITERATIONS and
                               not unmatched_memsets and len(mapping) == ITERATIONS and not errors)
            memory_coverage = dict(status='COMPLETE' if memory_complete else 'UNKNOWN_CAPABILITY',
                gpu_memory_coverage_complete=memory_complete,
                known_nonzero_memset_apis=len(memset_apis), observed_activities=len(target_memsets),
                activity_table_present='CUPTI_ACTIVITY_KIND_MEMSET' in tables,
                expected_by_bytes={'512': 16, '16384': 16},
                observed_by_bytes=dict(Counter(str(r['bytes']) for r in target_memsets)),
                unmatched_by_bytes=dict(Counter(str(r['bytes']) for r in unmatched_memsets)),
                unmatched_apis=unmatched_memsets,
                scope='Nonzero argument sizes come from the independent fixture ledger. Missing GPU memset activity is UNKNOWN_CAPABILITY, not proof of dropped records; model gate must require complete coverage.')
            levels = {r['id']: r['name'] for r in db.execute('SELECT * FROM ENUM_DIAGNOSTIC_SEVERITY_LEVEL')}
            messages = [dict(r, severity_name=levels.get(r['severity'], 'UNKNOWN')) for r in
                        db.execute('SELECT * FROM DIAGNOSTIC_EVENT')]
            diagnostic_errors, warnings, auxiliary = [], [], []
            for r in messages:
                severity = r['severity_name'].lower()
                if UNSUPPORTED_UM.fullmatch(r['text'].strip()):
                    auxiliary.append(r)
                elif severity not in ('info', 'warning') or re.search(r'\b(dropped|lost|overflow|truncat\w*|incomplete)\b', r['text'], re.I):
                    diagnostic_errors.append(r)
                elif severity == 'warning':
                    warnings.append(r)
            diagnostics = dict(messages=messages, errors=diagnostic_errors, warnings=warnings,
                valid=not diagnostic_errors and not warnings,
                unavailable_auxiliary_features=auxiliary,
                actual_target_warnings=[r for r in warnings if r['globalPid'] is not None and pid(r['globalPid']) == target_pid],
                scope='All diagnostics retained. Known fixture work can be complete despite warning; this never validates a model trace or grants warning exemptions.')
            counts = dict(application_launches=len(launches), application_kernels=len(target_kernels),
                          application_copy_apis=len(copy_apis), application_copies=len(target_copies),
                          application_memset_apis=len(memset_apis), application_memsets=len(target_memsets),
                          fixture_nvtx=len(nvtx), mapped_steps=len(mapping),
                          other_thread_apis=len(apis) - len(main_apis))
    except (OSError, sqlite3.Error, KeyError, IndexError, TypeError, ValueError) as error:
        errors.append('audit structure/parse: ' + repr(error))
        counts = locals().get('counts', {})
    known_complete = not errors
    diagnostic_errors = diagnostics.get('errors', ['diagnostics unavailable'])
    passed = known_complete and not diagnostic_errors
    return dict(schema=1, known_work_complete=known_complete, fixture_contract_passed=passed,
        gpu_memory_coverage_complete=memory_coverage['gpu_memory_coverage_complete'],
        memory_coverage=memory_coverage,
        errors=errors, diagnostics=diagnostics, counts=counts, iteration_mapping=mapping,
        receipt=records, expected_pid=expected_pid, external_pid_binding=expected_pid is not None,
        input_hashes=dict(database=sha(database), receipt=sha(receipt)),
        performance_acceptance=False, model_trace_validated=False,
        scope='Known 32 kernels, 64 copies, 33 NVTX ranges plus successful APIs/output core only. GPU memset coverage is separate and required for model capture. Ground truth is independent of trace pair self-consistency; warnings remain unresolved for general model traces.')


def negative_copies(database, receipt, output_directory, expected_pid=None):
    """Create new SQLite derivatives; original report/database remain immutable."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=False)
    original_sha = sha(database)
    positive = audit(database, receipt, expected_pid)
    if not positive['fixture_contract_passed']:
        raise ValueError('negative derivatives require a complete known-work positive')
    final = positive['iteration_mapping'][-1]
    results = {}
    for kind in ('drop-launch-kernel-pair', 'drop-last-nvtx'):
        destination = output_directory / (kind + '.sqlite')
        with connect(database) as source, sqlite3.connect(destination) as target:
            source.backup(target)
            if kind == 'drop-launch-kernel-pair':
                target.execute('DELETE FROM CUPTI_ACTIVITY_KIND_RUNTIME WHERE rowid=?', (final['launch_api_rowid'],))
                target.execute('DELETE FROM CUPTI_ACTIVITY_KIND_KERNEL WHERE rowid=?', (final['kernel_rowid'],))
            else:
                target.execute('DELETE FROM NVTX_EVENTS WHERE rowid=?', (final['nvtx_rowid'],))
            target.commit()
        result = audit(destination, receipt, expected_pid)
        results[kind] = dict(path=str(destination.resolve()), sha256=sha(destination),
                            expected_rejected=not result['fixture_contract_passed'], audit=result)
        if result['fixture_contract_passed']:
            raise ValueError('negative derivative incorrectly accepted: ' + kind)
    if sha(database) != original_sha:
        raise ValueError('original SQLite changed during derivative checks')
    return dict(original_sha256=original_sha, derivatives=results,
                scope='Offline validator negative controls; not native collector-loss experiments.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--target-pid', type=int)
    parser.add_argument('--negative-copies', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('refuse output overwrite')
    result = audit(args.database, args.receipt, args.target_pid)
    if args.negative_copies and result['fixture_contract_passed']:
        result['negative_controls'] = negative_copies(args.database, args.receipt, args.negative_copies, args.target_pid)
    result['script_sha256'] = sha(Path(__file__))
    with args.output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(dict(known_work_complete=result['known_work_complete'],
                         fixture_contract_passed=result['fixture_contract_passed'],
                         gpu_memory_coverage_complete=result['gpu_memory_coverage_complete'],
                         memory_coverage_status=result['memory_coverage']['status'],
                         diagnostics_valid=result['diagnostics'].get('valid', False), errors=result['errors'])))
    return 0 if result['fixture_contract_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
