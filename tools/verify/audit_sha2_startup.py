"""Read-only audit of the bounded SHA2 stage; never runs inference."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(mapping, root=None):
    for path, expected in mapping.items():
        assert sha((root / path) if root else Path(path)) == expected, path
    return len(mapping)


def group(path):
    rows = read(path / 'results.json')
    exit_state = read(path / 'exit.json')
    assert exit_state['server'] == 0 and exit_state['http_output_checks_passed']
    log = (path / 'server.log').read_text()
    final = [json.loads(line.split('] ', 1)[1]) for line in log.splitlines()
             if line.startswith('[startup-reservation] ') and '"event":"finalized"' in line]
    assert len(final) == 1 and final[0]['status'] == 'PASS'
    assert final[0]['release_unknown'] == final[0]['handed_live'] == final[0]['unhanded_remaining'] == 0
    assert 'MTP init failed' not in log and 'retained after failed drain' not in log
    phase = {key: int(ms) / 1000 for key, ms in re.findall(r'\[startup\] (\w+) (\d+) ms', log)}
    events = [json.loads(line) for line in (path / 'events.jsonl').read_text().splitlines()]
    ready = next(e['monotonic_seconds'] for e in events if e['event'] == 'server_ready')
    start = next(e['monotonic_seconds'] for e in events if e['event'] == 'server_started')
    if 'metrics' in rows[0]:
        assert [r['length'] for r in rows] == [1024, 4096, 8192, 45056, 204800]
        assert all(len(r['metrics']) == 3 and len(set(r['outputs'])) == 1 for r in rows)
        count = sum(len(r['metrics']) for r in rows)
    else:
        assert all(r['success'] == 1 and r['exact_match'] for r in rows)
        count = len(rows)
    histograms = [[int(x) for x in value.split(',')] for value in
                  re.findall(r'\[mtp-accept\] request=\S+ draft_counts=(\d+,\d+,\d+,\d+)', log)]
    return dict(requests=count, rows=rows, finalized=final[0], phase_seconds=phase,
                ready_seconds=ready-start, acceptance_histograms=histograms)


def binary_codegen(stage):
    directory = stage / 'code-comparison'
    functions = []
    raw = {}
    for label, binary in [('old', stage / 'parent/q4t'), ('new', stage / 'build/q4t')]:
        output = directory / (label + '-binary.sass')
        if not output.exists():
            with output.open('x') as stream:
                subprocess.run(['/usr/local/cuda/bin/cuobjdump', '--dump-sass',
                                str(binary)], stdout=stream, check=True)
        raw[output.name] = sha(output)
        blocks, function = {}, None
        for line in output.read_text().splitlines(True):
            if 'Function :' in line:
                function = line.strip()
                assert function not in blocks, 'duplicate GPU function'
                blocks[function] = ''
            if function is not None:
                blocks[function] += line
        functions.append(blocks)
    old, new = functions
    assert old.keys() == new.keys() and len(new) >= 105
    changed = [name for name in new if old[name] != new[name]]
    assert len(changed) == 1 and 'GatedDeltaNetRegKernelILi4E' in changed[0]
    return dict(total_functions=len(new), changed_functions=changed,
                default_functions_equal=True, raw_files=raw,
                scope='actual frozen executables; only unused Q4T_GDN_REG=4 differs; default ROWS=8 unchanged')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--previous-stage', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    stage, previous = args.stage.resolve(), args.previous_stage.resolve()
    plan = read(stage / 'frozen-plan.json')
    out = Path(plan['output'])
    state = read(out / 'state.json')
    assert state['status'] == 'MEASURED_REQUIRES_REVIEW'
    assert state['completed'] == [s['name'] for s in plan['steps']]
    assert state['finished'] - state['started'] <= plan['stage_timeout_seconds']
    binary_comparison = binary_codegen(stage)
    bound = verify(plan['files'])
    sealed = verify(read(out / 'files.json'), out)
    guard_dir = root / '.q4t-work/sha2-startup-lease-v1'
    guard = read(guard_dir / 'exit.json')
    assert guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0
    nvml = [json.loads(line) for line in (guard_dir / 'nvml-queries.jsonl').read_text().splitlines()]
    assert nvml and all(row['status'] == 'OK' and row['returncode'] == 0 for row in nvml)
    prior = root / '.q4t-work/acceptance-v2'
    old_guard = read(previous / 'lease-v2/exit.json')
    assert old_guard['status'] == 'COMPLETED' and old_guard['child_returncode'] == 0
    # The prior stage seal binds all reused output evidence, including raw HTTP.
    old_seal = read(prior / 'files.json')
    reused = {p: digest for p, digest in old_seal.items()
              if p.split('/')[0] in ['quality-on', 'quality-off', 'candidate-on', 'candidate-off']}
    assert reused
    reused_count = verify(reused, prior)
    groups = {name: group(out / name) for name in
              ['quality-on', 'parent-start-a', 'quality-off', 'candidate-off', 'parent-start-b', 'candidate-on']}
    assert sum(row['requests'] for row in groups.values()) == plan['http_requests'] == 54
    comparisons = {}
    for mode in ['on', 'off']:
        old = group(prior / ('candidate-' + mode))
        new = groups['candidate-' + mode]
        assert new['acceptance_histograms'] == old['acceptance_histograms']
        comparison = []
        for a, b in zip(old['rows'], new['rows']):
            assert a['length'] == b['length'] and a['outputs'] == b['outputs']
            assert a['prompt_sha256'] == b['prompt_sha256']
            row = dict(length=a['length'], outputs_equal=True,
                       parent_metrics=a['metrics'], candidate_metrics=b['metrics'])
            for metric in ['ttft', 'decode_tps']:
                before = statistics.median(v[metric] for v in a['metrics'])
                after = statistics.median(v[metric] for v in b['metrics'])
                row[metric] = dict(parent=before, candidate=after, change_pct=(after/before-1)*100)
            comparison.append(row)
        comparisons[mode] = comparison
        old_quality = read(prior / ('quality-' + mode) / 'results.json')
        for a, b in zip(old_quality, groups['quality-' + mode]['rows']):
            assert (a['id'], a['text'], a['actual_output'], a['prompt_sha256']) == (b['id'], b['text'], b['actual_output'], b['prompt_sha256'])
    contract = (out / 'sha-contract-driver.log').read_text()
    assert 'hardware_sha2=1' in contract and 'PASS vectors=22 digests=1408 file_errors=4' in contract
    artifact = read(out / 'artifact-cases/result.json')
    assert artifact['source_rows_bit_exact'] == 65536
    assert len(artifact['cases']) == 14 and all(c['passed'] for c in artifact['cases'])
    # Runtime changes are restricted to SHA and its test-only declarations.
    old_plan = read(previous / 'frozen-plan-v2.json')
    changed = [p for p, digest in old_plan['files'].items()
               if str(root / 'src') + '/' in p or str(root / 'include') + '/' in p
               if sha(Path(p)) != digest]
    assert sorted(str(Path(p).relative_to(root)) for p in changed) == [
        'include/q4t/trace/sha256.h', 'src/trace/sha256.cpp']
    archives = {p.name: sha(p) == sha(previous / 'build' / p.name)
                for p in (stage / 'build').glob('libq4t*.a')
                if (previous / 'build' / p.name).is_file()}
    assert all(archives[name] for name in ['libq4t_io.a', 'libq4t_text.a',
                                          'libq4t_server.a', 'libq4t_runtime.a',
                                          'libq4t_startup_plan.a'])
    code_comparison = read(stage / 'code-comparison/comparison.json')
    assert code_comparison['ple']['sass_equal'] and code_comparison['quant']['sass_equal']
    assert code_comparison['model']['default_functions_equal']
    assert code_comparison['model']['only_changed_unused_gdn_rows4']
    build_text = (stage / 'build.log').read_text() + (stage / 'configure.log').read_text()
    assert not re.search(r'warning:|CMake Warning|error:', build_text, re.I)
    parent_start = [groups[n]['phase_seconds']['startup_total'] for n in ['parent-start-a', 'parent-start-b']]
    candidate_start = [groups[n]['phase_seconds']['startup_total'] for n in ['quality-on', 'candidate-on']]
    benefit = statistics.median(parent_start) - statistics.median(candidate_start)
    memory = []
    for line in (out / 'unified-memory.jsonl').read_text().splitlines():
        record = json.loads(line)
        mem = {k: int(v) for k, v in re.findall(r'^(\w+):\s+(\d+) kB', record['meminfo'], re.M)}
        memory.append(dict(time=record['time'], available_kib=mem['MemAvailable'],
                           cache_kib=mem['Cached'], free_kib=mem['MemFree']))
    audit = dict(status='PASS_BOUNDED_EVIDENCE', source_commit=plan['source_commit'],
                 parent_sha256=plan['parent_sha256'], candidate_sha256=plan['candidate_sha256'],
                 frozen_files=bound, sealed_run_files=sealed, reused_files=reused_count,
                 elapsed_seconds=state['finished']-state['started'], http_requests=54,
                 guard=guard, nvml_queries=len(nvml), nvml_max_seconds=max(r['seconds'] for r in nvml),
                 build_zero_warnings=True, runtime_changed=changed, archives_equal=archives,
                 code_comparison=code_comparison, binary_codegen=binary_comparison,
                 hash_contract=contract, artifact_contract=artifact,
                 parent_startup_seconds=parent_start, candidate_startup_seconds=candidate_start,
                 startup_median_saved_seconds=benefit, startup_target_met=benefit >= 20,
                 memory=dict(samples=len(memory), minimum_available_kib=min(r['available_kib'] for r in memory),
                             minimum_free_kib=min(r['free_kib'] for r in memory),
                             maximum_cached_kib=max(r['cache_kib'] for r in memory),
                             note='host-wide physical memory; peaks are not additive or process-attributable'),
                 groups=groups, comparisons=comparisons)
    with (stage / 'audit.json').open('x') as stream:
        json.dump(audit, stream, indent=2); stream.write('\n')
    print(json.dumps({k: v for k, v in audit.items()
                      if k not in ['groups', 'hash_contract', 'guard']}, indent=2))


if __name__ == '__main__':
    main()
