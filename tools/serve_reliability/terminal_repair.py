"""Four reserved HTTP checks for the identified post-finish exception gap."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-reliability-20261030'
sys.path.insert(0, str(ROOT/'tools/evalscope'))
from serve_reliability import Runner, capability, decoded, fields, save, sha
import gpu_exclusive
import nvml_process_query


def prepare():
    original = json.loads((BASE/'http-plan.json').read_text())
    assert time.time() < original['deadline_unix']-900
    folder = BASE/'terminal-repair'
    folder.mkdir()
    frozen = folder/'frozen'
    frozen.mkdir()
    for source, name in [('build/q4t', 'candidate'), ('fault-build/q4t', 'fault'),
                         ('build/CMakeCache.txt', 'CMakeCache.txt')]:
        shutil.copy2(BASE/source, frozen/name)
    runtime = {str(p.relative_to(ROOT)):sha(p) for top in ['src', 'include']
               for p in (ROOT/top).rglob('*') if p.is_file()}
    old = json.loads((BASE/'frozen/runtime-source.json').read_text())
    delta = sorted(k for k in runtime if old.get(k) != runtime[k])
    assert delta == ['include/q4t/server/http_response.h',
                     'src/server/chat_generation.cpp', 'src/server/http_response.cpp']
    save(frozen/'runtime-source.json', runtime)
    save(folder/'dependency-review.json', dict(changed=delta,
        reused_http=92, reused_starts=18, original_candidate=sha(BASE/'frozen/candidate'),
        original_evidence=str(BASE/'decision.json'),
        reason='Only terminal construction/state changes; tokenizer, scheduling, '
               'model math, cache save/restore and statistics are unchanged. '
               'Successful final events are byte-equivalent apart from timestamps.'))
    plan = copy.deepcopy(original)
    plan.update(candidate=str(frozen/'candidate'), fault=str(frozen/'fault'),
        output=str(folder/'http'), planned_measurement_http=4,
        planned_measurement_starts=3, source_commit=subprocess.check_output(
            ['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        previous_measurement_http=92, previous_measurement_starts=18,
        repair='Post-finish exceptions cannot append a contradictory terminal.',
        runtime_manifest=str(frozen/'runtime-source.json'),
        build_cache=str(frozen/'CMakeCache.txt'), steps=[
            dict(name='terminal-full', mode='full', binary='fault', kind='terminal',
                 requests=2, environment={'Q4T_SERVE_FAULT':'handler.after_finish'}),
            dict(name='terminal-plain', mode='plain', binary='candidate', kind='terminal', requests=1),
            dict(name='terminal-subset', mode='subset', binary='candidate', kind='terminal', requests=1)])
    paths = [ROOT/n for n in runtime] + list(frozen.iterdir())
    paths += [Path(__file__), ROOT/'tools/evalscope/serve_reliability.py',
              BASE/'decision.json', folder/'dependency-review.json',
              BASE/'terminal-host.log', BASE/'build-terminal.log',
              BASE/'build-terminal-fault.log', ROOT/'CMakeLists.txt']
    paths += [Path(plan['artifact'])/n for n in ['head.bf16','token_ids.bin','manifest.json']]
    plan['files'] = {str(p):sha(p) for p in paths if p.is_file()}
    save(BASE/'repair-plan.json', plan)
    save(BASE/'delivery-package-superseded.json', dict(
        package=str(BASE/'delivery-package'), installed=False,
        reason=plan['repair'], replacement_plan=str(BASE/'repair-plan.json')))


class Repair(Runner):
    def run(self):
        fixture = self.plan['quality']['1024']
        for step in self.plan['steps']:
            with self.service(step) as (folder, _):
                if step['binary'] == 'fault':
                    label = step['name']+'-fault'
                    response = self.http(folder, label, self.body(label, fixture['prompt']))
                    wire = bytes.fromhex(response['wire_hex'])
                    assert response['status'] == 200 and b'[DONE]' not in wire
                    events = [json.loads(line[6:].decode('utf-8')) for line in wire.splitlines()
                              if line.startswith(b'data: ')]
                    assert not any('error' in event or 'usage' in event for event in events)
                    choices = [c for event in events for c in event.get('choices', [])]
                    assert sum(c.get('finish_reason') is not None for c in choices) == 1
                    assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == fixture['expected']
                    try:
                        decoded(response)
                    except AssertionError:
                        pass
                    else:
                        raise AssertionError('client accepted truncated terminal tail')
                    self.free()
                    raw = (folder/'server.log').read_text()
                    ends = [r for r in fields(raw, '[q4t][request] ') if r['event']=='end']
                    assert len(ends)==1 and ends[0]['outcome']=='error'
                    metrics = self.http(None, 'metrics', path='/metrics')
                    metric_text = bytes.fromhex(metrics['wire_hex']).decode()
                    assert '\nq4t_requests_success_total 0\n' in metric_text
                    assert '\nq4t_requests_error_total 1\n' in metric_text
                    save(folder/'fault-check.json', dict(passed=True,
                        truncated_rejected=True, contradictory_terminal=False,
                        metrics=metrics))
                label = step['name']+'-normal'
                body = self.body(label, fixture['prompt'])
                if step['binary'] == 'fault':
                    # Reuse the failed request identity with fresh credentials.
                    body['request_id'] = step['name']+'-fault'
                    body['cancel_token'] = capability(label)
                row = decoded(self.http(folder, label, body))
                prior = json.loads((BASE/'http'/('candidate-'+step['mode'])/'quality.json').read_text())
                expected = next(r for r in prior if r['length']==1024 and r['stream'])
                assert all(row[k]==expected[k] for k in ['text','usage','finish'])
                self.free()
                save(folder/'normal.json', row)
            self.completed.append(step['name'])
            print(step['name']+': PASS', flush=True)
        final = nvml_process_query.query(timeout=5)
        save(self.out/'final-gpu.json', final)
        assert final['status']=='OK' and not final['pids']
        assert self.http_count==4 and self.starts==3
        save(self.out/'result.json', dict(passed=True, http=4, starts=3,
             completed=self.completed, finished=time.time()))


def run():
    lease = json.loads(gpu_exclusive.common_lock().read_text())
    assert lease.get('state')=='held' and lease['pid']==__import__('os').getppid()
    plan = json.loads((BASE/'repair-plan.json').read_text())
    for path, digest in plan['files'].items():
        assert sha(path)==digest, path
    Repair(plan).run()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare','run'])
    globals()[parser.parse_args().action]()
