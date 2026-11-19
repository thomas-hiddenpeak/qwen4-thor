"""Qualify actual Realtime success/quota/cancel diagnostics, not audio quality anew."""
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
from tools.serve_realtime import vad_acceptance as vad
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_service.package import require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              committed_items=3, chats=1, controls=2000, seconds=600)


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed sources before freezing')
    previous = base.WORK / 'noise-business-02'
    require(json.loads((previous / 'run/result.json').read_text())['status'] == 'PASS'
            and json.loads((previous / 'lease/exit.json').read_text())['status'] == 'COMPLETED',
            'actual noise business not qualified')
    plan = json.loads((previous / 'run/plan.json').read_text())
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'default drift')
    out = args.output.resolve()
    require(out.is_relative_to(base.WORK), 'private phase directory required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous / 'run/client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client['requests_per_minute'] = 1 if client['id'] == 'alpha' else 10
    save(out / 'client-policy.json', policy)
    (out / 'client-policy.json').chmod(0o600)
    tls = certificate(out / 'credentials')
    binary = args.binary.resolve(strict=True)
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/release'], cwd=ROOT, text=True).splitlines()
    plan.pop('continuation', None)
    plan.update(schema='q4t-realtime-diagnostics-business-v1', limits=LIMITS,
        output=str(out), port=args.port, operations_port=args.port + 1,
        public_port=args.port + 2, binary=str(binary), binary_sha256=sha(binary),
        sources={n: sha(ROOT / n) for n in sources},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        client_policy_sha256=sha(out / 'client-policy.json'), tls=tls,
        tls_files={tls[n]: sha(tls[n]) for n in ('certificate', 'key')},
        acceptance=dict(success='success/execution_complete',
            quota='error/client_rpm_exhausted', disconnect='aborted/client_disconnected',
            expected_requests=4, expected_success=2, expected_error=1, expected_aborted=1,
            delta_before_cancel=True, no_fake_success=True, same_core_pid=True),
        scope='Object execution observation only: one real success, one RPM rejection, one disconnect after actual delta and same-PID Chat. Reuse fixed noise/ASR/SDK evidence; not full quality/performance/Guardian qualification.')
    save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        super().__init__(plan)
        require(plan['schema'] == 'q4t-realtime-diagnostics-business-v1', 'phase schema')
        require(sha(Path(plan['vad_directory']) / vad.WEIGHT) == vad.DIGEST,
                'VAD fixed asset drift')

    def extra_model_args(self):
        return ['--vad-model-dir', self.plan['vad_directory']]

    def diagnostic(self, item, outcome, reason):
        rows = [line for line in (self.out / 'model.log').read_text().splitlines()
                if re.search(r'\bid=' + re.escape(item) + r'\b', line) and
                'event=end ' in line]
        require(len(rows) == 1 and
                f'outcome={outcome} reason={reason} ' in rows[0],
                'actual executor diagnostic classification differs: ' + item)
        return rows[0]

    def business(self):
        self.snapshot('before')
        setting = base.settings()
        setting['audio']['input']['noise_reduction'] = dict(type='near_field')
        zh = dict(file=self.plan['clips']['zh']['path'], settings=setting)
        result = self.sdk('success-quota', 'alpha',
                          [zh, dict(zh, expect_failure='client_rpm_exhausted')])
        success, rejected = result['turns']
        base.quality(success['terminal']['transcript'], 'zh')
        base.usage(success['terminal'], sdk=True)
        state = self.snapshot('alpha-drained')
        require(state['q4t_client_rate_admitted_total{client="alpha"}'] == 1 and
                state['q4t_client_rate_rejected_total{client="alpha",kind="rpm"}'] == 1 and
                state['q4t_client_rate_settled_tokens_total{client="alpha"}'] ==
                success['terminal']['usage']['total_tokens'] and
                state['q4t_client_rate_reserved_tokens{client="alpha"}'] == 0,
                'success/quota settlement')
        observed = [self.diagnostic(success['item_id'], 'success', 'execution_complete'),
                    self.diagnostic(rejected['item_id'], 'error', 'client_rpm_exhausted')]
        cancelled = self.sdk('cancel-after-delta', 'beta', [dict(
            file=self.plan['clips']['en']['path'], settings=setting,
            cancel_after_delta=True)])
        turn = cancelled['turns'][0]
        require(turn['cancelled'], 'cancelled turn absent')
        state = self.snapshot('beta-drained')
        require(state['q4t_client_rate_reserved_tokens{client="beta"}'] == 0 and
                state['q4t_client_rate_settled_tokens_total{client="beta"}'] > 0,
                'cancelled actual work must settle and release')
        observed.append(self.diagnostic(turn['item_id'], 'aborted', 'client_disconnected'))
        self.core('core-after')
        state = self.snapshot('final-drained')
        for name, count in [('total', 4), ('success_total', 2),
                             ('error_total', 1), ('aborted_total', 1)]:
            require(state['q4t_requests_' + name] == count, 'request outcome metrics: ' + name)
        save(self.out / 'observed-diagnostics.json', dict(rows=observed,
             scope='Executor outcome, not final socket delivery or automatic recovery.',
             success_tokens=success['terminal']['usage']['total_tokens']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--port', type=int, default=18358)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()
