"""One real Chat with a changed private SIP configuration at engine startup."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import responses_acceptance as live
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service import package

LIMITS = dict(starts=1, ingress_starts=1, chats=1, controls=2000, seconds=300)


class Stage(base.Stage):
    limits = LIMITS

    def extra_model_args(self):
        return package.live_transport_args(self.plan)

    def business(self):
        for _ in range(100):
            status, raw = self.http('/v1/config')
            package.require(status == 200, 'config unavailable')
            sip = json.loads(raw)['sip']
            if sip['activation_failures']:
                break
            time.sleep(.05)
        package.require(sip['activation_failures'] == 1 and
                        sip['configured'] == sip['ready'] == 0, 'trunk identity check')
        self.core('chat-after-sip-failure')
        self.same_model()
        package.save(self.out/'business.json', dict(sip=sip, chat='42', same_model=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'finalize'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qualification', type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory')
    if args.action == 'prepare':
        package.require(not out.exists(), 'fresh stage required')
        live.prepare(out, args.qualification)
        plan = package.read(out/'plan.json')
        path = out/'trunks.json'
        row = dict(client_id='alpha', listen_ip='127.0.0.1', port=18814,
                   peer_subject='carrier', certificate_dir=str(out/'certificates'))
        package.save(path, dict(trunks=[row]))
        path.chmod(0o600)
        binding = package.live_transport_binding(plan['binary'], '127.0.0.1', path)
        # The altered file remains syntactically valid. The receipt alone must
        # prevent its listener from being configured.
        row['port'] += 1
        package.save(path, dict(trunks=[row]))
        path.chmod(0o600)
        changed = package.live_transport_binding(plan['binary'], '127.0.0.1', path)
        package.require(changed['sip_trunks_sha256'] != binding['sip_trunks_sha256'],
                        'fixture did not change')
        plan.update(limits=LIMITS, schema='q4t-managed-live-trunk-isolation-v1',
                    live_transport=binding, changed_sha256=changed['sip_trunks_sha256'])
        package.save(out/'plan.json', plan)
        return
    plan = package.read(out/'plan.json')
    package.require(plan['schema'] == 'q4t-managed-live-trunk-isolation-v1' and
                    package.sha(out/'trunks.json') == plan['changed_sha256'], 'plan drift')
    if args.action == 'run':
        with base.lifecycle.child_subreaper():
            Stage(plan).run()
        return
    result = package.read(out/'result.json')
    counts = result['counts']
    package.require(result['status'] == 'PASS' and result['default_unchanged'] and
                    result['seconds'] <= LIMITS['seconds'] and
                    all(counts[k] == 1 for k in ('starts','ingress_starts','chats')),
                    'business or limit failed')
    env = qualify.environment(package.read(out/'lease/exit.json'),
        [json.loads(s) for s in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
        [package.read(out/'cleanup.json')], expected_starts=1)
    package.save(out/'acceptance.json', dict(environment=env, result=result,
        business=package.read(out/'business.json'),
        changed_sha256=plan['changed_sha256'], pinned_sha256=plan['live_transport']['sip_trunks_sha256']))


if __name__ == '__main__':
    main()
