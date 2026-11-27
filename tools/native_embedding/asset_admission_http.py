"""Run one bound primary plan with the cold embedding and Live VAD readers."""
import argparse
import contextlib
import http.client
import json
from pathlib import Path
import ssl
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.evalscope import gpu_exclusive
from tools.native_embedding import business_acceptance as embeddings
from tools.serve_live import profile_acceptance as profiles
from tools.serve_recovery import bundle, journal
from tools.serve_service import package
from tools.serve_service.package import read, require, save, sha


def children(pid):
    return [child for child, row in gpu_exclusive.processes().items()
            if row.get('ppid') == pid]


def held_weight(pid, identity):
    try:
        for fd in Path(f'/proc/{pid}/fd').iterdir():
            try:
                stat = fd.stat()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if (stat.st_dev, stat.st_ino) == identity:
                return True
    except (FileNotFoundError, ProcessLookupError):
        pass
    return False


def wait_reader(model, weight):
    info = weight.stat()
    identity = (info.st_dev, info.st_ino)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        require(model.poll() is None, 'core stopped during embedding weight read')
        for pid in children(model.pid):
            process = Path(f'/proc/{pid}')
            try:
                args = (process/'cmdline').read_bytes().split(b'\0')
                if args[1:2] == [b'--model-asset-worker'] and held_weight(pid, identity):
                    require((process/'exe').samefile(f'/proc/{model.pid}/exe'),
                            'reader executable differs from core')
                    return pid, identity
            except (FileNotFoundError, ProcessLookupError):
                continue
        time.sleep(.01)
    raise TimeoutError('native embedding weight reader was not observed')


def receive(conn, expected):
    response = conn.getresponse()
    payload = response.read(65537)
    require(len(payload) <= 65536, 'oversized response')
    value = json.loads(payload)
    require(response.status == expected,
            f'HTTP {response.status}: {value.get("error", {}).get("code")}')
    return value


class Stage(profiles.Stage):
    def reader_health(self):
        status, raw = self.http('/v1/config', timeout=3)
        require(status == 200, 'could not observe native reader health')
        config = json.loads(raw)
        return {name: config[f'native_{name}_health'] for name in ('embedding', 'vad')}

    def observe_contention(self, pid, start_ticks, identity):
        # The POST can be in flight during WebRTC negotiation before VAD starts.
        # Check the original embedding reader again after sampling the server.
        for _ in range(60):
            health = self.reader_health()
            require(held_weight(pid, identity) and journal.process_start(pid) == start_ticks,
                    'embedding weight reader finished before VAD loading was observed')
            if health['vad']['state'] == 'loading':
                require(health['embedding']['state'] == 'loading'
                        and health['vad']['load_attempts'] == 1,
                        'native backend state differs during the overlapping read')
                return health
            require(health['vad']['state'] == 'configured',
                    f'unexpected VAD state before overlapping load: {health["vad"]}')
            time.sleep(.025)
        raise TimeoutError('VAD did not start while the embedding reader held its weight')

    def business(self):
        plan = self.plan
        cmd = plan['command']
        weights = Path(cmd[cmd.index('--embedding-model-dir') + 1])/'model.safetensors'
        tls = ssl.create_default_context(cafile=plan['tls']['certificate'])
        rtc = profiles.media.rtc
        rtc.Gst.init(None)
        with contextlib.ExitStack() as stack:
            peer, _, _, channel, events, offer, _ = stack.enter_context(rtc.peer_offer('OPUS'))
            def connection():
                conn = http.client.HTTPSConnection('127.0.0.1', plan['public_port'],
                                                   context=tls, timeout=45)
                stack.callback(conn.close)
                conn.connect()
                return conn
            embed, live = connection(), connection()
            def post(conn, path, body, client):
                conn.request('POST', path, json.dumps(body),
                    {'Authorization': 'Bearer '+self.keys[client], 'Content-Type': 'application/json'})
            session = dict(model='qwen3.8-flash-next', client=dict(data_channel=dict(
                allowed_client_events=['session.close'])))
            initial = self.reader_health()
            require(all(h['state'] == 'configured' and h['load_attempts'] == 0
                        for h in initial.values()), 'both asset readers must start cold')
            post(embed, '/v1/embeddings', dict(model=embeddings.MODEL,
                 input='project reference', dimensions=32, encoding_format='float'), 'alpha')
            reader, identity = wait_reader(self.model, weights)
            reader_start = journal.process_start(reader)
            start = time.monotonic_ns()
            post(live, '/v1/live/sessions', dict(transport=dict(type='webrtc', sdp=offer),
                 session=session), 'beta')
            sent = time.monotonic_ns()
            require(held_weight(reader, identity), 'embedding weight read ended before concurrent POST')
            require(journal.process_start(reader) == reader_start,
                    'embedding worker identity changed during the concurrent POST')
            loading = self.observe_contention(reader, reader_start, identity)
            reply = receive(live, 201)
            ident = reply['session']['id']
            rtc.accept_answer(peer, reply['transport']['sdp'])
            def observed(kind):
                return any(ev.get('type') == kind and ev.get('session', {}).get('id') == ident
                           for ev in map(json.loads,list(events)))
            rtc.wait_for(lambda: observed('session.started'), 'session start', 15)
            channel.send_string(json.dumps(dict(type='session.close', event_id='done')))
            rtc.wait_for(lambda: observed('session.closed'), 'session close', 15)
            vector = embeddings.vectors(receive(embed, 200), 1, 32)
        ready = self.reader_health()
        require(all(h['state'] == 'ready' and h['load_failures'] == 0
                    for h in ready.values()) and ready['vad']['load_attempts'] == 1
                and ready['vad']['host_weight_bytes'] > 0 and ready['embedding']['device_bytes'] > 0,
                'native readers did not both finish their first cold load')
        self.budget('chats')
        code, chat = self.api('POST', '/v1/chat/completions', body=dict(model=session['model'],
            temperature=0, enable_thinking=False, max_completion_tokens=16,
            messages=[dict(role='user',content='Compute 17 plus 25. Reply only with the number.')]))
        require(code == 200 and chat['choices'][0]['message']['content'].strip() == '42',
                'independent Chat failed after overlapping loads')
        save(self.out/'asset-admission.json', dict(model_pid=self.model.pid,
             embedding_worker_pid=reader, reader_start_ticks=reader_start,
             reader_held_weight_after_post=True,
             backend_health=dict(before=initial, overlapping=loading, after=ready),
             send_monotonic_ns=[start,sent], embedding_dimensions=len(vector[0]),
             live_status=201, live_session=ident, closed=True, independent_chat='42',
             business_https_posts=dict(embeddings=1, live_sessions=1, chat=1)))


