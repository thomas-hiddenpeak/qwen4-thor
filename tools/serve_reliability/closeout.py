"""Bounded header repair and discriminating private-cache publication checks."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/serve-closeout-20261031'
PRIOR = ROOT / '.q4t-work/serve-reliability-20261030'
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
from serve_reliability import Runner, capability, decoded, fields, save, sha, turn
import gpu_exclusive
import nvml_process_query


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


helpers = module('closeout_delivery_helpers',
                 ROOT / 'tools/verify/startup_delivery/deploy.py')


def validate_probe(saved, continuation):
    """Mirror the documented *input domain*, not the cache's publication state."""
    if len(saved) < 2 or len(continuation) < len(saved):
        raise AssertionError('probe is shorter than the saved prefix/pending')
    if saved[:-1] != continuation[:len(saved)-1]:
        raise AssertionError('probe does not match actual saved token prefix')
    return len(saved)-1


def validate_pair(boundary, positive, negative):
    if positive != boundary or negative != 0:
        raise AssertionError('private publication positive/negative contrast')


def public_save_seen(raw, request):
    return any(r.get('request') == request and r.get('event') == 'save' and
               r.get('prefix') == '8192' and r.get('error') == 'none'
               for r in fields(raw, '[prefix-cache] '))


def prepare():
    assert not (BASE / 'plan.json').exists()
    old = json.loads((PRIOR / 'http-plan.json').read_text())
    frozen = BASE / 'frozen'
    frozen.mkdir()
    for source, name in [('build/q4t', 'candidate'), ('fault-build/q4t', 'fault'),
                         ('build/CMakeCache.txt', 'CMakeCache.txt'),
                         ('fault-build/CMakeCache.txt', 'FaultCMakeCache.txt')]:
        shutil.copy2(BASE / source, frozen / name)
    shutil.copy2(PRIOR / 'tokenizer-candidate', frozen / 'tokenizer')
    for name in ['header-contract.log', 'oracle-tests.log', 'build.log',
                 'build-fault.log']:
        assert (BASE / name).is_file()
    assert json.loads((BASE / 'host/result.json').read_text())['passed']
    for name in ['build.log', 'build-fault.log']:
        raw = (BASE / name).read_text()
        assert 'warning:' not in raw and 'error:' not in raw
    assert '3 tests, 3 passed, 0 failed, 0 skipped' in (BASE / 'header-contract.log').read_text()
    assert '\nOK\n' in (BASE / 'oracle-tests.log').read_text()
    runtime = {str(p.relative_to(ROOT)): sha(p) for top in ['src', 'include']
               for p in (ROOT / top).rglob('*') if p.is_file()}
    parent = json.loads((BASE / 'baseline/runtime-source.json').read_text())
    delta = sorted(k for k in runtime if runtime[k] != parent.get(k))
    assert delta == ['include/q4t/server/http_response.h',
                     'include/q4t/server/serve_fault.h',
                     'src/server/chat_generation.cpp', 'src/server/http_response.cpp']
    save(frozen / 'runtime-source.json', runtime)
    save(BASE / 'dependency-review.json', dict(changed=delta,
        unchanged=len(runtime)-len(delta), reused_prior_http=102,
        correction='Prior short private probe is not a publication negative. '
                   'Retain the original evidence; replace that inference.',
        reuse='Model math, tokenizer, scheduling, cache layout/storage unchanged. '
              'New HTTP covers header progress and actual private publication.'))
    meta = json.loads((BASE / 'baseline/q4t.release.json').read_text())
    plan = dict(model=old['model'], artifact=meta['subset_artifact'],
        port=8764, quality=old['quality'], cache_prompt=old['cache_prompt'],
        candidate=str(frozen / 'candidate'), fault=str(frozen / 'fault'),
        tokenizer=str(frozen / 'tokenizer'), output=str(BASE / 'http'),
        performance_acceptance=False, maximum_http=40, maximum_starts=13,
        measurement_http=30, measurement_starts=9, health_http=6, health_starts=2,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                             cwd=ROOT, text=True).strip(), steps=[])
    for point, mode in [('before_header', 'full'), ('partial_header', 'subset'),
                        ('after_header', 'full'), ('after_finish', 'subset')]:
        plan['steps'].append(dict(name='header-'+point, case='header',
            kind='cache', mode=mode, binary='fault', point=point, requests=2,
            environment={'Q4T_SERVE_FAULT': 'handler.'+point,
                         'Q4T_SERVE_REQUEST': 'unused-request'}))
    plan['steps'].append(dict(name='plain', case='plain', kind='quality',
                             mode='plain', binary='candidate', requests=2))
    for mode in ['full', 'subset']:
        for incremental in [0, 1]:
            name = f'private-{mode}-{incremental}'
            plan['steps'].append(dict(name=name, case='private', kind='cache',
                mode=mode, binary='fault', requests=5,
                environment={'Q4T_SESSION_INCREMENTAL': str(incremental),
                             'Q4T_SERVE_PREFIX_IDS': '1',
                             'Q4T_SERVE_REQUEST': name+'-failed'}))
    assert sum(s['requests'] for s in plan['steps']) == 30
    paths = list(frozen.iterdir()) + [ROOT / n for n in runtime]
    paths += [Path(__file__), Path(__file__).with_name('header_contract.cpp'),
              Path(__file__).with_name('test_closeout.py'),
              ROOT / 'tools/evalscope/serve_reliability.py',
              ROOT / 'tools/evalscope/gpu_exclusive.py',
              ROOT / 'tools/evalscope/nvml_process_query.py',
              ROOT / 'tools/evalscope/monitor_memory.py',
              ROOT / 'tools/evalscope/resource_metrics.py',
              ROOT / 'docs/SERVE_CLOSEOUT_2026-10-31.md', ROOT / 'CMakeLists.txt',
              BASE / 'dependency-review.json', BASE / 'host/result.json']
    paths += [BASE / n for n in ['header-contract.log', 'oracle-tests.log',
                                 'build.log', 'build-fault.log']]
    paths += [Path(plan['artifact']) / n for n in ['head.bf16', 'token_ids.bin', 'manifest.json']]
    paths += [Path(plan['model']) / n for n in ['config.json', 'tokenizer.json', 'model.safetensors.index.json']]
    paths += [PRIOR / 'http' / n / 'cache.json' for n in ['cache-full-0', 'cache-subset-0']]
    paths.append(PRIOR / 'http/candidate-plain/quality.json')
    plan['files'] = {str(p): sha(p) for p in paths}
    plan['started_unix'] = time.time()
    plan['deadline_unix'] = plan['started_unix'] + 90*60
    helpers.save(BASE / 'plan.json', plan)
    print(json.dumps(dict(http=36, starts=11, maximum_http=40, maximum_starts=13,
                         deadline_unix=plan['deadline_unix'])))


