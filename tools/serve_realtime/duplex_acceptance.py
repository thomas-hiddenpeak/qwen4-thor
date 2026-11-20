"""One-lease native duplex WSS/ASR/TTS/tools/VAD business, no default changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime import duplex_checks as checks
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_realtime.vad_acceptance import VAD, WEIGHT, DIGEST
from tools.serve_service.package import require, save, sha

WORK = ROOT / '.q4t-work/serve-realtime-duplex-20261120'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=2,
              responses=9, audio_inputs=7, chats=2, controls=2000, seconds=1200)
CONTRACT = dict(native_duplex=True, mixed_item=True, inline_input=True,
    private_transcript=True, speech_roundtrip=True, tool_return=True,
    vad_auto=True, vad_interrupt=True, zero_sample_idle=True,
    same_model=True, automatic_replay=False, default_unchanged=True,
    guardian_recovery=False, master_complete=False)


def check_plan(plan):
    require(plan['schema'] == 'q4t-realtime-duplex-business-v1' and
            plan['limits'] == LIMITS and plan['contract'] == CONTRACT,
            'frozen duplex scope drift')
    require(plan['record']['path'] == str(ROOT / 'docs/STATUS.md') and
            re.fullmatch(r'rev[0-9]+', plan['record']['version']),
            'real current project record')


def accounting(metrics, client, observed):
    key = lambda name: name + '{client="' + client + '"}'
    settled = metrics[key('q4t_client_rate_settled_tokens_total')]
    visible = observed['visible_tokens']
    require(metrics[key('q4t_client_rate_admitted_total')] == observed['admitted']
            and metrics[key('q4t_client_rate_reserved_tokens')] == 0,
            'actual input/response RPM or reservation leak')
    # The private ASR does not expose a public usage event. Record its actual
    # counter residual; do not invent a token oracle from the public response.
    require(settled > visible if observed['private_asr'] else settled == visible,
            'actual token settlement differs from visible/private ownership')
    return dict(actual_settled_tokens=settled, visible_tokens=visible,
                private_asr_counter_residual=settled-visible,
                private_asr_exact_token_oracle=False if observed['private_asr'] else None,
                admitted=observed['admitted'])


def capture_actor(command, arguments, output, errors, env, timeout):
    # Keep partial evidence even when the parent is interrupted before the
    # actor can serialize its final receipt. Do not buffer stdout in Python.
    with Path(output).open('xb') as stdout, Path(errors).open('xb') as stderr:
        return subprocess.run(command, input=json.dumps(arguments).encode(),
                              stdout=stdout, stderr=stderr, env=env,
                              timeout=timeout)


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed driver/source before freezing')
    build = WORK / 'mixed-entry-runtime-01'
    built = json.loads((build / 'result.json').read_text())
    require(built['status'] == 'BUILD_PASS' and built['warnings'] == 0,
            'zero warning native build required')
    for name, digest in json.loads((build / 'plan.json').read_text())['sources'].items():
        require(sha(ROOT/name) == digest, 'built native source drift')
    binary = args.binary.resolve(strict=True)
    require(sha(binary) == built['binary_sha256'], 'native build identity')
    cpu = WORK / 'duplex-driver-cpu-02'
    require(json.loads((cpu/'result.json').read_text())['status'] == 'PASS',
            'verifier negative cases not qualified')
    for name,digest in json.loads((cpu/'plan.json').read_text())['sources'].items():
        require(sha(ROOT/name) == digest, 'qualified driver source drift')
    previous = base.WORK / 'transcription-business-04/run'
    require(json.loads((previous/'result.json').read_text())['status'] == 'PASS',
            'reused original audio not qualified')
    plan = json.loads((previous/'plan.json').read_text())
    out = args.output.resolve()
    require(out.is_relative_to(WORK), 'private stage directory')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = json.loads((previous/'client-policy.json').read_text())
    policy['gateway_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client.update(requests_per_minute=30, tokens_per_minute=60000,
                      max_output_tokens=3457)
    save(out/'client-policy.json', policy); (out/'client-policy.json').chmod(0o600)
    tls = certificate(out/'credentials')
    tts = json.loads((ROOT/'build/service/engine/manifest.json').read_text())['settings']['native_tts']
    sdk = base.WORK / 'profile-sdk-01'
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/serve_realtime', 'tools/serve_grammar_reap',
        'tools/serve_recovery', 'tools/serve_service', 'tools/evalscope',
        'tools/native_asr', 'tools/native_tts', 'tools/release'], cwd=ROOT, text=True).splitlines()
    record = ROOT/'docs/STATUS.md'
    version = re.search(r'默认\s*rev([0-9]+)', record.read_text())
    require(version is not None, 'actual release version')
    protected = [ROOT/n for n in ('build/q4t', 'build/q4t.release.json',
        'build/service/manifest.json', 'build/service.release.json',
        'build/service-native-state/ledger.json')]
    plan.update(schema='q4t-realtime-duplex-business-v1', limits=LIMITS,
        contract=CONTRACT, output=str(out), port=args.port, operations_port=args.port+1,
        public_port=args.port+2, binary=str(binary), binary_sha256=sha(binary),
        native_tts=tts, tts_assets={str(p):sha(p) for p in sorted(Path(tts['directory']).rglob('*')) if p.is_file()},
        vad_directory=str(VAD), vad_assets={WEIGHT:DIGEST},
        sdk_directory=str(sdk), sdk_files={str(p):sha(p) for p in sorted((sdk/'node_modules').rglob('*')) if p.is_file()},
        sources={n:sha(ROOT/n) for n in sources}, protected={str(p):sha(p) for p in protected},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        client_policy_sha256=sha(out/'client-policy.json'), tls=tls,
        tls_files={tls[n]:sha(tls[n]) for n in ('certificate','key')},
        reused_core_before={}, qualified_files={str(p):sha(p) for directory in
            (build,cpu) for p in directory.iterdir() if p.is_file() and p.suffix in
            ('.json','.log','.txt','.stdout','.stderr')},
        record=dict(path=str(record), sha256=sha(record), version='rev'+version[1]),
        scope=__doc__)
    check_plan(plan)
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=LIMITS)))


class Stage(base.Stage):
    limits = LIMITS

    def __init__(self, plan):
        check_plan(plan)
        super().__init__(plan)
        for name,digest in {**plan['qualified_files'], **plan['tts_assets']}.items():
            require(sha(name) == digest, 'qualification/TTS drift')
        require(sha(Path(plan['vad_directory'])/WEIGHT) == DIGEST and
                sha(plan['record']['path']) == plan['record']['sha256'],
                'VAD or real record drift')

    def extra_model_args(self):
        tts = self.plan['native_tts']
        result = ['--tts-model-dir',tts['directory'], '--vad-model-dir',self.plan['vad_directory']]
        for key,value in tts['limits'].items():
            result += ['--tts-'+key.replace('_','-'), str(value)]
        return result

    def duplex(self, mode, client):
        self.budget('websocket_sessions')
        self.budget('responses', 6 if mode == 'manual' else 3)
        self.budget('audio_inputs', 5 if mode == 'manual' else 2)
        require(shutil.disk_usage(self.out).free >= 128 << 20, 'disk headroom')
        record = self.plan['record']; clip = self.plan['clips']['zh']
        args = dict(mode=mode, base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys[client], ca_file=self.plan['tls']['certificate'],
            clip=clip['path'], clip_sha256=clip['sha256'], expected=self.plan['expected']['zh'],
            record_file=record['path'], record_sha256=record['sha256'], expected_version=record['version'],
            journal_file=str(self.out/(mode+'.events.jsonl')))
        env = {k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = self.plan['sdk_directory']
        output = self.out/(mode+'.sdk.json')
        run = capture_actor(['node',str(ROOT/'tools/serve_realtime/duplex_sdk.mjs')],
            args, output, self.out/(mode+'.stderr'), env, 430)
        require(run.returncode == 0 and output.stat().st_size <= 16 << 20,
                'actual duplex SDK failed: '+mode)
        receipt = json.loads(output.read_text())
        journal = Path(args['journal_file'])
        require(journal.stat().st_size <= 32 << 20, 'event journal bound')
        checks.journal_matches([json.loads(line) for line in journal.read_text().splitlines()], receipt)
        observed = checks.observe(receipt, args['expected'], record)
        self.same_model()
        settlement = accounting(self.snapshot(mode+'-drained'), client, observed)
        save(self.out/(mode+'-receipt.json'), dict(observed=observed, settlement=settlement))
        return observed['quality']

    def business(self):
        self.snapshot('before'); self.core('core-before')
        quality = self.duplex('manual','alpha')
        save(self.out/'quality.json', quality)
        quality += self.duplex('vad','beta')
        save(self.out/'quality.json', quality)
        self.core('core-after'); self.snapshot('final-drained')
        require(all(row['status'] == 'PASS' for row in quality),
                'original model quality failure retained separately from protocol')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--binary',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True)
    prep.add_argument('--port',type=int,default=18508)
    run = sub.add_parser('run');run.add_argument('--plan',type=Path,required=True)
    args = parser.parse_args()
    if args.action == 'prepare': prepare(args)
    else:
        with base.lifecycle.child_subreaper(): Stage(json.loads(args.plan.read_text())).run()
