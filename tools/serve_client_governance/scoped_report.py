"""Independent bounded quota evidence; no default installation or full acceptance."""
import argparse
from dataclasses import asdict
import io
import json
import os
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_client_governance import cpu, stage as s, reuse, report as archive
from tools.serve_cuda_quota_integration.verify import custody

BASE, OUTPUT, PACKAGE = s.BASE, s.OUTPUT, s.PACKAGE


def audit():
    plan = s.check()
    s.h.verify_seal(OUTPUT)
    cpu.require(cpu.read(OUTPUT/'finalized.json')['status'] ==
                'SCOPED_VERIFIED_PENDING_DISABLED_CHECK', 'scoped receipt')
    s.a.verify_hashes(ROOT/'build', plan['old_hashes'])
    built = cpu.read(BASE/s.BUILD_LABEL/'result.json')
    gate = cpu.read(BASE/s.CPU_LABEL/'result.json')
    cpu.require(built['passed'] and built['zero_warnings'] and gate['passed'], 'build and CPU')
    reused = reuse.check()
    cpu.require(reused == cpu.read(PACKAGE/'reused-evidence.json'), 'reuse changed')
    folder = OUTPUT/'business'
    result = cpu.read(folder/'result.json')
    cpu.require(result['status'] == 'PASS_BOUNDED_CLIENT_GOVERNANCE_SCOPED' and
                result['counts'] == dict(starts=1, generation_attempts=12, controls=24) and
                result['successes'] == 8 and result['cancelled'] == 2, 'bounded response counts')
    responses, logical, prompts, generated = [], {'alpha': 0, 'beta': 0}, 0, 0
    for path in sorted(folder.glob('*-response.json')):
        label = path.name[:-len('-response.json')]
        request = cpu.read(folder/(label+'-request.json'))
        body = request['body']
        cpu.require(cpu.read(folder/(label+'-http.json'))['status'] == 200, 'success HTTP')
        raw = (folder/(label+'-wire.bin')).read_bytes()
        value = (s.s.a.read_sse if body['stream'] else s.s.a.read_json)(io.BytesIO(raw))
        recorded = cpu.read(path)
        recorded.pop('elapsed', None)
        cpu.require(asdict(value) == recorded and value.id == body['request_id'] and
                    value.finish_reason == 'stop' and value.message['content'].strip() == '710003' and
                    value.usage['completion_tokens'] <= body['max_tokens'], 'raw completion/quality')
        logical['beta' if request['client'] == 1 else 'alpha'] += value.usage['total_tokens']
        prompts += value.usage['prompt_tokens']
        generated += value.usage['completion_tokens']
        responses.append(dict(label=label, usage=value.usage))
    cpu.require(len(responses) == 8, 'response inventory')
    for label in ('active', 'a3'):
        body = cpu.read(folder/(label+'-request.json'))['body']
        s.s.cancelled_wire((folder/(label+'-wire.bin')).read_bytes(), body['request_id'])
    log = (folder/'scoped-model/stderr.log').read_text()
    order = s.admitted(log)
    expected = [cpu.read(folder/(label+'-request.json'))['body']['request_id']
                for label in ('active', 'b1', 'a1', 'a2')]
    cpu.require(order[2:6] == expected and
                cpu.read(folder/'a3-request.json')['body']['request_id'] not in order, 'slot order/cancelled waiter')
    metric = lambda label: s.s.parse_metrics((folder/(label+'-wire.bin')).read_text())
    before = metric('before-queued-cancel')
    after = s.s.parse_metrics(archive.latest_poll(folder, 'after-queued-cancel').read_text())
    preflight = cpu.read(folder/'a3-preflight-wire.bin')
    refunded = preflight['prompt_tokens']+preflight['output']['available_tokens']
    cpu.require(s.rate(before, 'alpha', 'reserved_tokens')-s.rate(after, 'alpha', 'reserved_tokens') == refunded and
                s.rate(before, 'alpha', 'settled_tokens_total') == s.rate(after, 'alpha', 'settled_tokens_total'), 'queued refund')
    before, after = metric('quota-before'), metric('quota-after')
    for label, code, client, kind in (
        ('rpm-denied', 'client_rpm_exhausted', 'alpha', 'rpm'),
        ('tpm-denied', 'client_tpm_exhausted', 'beta', 'tpm')):
        meta = cpu.read(folder/(label+'-http.json'))
        cpu.require(meta['status'] == 429 and cpu.read(folder/(label+'-wire.bin'))['error']['code'] == code,
                    'pre-SSE rate refusal')
        key = f'q4t_client_rate_rejected_total{{client="{client}",kind="{kind}"}}'
        cpu.require(after[key]-before[key] == 1, 'refusal reason metric')
    for client in ('alpha', 'beta'):
        for field in ('requests', 'settled_tokens_total', 'reserved_tokens'):
            cpu.require(s.rate(before, client, field) == s.rate(after, client, field), 'refused request charged')
    before, expired = metric('window-before'), metric('window-expired')
    cpu.require(all(s.rate(before, c, 'window_remaining_ms') > 0 and
        all(s.rate(expired, c, f) == 0 for f in ('requests', 'tokens', 'reserved_tokens'))
        for c in ('alpha', 'beta')), 'observed real rollover')
    final = s.s.parse_metrics(archive.latest_poll(folder, 'quota-final').read_text())
    active_prompt = cpu.read(folder/'active-preflight-wire.bin')['prompt_tokens']
    active_generated = final['q4t_generation_tokens_total']-generated
    cpu.require(final['q4t_prompt_tokens_total']-prompts == active_prompt and
                0 < active_generated <= 2048, 'active cancellation independent logical usage')
    cpu.require(s.rate(final, 'alpha', 'settled_tokens_total') == logical['alpha']+active_prompt+active_generated and
                s.rate(final, 'beta', 'settled_tokens_total') == logical['beta'], 'per-client actual settlement')
    for client in ('alpha', 'beta'):
        cpu.require(s.rate(final, client, 'reserved_tokens') == 0 and s.rate(final, client, 'requests') == 1,
                    'terminal refund/new window')
    cpu.require(final['q4t_requests_success_total'] == 8 and final['q4t_requests_aborted_total'] == 2 and
                final['q4t_structured_workers_started_total'] == final['q4t_structured_workers_reaped_total'] == 0,
                'logical request accounting')
    for name in s.s.a.EMPTY:
        cpu.require(final[name] == 0, 'idle resource: '+name)
    cpu.require(final['q4t_seq_slots_free'] == final['q4t_gpu_healthy'] == 1, 'slot/health')
    rows = [json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    observations = [r for r in rows if r['event'] == 'monitor']
    cpu.require(cpu.read(OUTPUT/'lease/exit.json')['status'] == 'COMPLETED' and
                rows[0]['event'] == 'lease_acquired' and rows[-1]['event'] == 'lease_released' and
                observations and all(not r['conflicts'] and r['unreadable_processes'] == 0 for r in observations), 'guard')
    duration = rows[-1]['monotonic']-rows[0]['monotonic']
    cpu.require(duration < 300 and duration+reused['lease_seconds'] < 480, 'cumulative lease budget')
    owned = []
    for root in (reuse.FIRST, OUTPUT):
        for path in (root/'business').glob('*/identity.json'):
            old = cpu.read(path)
            try:
                now = custody.proc(old['pid'])
            except FileNotFoundError:
                now = None
            cpu.require(now is None or now['start_ticks'] != old['start_ticks'], 'child retained')
            owned.append(dict(pid=old['pid'], start_ticks=old['start_ticks'], gone=True))
    post = cpu.read(BASE/'post-scoped/result.json')
    cpu.require(post['passed'] and len(owned) == 4, 'postflight')
    return dict(status='SCOPED_ACCEPTED_PENDING_DISABLED_CHECK', installed=plan['old_hashes'],
        default_unchanged=True, candidate_sha256=plan['candidate_sha256'], protocol_sha256=plan['protocol'],
        source_commit=plan['source_commit'], models=2, generation_attempts=16, successful_responses=12,
        reused_responses=4, real_tools=2, cancelled=2, refusals=2, responses=responses, reused=reused,
        queue_order=expected, queued_refunded_tokens=refunded,
        active_cancel_usage=dict(prompt=active_prompt, generated=active_generated),
        final_settled_tokens={c: s.rate(final, c, 'settled_tokens_total') for c in ('alpha', 'beta')},
        handler_elapsed_ms=[float(v) for v in re.findall(r'event=end [^\n]* elapsed_ms=([0-9.]+)', log)],
        lease_seconds=duration, cumulative_lease_seconds=duration+reused['lease_seconds'],
        lease_observations=len(observations), resources=post, owned=owned,
        numerical_reuse=cpu.read(PACKAGE/'numerical-reuse.json'), cpu_checks=gate['checks'],
        pending=['no-policy actual JSON/SSE check', 'default release installation and CLI health'],
        long_runs=False, performance_acceptance=False, managed_lifecycle_accepted=False,
        physical_ram_hard_limit=False, actual_lan_accepted=False)


def bind():
    archive.audit = audit
    archive.__file__ = __file__


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('audit', 'archive', 'verify'))
    args = parser.parse_args()
    bind()
    if args.action == 'audit':
        print(json.dumps(audit(), indent=2))
    else:
        getattr(archive, args.action)()