def prepare(output, build, deployment, expected_manifest, fixture):
    require(output.is_relative_to(ROOT/'.q4t-work'), 'private stage output required')
    selected = deployment/'build/service'
    manifest = bundle.verify(selected, expected_manifest)
    engine = package.verify(selected/'engine', manifest['engine_manifest_sha256'])
    sources = read(build/'generated/q4t/server/chat_protocol_sources.json')
    require('src/server/model_assets.cpp' in sources and
            all(sha(ROOT/k) == digest for k, digest in sources.items()),
            'candidate protocol sources differ from the checkout')
    ingress = selected/'bin/q4t-ingress'
    require(sha(ingress) == manifest['files']['bin/q4t-ingress'], 'ingress digest differs')
    edge = build/'q4t-ingress'
    require(sha(edge) == sha(ingress), 'this qualification uses the issued ingress')
    fixed = ('q4t', 'q4t.release.json', 'service/manifest.json',
             'service.release.json', 'service-native-state/ledger.json')
    protected = {str(deployment/'build'/n): sha(deployment/'build'/n) for n in fixed}
    profile = dict(engine['live_workers'], engine_sha256=sha(build/'q4t'))
    profiles.prepare_profiles(output, manifest, engine, profile, sha(edge),
        build=build, protected=protected, installed_workers=selected/'engine',
        selected_manifest=selected/'manifest.json', previous_fixture=fixture)
    path = output/'primary/plan.json'
    plan = read(path)
    driver = Path(__file__).resolve().relative_to(ROOT)
    plan['sources'][str(driver)] = sha(ROOT/driver)
    plan['sources']['src/server/model_assets.cpp'] = sources['src/server/model_assets.cpp']
    plan['asset_admission_package'] = expected_manifest
    save(path, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    prep = actions.add_parser('prepare')
    for name in ('output', 'build', 'deployment', 'fixture'):
        prep.add_argument('--'+name, type=Path, required=True)
    prep.add_argument('--manifest-sha256', required=True)
    run = actions.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.output.resolve(), args.build.resolve(), args.deployment.resolve(),
                args.manifest_sha256, args.fixture.resolve())
        return
    plan = read(args.plan)
    require(args.plan.resolve().is_relative_to(ROOT/'.q4t-work') and
            plan['schema'] == profiles.SCHEMA and plan['name'] == 'primary' and
            [value for path, value in plan['protected'].items()
             if path.endswith('/build/service/manifest.json')] == [plan['asset_admission_package']],
            'expected a protected primary asset-admission plan')
    require(plan['sources']['src/server/model_assets.cpp'] == sha(ROOT/'src/server/model_assets.cpp')
            and plan['binary_sha256'] == sha(Path(plan['binary'])), 'native candidate changed')
    with profiles.base.lifecycle.child_subreaper():
        Stage(plan).run()


if __name__ == '__main__':
    main()
