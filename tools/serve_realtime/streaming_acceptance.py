"""Bounded real continuous ASR, withholding future audio until actual delta."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

# The inherited counter reserves item ATTEMPTS before each SDK invocation.
# Five commits, one precommit clear and one precommit quota rejection are planned.
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=3,
              committed_items=7, chats=1, controls=2000, seconds=900)
CHUNKS = dict(minimal=8000, low=16000, medium=32000, high=64000, xhigh=128000)


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed sources before freezing')
    previous = base.WORK / 'transcription-business-04'
    require(json.loads((previous / 'run/result.json').read_text())['status'] == 'PASS'
            and json.loads((previous / 'lease/exit.json').read_text())['status'] == 'COMPLETED',
            'fixed clean audio business is not qualified')
    plan = json.loads((previous / 'run/plan.json').read_text())
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'default drift')
    cpu = base.WORK / 'stream-input-cpu-01'
    sdk = base.WORK / 'stream-sdk-cpu-01'
    for phase in (cpu, sdk):
        require(json.loads((phase / 'result.json').read_text())['status'] == 'PASS',
                'targeted CPU checks not qualified')
    binary = args.binary.resolve(strict=True)
    require(sha(binary) == json.loads((cpu / 'plan.json').read_text())['q4t_sha256'],
            'engine differs from zero-warning CPU build')
    out = args.output.resolve()
    require(out.is_relative_to(base.WORK), 'private phase directory required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'run/client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client['max_output_tokens'] = 256
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    tls = certificate(out / 'credentials')
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    plan.update(schema='q4t-realtime-continuous-business-v1', limits=LIMITS,
        output=str(out), port=args.port, operations_port=args.port + 1,
        public_port=args.port + 2, binary=str(binary), binary_sha256=sha(binary),
        sources={n: sha(ROOT / n) for n in sources},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        client_policy_sha256=sha(out / 'client-policy.json'), tls=tls,
        tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        planned=dict(successful_commits=5, precommit_clear=1, precommit_quota=1,
            native_passes_max=28, input_limit_per_item=4096, output_limit_per_item=256,
            zh_prefix_bytes=144000, en_prefix_bytes=288000, mulaw_prefix_bytes=24000),
        acceptance=dict(zh_exact=True, en_word_error_rate_max=0.05,
            actual_delta_with_future_withheld=True, no_premature_completion=True,
            actual_steps_match_delay=True, original_logprobs=True,
            actual_usage_matches_native_steps_and_client_settlement=True,
            one_rpm_per_item=True, same_connection_after_clear=True,
            same_core_pid=True, no_automatic_replay=True),
        scope='Native cumulative ASR + native TLS + official JS SDK, five real completions and two precommit terminal paths. Fixed-PID admission fixture, not Guardian recovery; no performance/full-quality claim. All master work continues.')
    save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        super().__init__(plan)
        require(plan['schema'] == 'q4t-realtime-continuous-business-v1', 'phase schema')
        self.observed = []

    def extra_model_args(self):
        # ParseServerOptions assigns integer flags in order. The exact command
        # and final config are retained; this overrides the old fixture's 128.
        return ['--asr-max-output-tokens', '256']

    def clip(self, label, delay, *, prefix=None, noise=None, **actions):
        setting = base.settings('audio/pcmu' if label == 'mulaw' else 'audio/pcm')
        setting['audio']['input']['transcription']['delay'] = delay
        setting['audio']['input']['noise_reduction'] = dict(type=noise) if noise else None
        result = dict(file=self.plan['clips'][label]['path'], settings=setting, **actions)
        if prefix is not None:
            result['prefix_bytes'] = prefix
        return result

    def steps(self, item):
        pattern = (r'^\[asr-stream\] id=' + re.escape(item) +
                   r' pass=(\d+) samples=(\d+) prefix=(\d+) input=(\d+) '
                   r'output=(\d+) cumulative_output=(\d+) final=([01])$')
        names = ('pass', 'samples', 'prefix', 'input', 'output', 'cumulative_output', 'final')
        rows = [dict(zip(names, map(int, match.groups()))) for line in
                (self.out / 'model.log').read_text().splitlines()
                if (match := re.match(pattern, line))]
        return rows

    def completed(self, turn, label, delay, prefix=None):
        value = turn['terminal']
        result = base.quality(value['transcript'], 'en' if label == 'en' else 'zh')
        base.usage(value, sdk=True)
        if prefix is not None:
            before = turn['before_commit']
            require(before['sent_bytes'] == prefix < before['total_bytes'] and
                    before['delta'] and before['item_id'] == turn['item_id'],
                    'actual nonempty delta before commit with future withheld')
        rows = self.steps(turn['item_id'])
        fixture = self.plan['clips'][label]
        samples = (fixture['bytes'] * 2 if label == 'mulaw' else
                   (fixture['bytes'] // 2) * 16000 // 24000)
        chunk = CHUNKS[delay]
        expected = list(range(chunk, samples, chunk)) + [samples]
        require([v['samples'] for v in rows] == expected and
                [v['pass'] for v in rows] == list(range(1, len(rows) + 1)),
                'actual cumulative audio extents differ from delay/real length')
        require(rows[-1]['final'] == 1 and all(v['final'] == 0 for v in rows[:-1]),
                'actual final sample pass missing or premature')
        require(all(v['prefix'] == 0 for v in rows[:2]) and
                (len(rows) <= 2 or any(v['prefix'] > 0 for v in rows[2:])),
                'actual fixed decoder continuation absent')
        u = value['usage']
        require(u['input_tokens'] == sum(v['input'] for v in rows) and
                u['output_tokens'] == sum(v['output'] for v in rows) and
                rows[-1]['cumulative_output'] == u['output_tokens'] <= 256,
                'actual repeated work differs from protocol usage')
        self.observed.append(dict(item=turn['item_id'], label=label, delay=delay,
            quality=result, before_commit=turn.get('before_commit'), steps=rows, usage=u))
        save(self.out / 'observed.json', self.observed)
        return u['total_tokens']

    def business(self):
        self.snapshot('before')
        result = self.sdk('alpha', 'alpha', [
            self.clip('zh', 'low', prefix=144000),
            self.clip('zh', 'xhigh'),
            self.clip('zh', 'low', prefix=52800,
                      expect_precommit_failure='client_rpm_exhausted')])
        tokens = self.completed(result['turns'][0], 'zh', 'low', 144000)
        tokens += self.completed(result['turns'][1], 'zh', 'xhigh')
        rejected = result['turns'][2]
        require(rejected['cleared'] and not self.steps(rejected['item_id']),
                'quota rejection unexpectedly performed native pass')
        state = self.snapshot('alpha-drained')
        require(state['q4t_client_rate_admitted_total{client="alpha"}'] == 2 and
                state['q4t_client_rate_rejected_total{client="alpha",kind="rpm"}'] == 1 and
                state['q4t_client_rate_settled_tokens_total{client="alpha"}'] == tokens and
                state['q4t_client_rate_reserved_tokens{client="alpha"}'] == 0,
                'one RPM per item/actual repeated tokens settlement')
        result = self.sdk('beta-clear-reuse', 'beta', [
            self.clip('en', 'medium', prefix=288000),
            self.clip('en', 'medium', prefix=288000, clear_after_delta=True),
            self.clip('zh', 'low', prefix=144000)])
        tokens = self.completed(result['turns'][0], 'en', 'medium', 288000)
        tokens += self.completed(result['turns'][2], 'zh', 'low', 144000)
        cleared = result['turns'][1]
        require(cleared['cleared'] and cleared['before_commit']['delta'] and
                len({v['item_id'] for v in result['turns']}) == 3 and
                cleared['terminal']['error']['code'] == 'explicit_cancel',
                'clear failed to cancel original item on the same connection')
        state = self.snapshot('beta-drained')
        require(state['q4t_client_rate_admitted_total{client="beta"}'] == 3 and
                state['q4t_client_rate_settled_tokens_total{client="beta"}'] > tokens and
                state['q4t_client_rate_reserved_tokens{client="beta"}'] == 0,
                'cancelled actual work must also settle without reservation leak')
        result = self.sdk('gamma-mulaw-near', 'gamma', [
            self.clip('mulaw', 'low', prefix=24000, noise='near_field')])
        tokens = self.completed(result['turns'][0], 'mulaw', 'low', 24000)
        state = self.snapshot('gamma-drained')
        require(state['q4t_client_rate_admitted_total{client="gamma"}'] == 1 and
                state['q4t_client_rate_settled_tokens_total{client="gamma"}'] == tokens and
                state['q4t_client_rate_reserved_tokens{client="gamma"}'] == 0,
                'processed G711 streaming settlement')
        total = sum(len(self.steps(v['item'])) for v in self.observed)
        cancelled = self.steps(cleared['item_id'])
        require(cancelled and all(v['samples'] <= 6 * 16000 for v in cancelled) and
                total + len(cancelled) <= self.plan['planned']['native_passes_max'],
                'cancel read future audio or exceeded actual step bound')
        save(self.out / 'cancelled.json', dict(item=cleared['item_id'], steps=cancelled,
            note='Completed steps only; cancellation during a native step may add actual settled tokens.'))
        self.core('core-after')
        state = self.snapshot('final-drained')
        require(state['q4t_requests_total'] == 8 and
                state['q4t_requests_success_total'] == 6 and
                state['q4t_requests_error_total'] == 1 and
                state['q4t_requests_aborted_total'] == 1, 'actual outcome classification')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final configuration')
        health = json.loads(raw)['native_asr_health']
        require(health['state'] == 'ready' and health['load_attempts'] <= 2 and
                health['compute_failures'] == 0, 'ASR health/reload bound')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--port', type=int, default=18368)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
