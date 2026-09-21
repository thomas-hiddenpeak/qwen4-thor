"""Trace one accepted three-request HTTP workload per binary after all gates.

Trace timings are diagnostic only. Actual packed B=2 execution must be audited
from the exported CUDA timeline; request concurrency alone is insufficient.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

from run_prefill_batch_rows import ROOT, client, results, save, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['accepted-root', 'output', 'model-dir']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    run, out = args.accepted_root.resolve(), args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    gate = json.loads((run / 'acceptance.json').read_text())
    assert gate['quality_passed'] and gate['performance_accepted']
    for name in ['batch-http', 'interleave']:
        assert json.loads((run / name / 'review.json').read_text())['accepted']
    assert json.loads((run / 'lifecycle/complete.json').read_text())['passed'] == 4
    assert json.loads((run / 'numerical/exit.json').read_text())['numerical'] == 0
    assert len(json.loads((run / 'numerical/raw-review.json').read_text())['bit_exact_files']) == 68
    for name in ['src/model/model.cu', 'include/q4t/model/model.h',
                 'src/server/chat_server.cpp', 'include/q4t/server/chat_server.h']:
        assert (ROOT / name).read_bytes() == (run / 'source' / name).read_bytes()
    binaries = {'parent': run / 'q4t-before', 'candidate': ROOT / 'build/q4t'}
    baseline_groups = {'parent': 'parent-before', 'candidate': 'candidate'}
    accepted = json.loads((run / 'batch-http/results.json').read_text())
    out.mkdir(parents=True, exist_ok=False)
    for script in [Path(__file__), ROOT / 'tools/evalscope/run_prefill_batch_rows.py',
                   ROOT / 'tools/evalscope/run_chunk_interleave.py']:
        shutil.copy2(script, out / script.name)
    (out / 'nsys-version.txt').write_bytes(subprocess.check_output(['nsys', '--version']))
    shutil.copy2(run / 'batch-http/long.jsonl', out / 'long.jsonl')
    shutil.copy2(run / 'batch-http/parent-before/0/shorts.jsonl', out / 'shorts.jsonl')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    for version, binary in binaries.items():
        directory = out / version
        directory.mkdir()
        baseline = run / 'batch-http' / baseline_groups[version]
        assert sha(binary) == (baseline / 'binary.sha256').read_text().strip()
        command = json.loads((baseline / 'server-command.json').read_text())
        command[command.index('--port') + 1] = str(args.port)
        session = f'q4t-batch-{version}-{os.getpid()}'
        command = ['nsys', 'launch', '--session-new=' + session, '--trace=cuda',
                   '--discard-environment=true'] + command
        save(directory / 'server-command.json', command)
        (directory / 'binary.sha256').write_text(sha(binary) + '\n')
        failure, server, children, shutdown = None, None, [], None
        with (directory / 'server.log').open('w') as log, (directory / 'nsys.log').open('w') as trace:
            try:
                server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 180
                while 'serving on port' not in (directory / 'server.log').read_text():
                    assert server.poll() is None and time.monotonic() < deadline
                    time.sleep(1)
                startup = (directory / 'server.log').read_text()
                assert '=> max_len=65536 max_seq=3' in startup
                assert 'MTP disabled; plain decode' in startup
                start = ['nsys', 'start', '--session=' + session, '--sample=none',
                         '--cpuctxsw=none', '--output=' + str(directory / 'trace')]
                save(directory / 'capture-command.json', start)
                subprocess.run(start, stdout=trace, stderr=subprocess.STDOUT, check=True)
                children.append(client(args, out / 'long.jsonl', directory / 'long',
                                       1, 1, 1, 45056, 45056))
                deadline = time.monotonic() + 120
                while True:
                    assert children[0].poll() is None and time.monotonic() < deadline
                    with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/metrics', timeout=10) as response:
                        metrics = response.read().decode()
                    match = re.search(r'^q4t_requests_total (\d+)$', metrics, re.M)
                    assert match
                    if int(match[1]) == 1:
                        break
                    time.sleep(0.2)
                children.append(client(args, out / 'shorts.jsonl', directory / 'shorts',
                                       2, 2, 16, 1024, 4096))
                for child in children:
                    assert child.wait(timeout=900) == 0
                subprocess.run(['nsys', 'stop', '--session=' + session],
                               stdout=trace, stderr=subprocess.STDOUT, check=True)
                actual = results(directory / 'long') + results(directory / 'shorts')
                reference = next(x for x in accepted
                                 if x['group'] == baseline_groups[version] and x['repeat'] == 0)
                expected = {x['prompt_sha256']: x for x in reference['long'] + reference['short']}
                assert len(actual) == 3 and len({x['prompt_sha256'] for x in actual}) == 3
                for row in actual:
                    old = expected[row['prompt_sha256']]
                    assert row['success']
                    for field in ['prompt_tokens', 'completion_tokens', 'output_sha256']:
                        assert row[field] == old[field], (version, field)
                save(directory / 'results.json', actual)
            except Exception as error:
                failure = repr(error)
                raise
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                shutdown = subprocess.run(['nsys', 'shutdown', '--session=' + session,
                                           '--kill=sigterm'], stdout=trace, stderr=subprocess.STDOUT)
                if server is not None:
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                save(directory / 'exit.json', {'failure': failure,
                     'shutdown': shutdown.returncode,
                     'launcher': server.returncode if server else None,
                     'trace_exports_complete': False})
        assert shutdown.returncode == 0 and server.returncode == 0
        with (directory / 'export.log').open('w') as log:
            subprocess.run(['nsys', 'export', '--type=sqlite',
                            '--output=' + str(directory / 'trace.sqlite'),
                            str(directory / 'trace.nsys-rep')],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        terminal = json.loads((directory / 'exit.json').read_text())
        terminal['trace_exports_complete'] = True
        save(directory / 'exit.json', terminal)
        print(version + ': HTTP outputs matched; timeline coverage review pending', flush=True)
    save(out / 'complete.json', {'captures': 2, 'timeline_review_pending': True})


if __name__ == '__main__':
    main()
