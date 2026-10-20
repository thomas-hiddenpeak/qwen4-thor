"""Audit a terminal, sealed batch-performance-v1; never launch measurements."""
import json
import math
from pathlib import Path
import shutil

from run_batch_performance import HASHES, LENGTHS, ORDER, OUT, ROOT, compare, sha


def main():
    # No writes until the entire evidence set has passed the terminal audit.
    manifest = json.loads((OUT / 'sealed-files.json').read_text())
    for name, digest in manifest.items():
        if sha(OUT / name) != digest:
            raise ValueError('sealed file changed: ' + name)
    run = json.loads((OUT / 'run.json').read_text())
    seal = json.loads((OUT / 'seal.json').read_text())
    lease = json.loads((OUT / 'lease/exit.json').read_text())
    assert run['status'] == 'MEASUREMENTS_COMPLETE_ACCEPTANCE_PENDING'
    assert run['completed'] == ORDER
    assert seal['guard_exit'] == 0 and seal['guard_status'] == 'COMPLETED'
    assert lease['status'] == 'COMPLETED'
    queries = [json.loads(line) for line in (OUT / 'lease/nvml-queries.jsonl')
               .read_text().splitlines()]
    assert queries and all(row['status'] == 'OK' for row in queries)
    observations = {mode: compare(mode) for mode in ['off', 'on']}
    assert observations == json.loads((OUT / 'observations.json').read_text())
    for name in ORDER:
        label, mode = name.split('-')
        directory = OUT / name
        assert (directory / 'binary.sha256').read_text().strip() == HASHES[label]
        assert sha(OUT / (label + '-build/q4t')) == HASHES[label]
        shutdown = json.loads((directory / 'exit.json').read_text())
        assert shutdown['server'] == 0 and shutdown['completed'] == 5
        assert shutdown['http_output_checks_passed']
        assert shutdown['full_five_tier_completed']
        assert shutdown['failure'] is None and shutdown['cleanup_failure'] is None
        plan = json.loads((directory / 'performance-plan.json').read_text())
        assert plan['lengths'] == LENGTHS and plan['repeats'] == 3
        assert plan['client_protocol'] == 'one_evalscope_process_per_tier'
        command = json.loads((directory / 'server-command.json').read_text())
        assert not command['effective_q4t_environment']
        argv = command['argv']
        assert argv[argv.index('--max-seq') + 1] == '1'
        assert argv[argv.index('--max-prefill') + 1] == '8192'
        assert argv[argv.index('--max-len') + 1] == '208896'
        if mode == 'on':
            assert '--mtp' in argv and argv[argv.index('--mtp-k') + 1] == '3'
        else:
            assert '--no-mtp' in argv
        log = (directory / 'server.log').read_text()
        assert 'effective mtp=' + ('1' if mode == 'on' else '0') in log
    for rows in observations.values():
        for row in rows:
            for label in HASHES:
                assert len(row[label]) == 3
                assert all(math.isfinite(m[k]) and m[k] > 0
                           for m in row[label] for k in ['ttft', 'decode_tps'])
    assert sha(ROOT / 'build/q4t') == HASHES['parent']
    audit = dict(status='COMPLETE_DESCRIPTIVE_COMPARISON_NOT_STATISTICAL_ACCEPTANCE',
                 candidate_sha256=HASHES['candidate'], parent_sha256=HASHES['parent'],
                 sealed_files_verified=len(manifest), nvml_queries_ok=len(queries),
                 elapsed_seconds=seal['finished'] - seal['started'], model_loads=4,
                 performance_requests=60, retries=0, default_unchanged=True,
                 deployed=False, statistical_performance='INSUFFICIENT_EVIDENCE')
    destination = ROOT / 'docs/evidence/metadata-followup-20261020/batch-performance-v1'
    destination.mkdir(exist_ok=False)
    (destination / 'audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    for name in ['plan.json', 'run.json', 'seal.json', 'sealed-files.json',
                 'observations.json', 'frozen-plan.md']:
        shutil.copy2(OUT / name, destination / name)
    for name in ORDER:
        target = destination / name
        target.mkdir()
        for filename in ['results.json', 'exit.json', 'server-command.json',
                         'performance-plan.json', 'capacity.json', 'events.jsonl']:
            shutil.copy2(OUT / name / filename, target / filename)
    shutil.copy2(OUT / 'lease/exit.json', destination / 'lease-exit.json')
    print(json.dumps(audit, indent=2))
    for mode, rows in observations.items():
        for row in rows:
            print(mode, row['length'],
                  'TTFT%', round(row['ttft_median_change_pct'], 4),
                  'decode%', round(row['decode_tps_median_change_pct'], 4))


if __name__ == '__main__':
    main()
