"""One bounded double-buffer candidate, reusing the fixed ownership monitor."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('save_seed', ROOT/'tools/prefix_seed/run_stage.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
reuse, legacy, http, save = seed.reuse, seed.legacy, seed.http, seed.save


def parse(raw, prefix):
    return [dict(re.findall(r'(\w+)=(\S+)', line)) for line in raw.splitlines()
            if line.startswith(prefix)]


def validate(plan):
    if (plan['http_requests'], plan['http_loads'], plan['maximum_gpu_contracts']) != (40, 4, 0):
        raise ValueError('frozen budget')
    if len(plan['steps']) != 5 or time.time() >= plan['task_deadline_unix']:
        raise RuntimeError('step scope/deadline')


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output'])/name
    out.mkdir()
    env = legacy.clean_env() | {'Q4T_SESSION_SAVE_TIMING': '1',
                               'Q4T_SESSION_PREALLOCATE': str(step['preallocate']),
                               'Q4T_SESSION_PIPELINE': str(step['pipeline'])}
    if step['kind'] == 'contract':
        with (out/'contract.log').open('x') as log:
            result = subprocess.run([plan['storage_contract'], str(out/'cache')],
                                    env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=min(90, plan['task_deadline_unix']-time.time()))
        save(out/'result.json', dict(passed=result.returncode == 0,
             logical_write_upper_bound=256*1024**2))
        if result.returncode:
            raise RuntimeError('CPU storage contract')
        return
    command = [plan['candidate'], 'serve', '--model-dir', plan['model'],
        '--host', '127.0.0.1', '--port', str(plan['port']), '--max-seq', '4',
        '--max-prefill', '4096', '--max-len', '208896',
        '--seq-capacities', '208896,65536,65536,65536', '--mtp-suffix']
    for kind in ['session', 'prefix']:
        cache = out/kind
        cache.mkdir(mode=0o700)
        command += ['--'+kind+'-cache-dir', str(cache)]
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    save(out/'server-command.json', dict(argv=command,
         environment={k:v for k,v in env.items() if k.startswith('Q4T_')}))
    save(out/'step.json', step)
    server, failure, passed = None, None, False
    def check():
        if time.time() >= plan['task_deadline_unix']-30:
            raise TimeoutError('absolute task deadline')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited')
    with (out/'server.log').open('x') as log:
        try:
            http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            http.event(out, 'server_started', pid=server.pid)
            while 'serving on port' not in (out/'server.log').read_text():
                check()
                time.sleep(1)
            raw = (out/'server.log').read_text()
            capacity = parse(raw, '[q4t][capacity] ')
            if len(capacity) != 1 or 'capacities=208896,65536,65536,65536' not in raw:
                raise RuntimeError('slot capacities')
            for prefix in ['requested', 'effective']:
                for key, value in [('max_seq', 4), ('max_len', 208896), ('max_prefill', 4096)]:
                    if int(capacity[0][prefix+'_'+key]) != value:
                        raise RuntimeError('capacity reduced')
            http.event(out, 'server_ready', pid=server.pid)
            results = []
            for shared in step['shared_order']:
                folder = out/('shared-on' if shared else 'shared-off')
                folder.mkdir()
                def extra(i):
                    key = name+str(shared)
                    value = {'session_id': seed.capability(key+str(i))}
                    if shared:
                        value['prefix_cache_scope'] = seed.capability(key)
                        if i == 0:
                            value['prefix_cache_write_tokens'] = 8192
                    return value
                def one(i):
                    row = seed.old.eval_one(plan, plan['scenario']['cases'][i],
                                           folder/str(i), env, check, extra(i))
                    expected = plan['references'][i]
                    if any(row[k] != expected[k] for k in
                           ['prompt_sha256', 'input_tokens', 'output_sha256',
                            'output_tokens', 'finish']):
                        raise RuntimeError('fixed full-output oracle')
                    return row
                rows = [one(0)]
                with ThreadPoolExecutor(max_workers=3) as pool:
                    rows += list(pool.map(one, [1, 2, 3]))
                raw = (out/'server.log').read_text()
                restores = {r['request']:r for r in parse(raw, '[session-cache] request=')
                            if r['event'] == 'restore'}
                writes = [r for r in parse(raw, '[prefix-cache] request=')
                          if r['request'] in {row['response_id'] for row in rows}]
                if len(writes) != int(shared) or any(
                        r['request'] != rows[0]['response_id'] or r['error'] != 'none'
                        for r in writes):
                    raise RuntimeError('exact seed policy')
                for i, row in enumerate(rows):
                    entry = restores[row['response_id']]
                    row.update(cached_tokens=int(entry['prefix']), cache_source=entry['source'])
                    if row['cached_tokens'] != (8192 if shared and i else 0):
                        raise RuntimeError('exact cache prefix')
                item = dict(shared=shared, rows=rows,
                    makespan=max(r['end'] for r in rows)-min(r['start'] for r in rows),
                    output_tokens=sum(r['output_tokens'] for r in rows))
                save(folder/'result.json', item)
                results.append(item)
                save(out/'results.json', results)
            resumes = []
            for shared in step['shared_order']:
                key = name+str(shared)
                row = seed.old.eval_one(plan, plan['resume_case'],
                    out/('resume-'+str(shared)), env, check,
                    {'session_id': seed.capability(key+'0')})
                raw = (out/'server.log').read_text()
                restored = [r for r in parse(raw, '[session-cache] request=')
                            if r['event'] == 'restore' and r['request'] == row['response_id']]
                if (len(restored) != 1 or int(restored[0]['prefix']) != 131323 or
                        restored[0]['source'] != 'session' or row['text'].strip() != '44'):
                    raise RuntimeError('overwritten long-slot session restore/quality')
                row['cached_tokens'] = int(restored[0]['prefix'])
                resumes.append(row)
                save(out/'restores.json', resumes)
            check()
            with urllib.request.urlopen(f"http://127.0.0.1:{plan['port']}/healthz", timeout=5) as response:
                health = json.load(response)
            save(out/'health.json', health)
            if not health['gpu_healthy'] or health['seq_slots_free'] != 4:
                raise RuntimeError('post-restore health')
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http.event(out, 'server_before_shutdown', pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                http.event(out, 'server_after_shutdown', pid=server.pid,
                           server_returncode=server.returncode)
            save(out/'exit.json', dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server is not None and server.returncode == 0,
                failure=failure))


def audit(out, count):
    seed.audit(out, count)
    step = json.loads((out/'step.json').read_text())
    records = parse((out/'server.log').read_text(), '[session-save-timing] ')
    if len(records) != 11 or any(r['success'] != '1' or r['valid'] != '1'
                               for r in records):
        raise RuntimeError('save timing ledger')
    for r in records:
        if (int(r['preallocated']) != step['preallocate'] or
                int(r['pipeline']) != step['pipeline'] or r['fallback'] != '0'
                or abs(float(r['residual_ms'])) > 100
                or float(r['residual_ms']) < -1
                or float(r['residual_cpu_ms']) < -1):
            raise RuntimeError('pipeline or timing closure')
    writes = sum(int(r['disk_bytes']) for r in records)
    if writes > 28*1024**3:
        raise RuntimeError('service logical write budget')
    save(out/'timing-audit.json', dict(passed=True, logical_write_bytes=writes,
         records=records, cpu_contract_write_upper_bound=256*1024**2))


if __name__ == '__main__':
    reuse.validate = validate
    reuse.custom_step = custom_step
    reuse.admission.audit_server = audit
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--custom-step')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    preparation = None
    if not args.custom_step:
        legacy.require_guard()
        legacy.verify_files(plan)
        spec = importlib.util.spec_from_file_location('save_io', ROOT/'tools/prefix_seed/precondition.py')
        io = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(io)
        preparation = io.prepare(args.plan.parent, plan['task_deadline_unix'])
    try:
        reuse.main()
    finally:
        if preparation is not None:
            io.release(preparation, args.plan.parent)
