"""Bounded TLS handshake through native ingress and a real model process."""
import argparse
import json
from pathlib import Path
import ssl
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import responses_acceptance as live
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=3, chats=1,
              controls=2000, seconds=300)
SCHEMA = 'q4t-live-subprotocol-v1'


def next_event(ws, kind):
    for _ in range(30):
        value = json.loads(ws.recv())
        require(value['type'] != 'error', 'session returned an error')
        if value['type'] == kind:
            return value
    raise ValueError('session event budget exceeded')


class Stage(live.Stage):
    limits = LIMITS

    def business(self):
        import websocket
        self.snapshot('before')
        rows = []
        for path, protocol in [('/v1/live/sessions','live'),('/v1/realtime','realtime'),
                               ('/v1/realtime?intent=transcription','realtime')]:
            self.budget('websocket_sessions')
            ws = websocket.create_connection(
                f'wss://127.0.0.1:{self.plan["public_port"]}'+path,
                subprotocols=[protocol],header={'Authorization':'Bearer '+self.keys['alpha']},
                suppress_origin=True, http_no_proxy=['127.0.0.1'],
                sslopt={'context':ssl.create_default_context(cafile=self.plan['tls']['certificate'])},
                timeout=10)
            try:
                row = dict(path=path,status=ws.getstatus(),protocol=ws.getsubprotocol())
                require((row['status'],row['protocol']) == (101,protocol), 'negotiated subprotocol')
                if protocol == 'live':
                    ws.send(json.dumps(dict(type='session.start',session=dict(model='qwen3.8-flash-next'))))
                    started = next_event(ws,'session.started')
                    ws.send(json.dumps(dict(type='session.close')))
                    closed = next_event(ws,'session.closed')
                    row['session_id'] = started['session']['id']
                    require(closed['session']['id'] == row['session_id'], 'same session closed')
                rows.append(row)
            finally:
                ws.close(timeout=3)
        self.same_model()
        self.snapshot('live-drained')
        self.core('core-after')
        self.snapshot('final-drained')
        save(self.out/'handshakes.json',rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','run','finalize'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--qualification',type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence path')
    if args.action == 'prepare':
        live.prepare(out,args.qualification)
        plan = read(out/'plan.json')
        plan.update(schema=SCHEMA,limits=LIMITS,port=18711,operations_port=18712,public_port=18713)
        save(out/'plan.json',plan)
    elif args.action == 'run':
        plan = read(out/'plan.json')
        require(plan['schema']==SCHEMA, 'plan identity')
        with base.lifecycle.child_subreaper():
            Stage(plan).run()
    else:
        result, plan = read(out/'result.json'), read(out/'plan.json')
        require(result['status']=='PASS' and result['default_unchanged'] and
                result['seconds'] <= LIMITS['seconds'] and
                all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')),
                'complete bounded run')
        env = qualify.environment(read(out/'lease/exit.json'),
            [json.loads(line) for line in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
            [read(out/'cleanup.json')],expected_starts=1)
        require(all(sha(ROOT/n)==h for n,h in plan['sources'].items()),'qualified sources')
        save(out/'acceptance.json',dict(environment=env, handshakes=read(out/'handshakes.json'),
            seconds=result['seconds'], default_unchanged=True, core='42'))


if __name__ == '__main__':
    main()
