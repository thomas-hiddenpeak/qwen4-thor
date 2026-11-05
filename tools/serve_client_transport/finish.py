"""Resume only missing client coverage after the request-drain fixture repair."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_client_transport import stage as s
from tools.serve_service import package as pkg

PLAN = s.BASE/'finish-plan-03.json'
OUT = s.BASE/'execution-03'
OLD = s.BASE/'execution'
OLD_PLAN = s.BASE/'plan.json'
ALLOWED = {'tools/serve_client_transport/probes.py',
           'tools/serve_client_transport/finish.py',
           'tools/serve_client_transport/report.py'}


def fixture_check():
    class Fake:
        def __init__(self):
            self.parts = [b'POST /probe HTTP/1.1\r\nContent-Length: 2\r\n\r\n', b'{}']
        def recv(self, _):
            return self.parts.pop(0) if self.parts else b''
        def sendall(self, _):
            pass
    peer = object.__new__(s.probes.Peer)
    peer.case = 'length-ok'
    peer.record = dict(requests=0, sent_bytes=0)
    peer.stop = threading.Event()
    fake = Fake()
    peer.respond(fake)
    pkg.require(not fake.parts and peer.record['request_body_bytes']==2,
                'fixture must drain a separately delivered POST body')
    return dict(passed=True, models=0, http=0, request_body_drained=True)


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve follow-up attempt')
    pkg.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
                'commit reviewed follow-up first')
    old = pkg.read(OLD_PLAN)
    previous = s.BASE/'execution-02'
    failed = pkg.read(previous/'result.json')
    pkg.require(failed['counts']==dict(starts=0,generation_attempts=0,controls=0,network=10)
        and failed['status']=='FAILED' and pkg.read(previous/'lease/exit.json')['status']=='COMMAND_FAILED'
        and failed['error']=="OSError(98, 'Address already in use')",
        'unexpected follow-up failure identity')
    for name,digest in old['sources'].items():
        pkg.require(pkg.sha(s.BASE/'initial-source'/name)==digest,'original source lost')
        if name not in ALLOWED:
            pkg.require(pkg.sha(ROOT/name)==digest,'unreviewed source delta: '+name)
    plan = copy.deepcopy(old)
    plan.update(sources=s.source_files(), source_commit=subprocess.check_output(
        ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        prior=dict(plan_sha256=pkg.sha(s.BASE/'finish-plan.json'), result_sha256=pkg.sha(previous/'result.json'),
                   seconds=failed['seconds'], counts=failed['counts']),
        reuse={f'python-{case}':str(OLD/'network'/f'python-{case}.json') for case in s.probes.SLOW},
        reuse_reason='Eight slow-phase deadlines completed before response close; client sources unchanged. '
          'The fixture now drains POST before closing, preventing an unread-body TLS reset.',
        additional_network_probes=26, cumulative_network_attempts=36)
    plan['preserved'].update({str(OLD_PLAN):pkg.sha(OLD_PLAN),str(OLD/'result.json'):pkg.sha(OLD/'result.json')})
    plan['preserved'].update({name:pkg.sha(name) for name in plan['reuse'].values()})
    for path in (s.BASE/'finish-plan.json',previous/'result.json'):
        plan['preserved'][str(path)]=pkg.sha(path)
    pkg.save(s.BASE/'fixture-fixed.json',fixture_check())
    pkg.save(PLAN,plan)
    configure()
    s.check()
    return dict(prepared=True,reused_network=8,new_network=26,total_network_attempts=36,
                models_remaining=1,generations_remaining=6)


def configure():
    s.PLAN, s.OUT = PLAN, OUT
    if getattr(s, '_finish_configured', False):
        return
    s._finish_configured = True
    original_execute = s.probes.execute
    def execute(*args):
        return original_execute(*args,reused=pkg.read(PLAN)['reuse'])
    s.probes.execute = execute
    original_counts = s.verify_counts
    def counts(value, successes, real_tools):
        pkg.require(value['network']==36,'cumulative network attempt count')
        normalized = dict(value,network=34)
        original_counts(normalized,successes,real_tools)
    s.verify_counts = counts


def run():
    configure()
    plan = s.check()
    pkg.require(not (OUT/'result.json').exists(),'preserve follow-up execution')
    with s.shared.sup.processes.child_subreaper():
        driver = s.Run(plan)
        driver.started -= plan['prior']['seconds']
        driver.counts.update(plan['prior']['counts'])
        return driver.execute()


if __name__=='__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'))
    args = parser.parse_args()
    print(json.dumps(globals()[args.action]()))
