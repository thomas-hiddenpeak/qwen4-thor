"""R03 bounded native progress/recovery validation, outside production bundles.

The only acceptance override is an explicit exact-identity validation release.
Production package/CLI verification is unchanged and rejects that release.
"""
import argparse
import copy
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_progress.pause import PausedMain, identity

spec = importlib.util.spec_from_file_location('q4t_progress_common',
    ROOT / 'tools/serve_recovery/acceptance.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
# Reuse the verified lifecycle and cleanup, dispatching explicit test actors.
common.__file__ = __file__
original_load = common.load_modules
LIMITS = dict(starts=2, chats=8, probes=1, controls=2000, seconds=900)
save, Budget = common.save, common.Budget


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def validation_gate(original, expected):
    def check(metadata, decision):
        if metadata.get('validation_only') is not True:
            return original(metadata, decision)
        if (metadata.get('acceptance') != 'VALIDATION_ONLY_R03' or
                decision.get('status') != 'PENDING_REAL_BUSINESS_R03' or
                fingerprint(metadata) != expected['metadata'] or
                fingerprint(decision) != expected['decision']):
            raise ValueError('validation release differs from frozen identity')
    return check


def load_modules(plan=None):
    modules = original_load(plan)
    if plan:
        engine = modules[-1]
        original = getattr(engine, '_r03_original_acceptance', engine.accepted_release)
        engine._r03_original_acceptance = original
        engine.accepted_release = validation_gate(original, plan['validation_release'])
    return modules


def verify(plan):
    bundle, _, _, _, _, engine = load_modules(plan)
    engine.require(plan['scenario'] == 'native_progress' and
        plan['limits'] == LIMITS, 'frozen R03 budget changed')
    for name, digest in plan['sources'].items():
        engine.require(engine.sha(ROOT / name) == digest, 'source changed: '+name)
    for row in plan['protected']:
        engine.require(engine.sha(row['path']) == row['sha256'],
            'protected artifact changed: '+row['path'])
    result = bundle.verify(plan['package']['package'],
        plan['package']['manifest_sha256'])
    engine.require(result['version'] == 5 and
        result['ingress_mode'] == 'native' and not result['tls'],
        'R03 native loopback validation bundle required')
    return result


common.load_modules, common.verify = load_modules, verify


def controller_actor(plan, config):
    load_modules(plan)
    from tools.serve_recovery import controller
    output = Path(plan['output']) / 'execution'
    original = controller.Controller.tick
    last_write = [0]
    dropped = set()

    def tick(actor, **kwargs):
        # The production tick and decisions execute unchanged. Capture bounded
        # actual private observations, never replace a work counter or clock.
        result = original(actor, **kwargs)
        observer = actor.progress
        now = time.monotonic()
        generation = observer.identity.model_generation if observer else 0
        if observer and generation == 2 and generation not in dropped and \
                (output / 'progress-drop').exists():
            snapshot = observer.snapshot()
            save(output / 'progress-drop-applied.json', dict(
                identity=asdict(observer.identity),
                before=asdict(snapshot) if snapshot else None,
                monotonic=now))
            observer.close()
            dropped.add(generation)
        if now - last_write[0] >= .2 or actor.progress_fault is not None:
            snapshot = observer.snapshot() if observer else None
            value = dict(phase=actor.phase, reason=actor.reason,
                monotonic=now, progress_reason=observer.reason if observer else None,
                snapshot=asdict(snapshot) if snapshot else None,
                fault=asdict(actor.progress_fault) if actor.progress_fault else None)
            if generation:
                save(output / ('progress-'+str(generation)+'.json'), value)
            if actor.progress_fault is not None:
                save(output / 'progress-fault-confirmed.json', value)
            last_write[0] = now
        return result

    controller.Controller.tick = tick
    return common.controller_actor(plan, config)


class Stage(common.Stage):
    def captured(self, generation):
        path = self.output / ('progress-'+str(generation)+'.json')
        if not path.exists():
            return None
        value = self.engine.read(path)
        return value if time.monotonic()-value['monotonic'] <= 2 else None

    def ordinary(self, label):
        for suffix in ('json', 'sse'):
            answer = self.request(label+'-'+suffix,
                copy.deepcopy(self.plan['requests'][label+'-'+suffix]))
            self.engine.require(answer.finish_reason == 'stop' and
                answer.message['content'].strip() == '42', 'ordinary business quality')

    def tool_business(self):
        from tools.examples import chat_client as client
        from project_records import ProjectRecords, VALIDATORS
        body = copy.deepcopy(self.plan['requests']['tool'])
        answer = self.request('recovered-tool', body)
        calls = client.prepare_tool_calls(answer, VALIDATORS)
        self.engine.require(len(calls) == 1 and calls[0].arguments ==
            dict(path='docs/MODEL.md', start_line=1, line_count=3),
            'real documentation tool argument contract')
        result = ProjectRecords(ROOT).execute(calls[0])
        self.engine.require(result['ok'] and result['source']['sha256'] ==
            self.plan['sources']['docs/MODEL.md'], 'real record identity')
        save(self.output / 'tool-execution.json', dict(executions=1,
            call_id=calls[0].call_id, result=result))
        follow = copy.deepcopy(self.plan['requests']['follow'])
        follow['messages'] = client.append_tool_results(body['messages'], answer,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        final = self.request('recovered-tool-final', follow)
        self.engine.require(final.finish_reason == 'stop' and
            not final.message.get('tool_calls') and
            result['source']['citation'] in final.message['content'],
            'recovered real tool closure quality')

    def declarations(self):
        from tools.serve_ops import transport
        for label, path in (('capabilities', '/v1/capabilities'),
                            ('configuration', '/v1/config')):
            self.budget.reserve('controls', label)
            response = transport.get(self.policy['upstream_operations_port'],
                path, timeout=2)
            self.engine.require(response['http_status'] == 200,
                'candidate discovery unavailable')
            value = json.loads(response['body'])
            expected = self.engine.read(self.package / 'engine/declarations' /
                (label+'.json'))
            if label == 'capabilities':
                self.engine.require(value == expected,
                    'actual capabilities differ from frozen expected contract')
            else:
                # Runtime memory samples, worker state and per-generation
                # credentials are observations, never invented frozen values.
                for dotted, wanted in self.plan['configuration_checks'].items():
                    actual = value
                    for part in dotted.split('.'):
                        actual = actual[part]
                    self.engine.require(actual == wanted,
                        'actual candidate configuration differs: '+dotted)
                generated = list((self.output / 'state').rglob('model-1/client-policy.json'))
                self.engine.require(len(generated) == 1 and
                    value['client_policy']['sha256'] == self.engine.sha(generated[0]),
                    'actual scoped client policy identity')
            save(self.output / (label+'.json'), value)

    def exercise(self):
        first = self.ready(1)
        self.declarations()
        self.ordinary('initial')
        self.metrics('initial', 3)
        self.wait(lambda _: bool(self.captured(1) and
            self.captured(1)['snapshot']), seconds=10)
        owner = identity(first['model_pid'])
        self.engine.require(owner['start_ticks'] == first['model_start_ticks'],
            'owned model birth changed before injection')
        # Interrupt only main, leaving the private publisher schedulable. A
        # pause between loop iterations is retried within the same model.
        frozen = None
        for attempt in range(3):
            with PausedMain(owner):
                time.sleep(.8)
                frozen = self.captured(1)
                rows = frozen['snapshot']['rows'] if frozen and frozen['snapshot'] else []
                if not any(r['stage'] == 10 and r['work'] == 8 for r in rows):
                    continue
                save(self.output / 'owned-main-paused.json', dict(
                    owner=owner, attempt=attempt+1, captured=frozen))
                self.unavailable('stalled-admission', post=False)
                self.wait(lambda _: (self.output /
                    'progress-fault-confirmed.json').exists(), seconds=80)
                confirmed = self.engine.read(self.output / 'progress-fault-confirmed.json')
                self.engine.require(confirmed['snapshot'] is not None and
                    confirmed['snapshot']['sequence'] > frozen['snapshot']['sequence'] and
                    confirmed['snapshot']['identity']['pid'] == owner['pid'] and
                    confirmed['fault'] is not None,
                    'actual new observations must confirm the owned execution fault')
                break
        else:
            raise RuntimeError('could not pause actual native service loop')
        replacement = self.ready(2)
        self.engine.require(replacement['model_pid'] != first['model_pid'] and
            replacement['controller_pid'] == first['controller_pid'] and
            replacement['guardian_pid'] == first['guardian_pid'],
            'recovery changed incorrect process scope')
        self.tool_business()
        self.metrics('before-progress-loss', 3)
        self.wait(lambda _: bool(self.captured(2) and
            self.captured(2)['snapshot']), seconds=10)
        (self.output / 'progress-drop').touch(exist_ok=False)
        self.wait(lambda s: (self.output / 'progress-drop-applied.json').exists() and
            self.captured(2) and self.captured(2)['snapshot'] is None and
            s['business_ready'] and
            s.get('controller_health', {}).get('observation') == 'UNKNOWN', seconds=15)
        self.ordinary('observation-lost')
        self.metrics('final', 5)
        final = self.sample()
        self.engine.require(final['model_generation'] == 2 and
            final['model_pid'] == replacement['model_pid'] and
            final['controller_health']['observation'] == 'UNKNOWN' and
            final['business_ready'],
            'lost auxiliary progress restarted a healthy model')
        counts = self.engine.read(self.output / 'counts.json')['counts']
        self.engine.require(counts['starts'] == 2 and counts['chats'] == 8 and
            counts['probes'] == 1, 'unexpected startup/generation/probe count')
        save(self.output / 'business-final.json', final)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('run', 'launcher', 'controller', 'verify'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.mode == 'launcher':
        return common.launcher(plan, args.plan)
    if args.mode == 'controller':
        return controller_actor(plan, args.config)
    verify(plan)
    return Stage(plan, args.plan).run() if args.mode == 'run' else 0


if __name__ == '__main__':
    raise SystemExit(main())