class Closeout(Runner):
    def charge(self, kind, label):
        super().charge(kind, label)
        if self.http_count > 30 or self.starts > 9:
            raise RuntimeError('fixed measurement budget')

    def normal(self, folder, label, body):
        result = self.http(folder, label, body)
        assert 'transport_error' not in result
        return decoded(result)

    def endings(self, folder, label):
        self.wait(lambda: any(r.get('id') == label and r['event'] == 'end'
            for r in fields((folder / 'server.log').read_text(), '[q4t][request] ')))
        rows = [r for r in fields((folder / 'server.log').read_text(), '[q4t][request] ')
                if r.get('id') == label and r['event'] == 'end']
        assert len(rows) == 1 and rows[0]['released'] == '1'
        return rows[0]

    def metric_success(self, folder, label, expected):
        result = self.http(None, 'metrics', path='/metrics')
        raw = bytes.fromhex(result['wire_hex']).decode()
        assert f'\nq4t_requests_success_total {expected}\n' in raw
        save(folder / (label+'-metrics.json'), result)

    def header(self, step, folder):
        name = step['name']; label = name+'-fault'
        extra = dict(session_id=capability(name+'session'),
                     prefix_cache_scope=capability(name+'scope'),
                     prefix_cache_write_tokens=8192)
        body = self.body(label, self.plan['cache_prompt'], maximum=16, **extra)
        sock = self.raw(folder, label, body)
        wire = bytearray()
        try:
            sock.settimeout(180)
            while chunk := sock.recv(65536):
                self.check(); wire.extend(chunk)
        finally:
            sock.close()
        save(folder / (label+'-wire.json'), dict(wire_hex=wire.hex()))
        if step['point'] == 'partial_header':
            assert wire == b'HTTP/1.1'
        else:
            header, sep, payload = wire.partition(b'\r\n\r\n')
            assert sep and header.startswith(b'HTTP/1.1 ')
            assert wire.count(b'HTTP/1.1 ') == 1
            status = int(header.split(b' ', 2)[1])
            response = dict(stream=True, status=status, wire_hex=payload.hex())
            if step['point'] == 'before_header':
                assert status == 500 and b'data:' not in payload
                decoded(response, success=False)
            elif step['point'] == 'after_header':
                assert status == 200
                decoded(response, success=False)
            else:
                assert status == 200 and b'[DONE]' not in payload and b'"error"' not in payload
                events = [json.loads(line[6:]) for line in payload.splitlines()
                          if line.startswith(b'data: ')]
                assert sum(c.get('finish_reason') is not None for e in events
                           for c in e.get('choices', [])) == 1
                try:
                    decoded(response)
                except AssertionError:
                    pass
                else:
                    raise AssertionError('truncated terminal accepted')
        self.free()
        assert self.endings(folder, label)['outcome'] == 'error'
        self.metric_success(folder, label, 0)
        assert public_save_seen((folder / 'server.log').read_text(), label)
        label = name+'-normal'
        body = self.body(label, self.plan['cache_prompt'], maximum=16, **extra)
        body['request_id'] = name+'-fault'  # Same ID, new cancellation credential.
        result = self.normal(folder, label, body)
        expected = json.loads((PRIOR / 'http' / ('cache-'+step['mode']+'-0') /
                               'cache.json').read_text())['seed']
        assert all(result[k] == expected[k] for k in ['text', 'usage', 'finish'])
        assert result['usage']['prompt_tokens_details']['cached_tokens'] == 0
        self.free()
        self.metric_success(folder, label, 1)
        save(folder / 'header-check.json', dict(passed=True, point=step['point'],
             public_negative=True, private_negative_claim=False, normal=result))

    def prefix(self, folder, label):
        start = '[serve-test-prefix] request='+label+' tokens='
        rows = [json.loads(line[len(start):]) for line in
                (folder / 'server.log').read_text().splitlines() if line.startswith(start)]
        assert len(rows) == 1
        return rows[0]

    def private(self, step, folder):
        name = step['name']; prompt = self.plan['cache_prompt']
        reference_session, failed_session = capability(name+'reference'), capability(name+'failed')
        scope = capability(name+'public')
        label = name+'-reference'
        reference = self.normal(folder, label, self.body(label, prompt, False, 16,
                                                       session_id=reference_session))
        self.free()
        saved_prefix = self.prefix(folder, label)
        continuation = prompt+reference['text']+turn('Reply with exactly 44.')
        tokenized = subprocess.run([self.plan['tokenizer'], self.plan['model']+'/tokenizer.json'],
            input=json.dumps(continuation, ensure_ascii=False)+'\n', capture_output=True,
            text=True, check=True, timeout=30)
        ids = json.loads(tokenized.stdout)
        boundary = validate_probe(saved_prefix, ids)
        assert boundary > reference['usage']['prompt_tokens']
        label = name+'-positive'
        positive = self.normal(folder, label, self.body(label, continuation,
                                                       session_id=reference_session))
        self.free()
        hit = positive['usage']['prompt_tokens_details']['cached_tokens']
        assert hit == boundary and positive['text'].strip() == '44'
        label = name+'-failed'
        body = self.body(label, prompt, False, 16, session_id=failed_session,
                        prefix_cache_scope=scope, prefix_cache_write_tokens=8192)
        sock = self.raw(folder, label, body)
        try:
            self.wait(lambda: (folder / 'gate.entered').exists(), 150)
            failed_prefix = self.prefix(folder, label)
            assert failed_prefix == saved_prefix
            assert validate_probe(failed_prefix, ids) == boundary
            save(folder / 'prefix-proof.json', dict(reference=saved_prefix,
                failed=failed_prefix, continuation=ids, boundary=boundary,
                tokenizer_sha256=sha(self.plan['tokenizer']),
                reference_session=reference_session, failed_session=failed_session))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            sock.close()
            (folder / 'gate.release').touch(exist_ok=False)
            self.free()
        finally:
            sock.close()
        assert self.endings(folder, label)['outcome'] == 'cancelled'
        self.metric_success(folder, label, 2)
        label = name+'-negative'
        probe = self.body(label, continuation, session_id=failed_session)
        assert 'prefix_cache_scope' not in probe and 'prefix_cache_write_tokens' not in probe
        negative = self.normal(folder, label, probe)
        self.free()
        miss = negative['usage']['prompt_tokens_details']['cached_tokens']
        validate_pair(boundary, hit, miss)
        assert negative['text'].strip() == '44'
        label = name+'-public-negative'
        public = self.normal(folder, label, self.body(label, prompt, maximum=16,
                                                    prefix_cache_scope=scope))
        self.free()
        assert public['usage']['prompt_tokens_details']['cached_tokens'] == 0
        assert public['text'] == reference['text']
        save(folder / 'cache-check.json', dict(passed=True, boundary=boundary,
            positive=positive, negative=negative, public=public,
            reference=reference, actual_token_prefix_verified=True,
            negative_precedes_any_successful_replacement=True))

    def plain(self, step, folder):
        fixture = self.plan['quality']['1024']
        prior = json.loads((PRIOR / 'http/candidate-plain/quality.json').read_text())
        rows = []
        for stream in [False, True]:
            label = 'plain-'+str(int(stream))
            result = self.normal(folder, label, self.body(label, fixture['prompt'], stream))
            expected = next(r for r in prior if r['length'] == 1024 and r['stream'] == stream)
            assert all(result[k] == expected[k] for k in ['text', 'usage', 'finish'])
            self.free(); rows.append(result)
        save(folder / 'quality.json', rows)

    def run(self):
        for step in self.plan['steps']:
            before = self.http_count
            with self.service(step) as (folder, _):
                getattr(self, step['case'])(step, folder)
            assert self.http_count-before == step['requests']
            raw = (folder / 'server.log').read_text()
            finalized = [json.loads(line.split('] ', 1)[1]) for line in raw.splitlines()
                         if line.startswith('[startup-reservation] ') and '"event":"finalized"' in line]
            assert len(finalized) == 1 and finalized[0]['status'] == 'PASS'
            assert finalized[0]['handed_live'] == finalized[0]['unhanded_remaining'] == 0
            self.completed.append(step['name'])
            print(step['name']+': PASS', flush=True)
        final = nvml_process_query.query(timeout=5)
        save(self.out / 'final-gpu.json', final)
        assert final['status'] == 'OK' and not final['pids']
        assert self.http_count == 30 and self.starts == 9
        save(self.out / 'result.json', dict(passed=True, http=30, starts=9,
             completed=self.completed, finished=time.time()))


def guarded():
    lease = json.loads(gpu_exclusive.common_lock().read_text())
    assert lease.get('state') == 'held' and lease['pid'] == os.getppid()


def run(plan_path):
    guarded()
    plan = json.loads(Path(plan_path).read_text())
    for path, digest in plan['files'].items():
        assert sha(path) == digest, path
    Closeout(plan).run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run'])
    parser.add_argument('--plan', type=Path, default=BASE / 'plan.json')
    args = parser.parse_args()
    prepare() if args.action == 'prepare' else run(args.plan)
