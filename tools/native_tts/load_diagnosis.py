"""One unmodified candidate load/request with read-only owned kernel stacks."""
import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_tts import http_acceptance as http
from tools.native_tts.acceptance import defaults
from tools.serve_service.package import read, require, save, sha
from tools.verify.inference_metadata.diagnose_warm_start import read_text

LIMITS = dict(starts=1, chats=1, controls=2000, seconds=180,
              observer_seconds=180, observer_interval=.2, artifact_mib=32)


def observe(folder):
    """Root reads only this owned model and the parent guard's query worker."""
    plan = read(folder/'plan.json')
    require(plan['limits'] == LIMITS, 'observer plan')
    guard_pid = read(folder/'runtime.json')['guard_pid']
    end = time.monotonic()+LIMITS['observer_seconds']
    with (folder/'stacks.jsonl').open('x') as output:
        while time.monotonic() < end and not (folder/'stop-observer').exists():
            targets = []
            model_record = read(folder/'model-identity.json') if (folder/'model-identity.json').exists() else None
            for proc in Path('/proc').iterdir():
                if not proc.name.isdecimal(): continue
                stat = read_text(proc/'stat')['value']
                cmd = read_text(proc/'cmdline')['value']
                if not stat or not cmd: continue
                fields = stat.rsplit(')',1)[-1].split()
                argv = cmd.split('\0')
                model = (model_record and int(proc.name) == model_record['pid']
                    and int(fields[19]) == model_record['start_ticks'])
                query = (int(fields[1]) == guard_pid and '--worker' in argv
                    and str(ROOT/'tools/evalscope/nvml_process_query.py') in argv)
                if not model and not query: continue
                tasks = []
                try: entries = list((proc/'task').iterdir())
                except OSError: entries = []
                for task in entries:
                    current = read_text(task/'stat')
                    state = current['value'].rsplit(')',1)[-1].split()[0] if current['value'] else None
                    if task.name != proc.name and state != 'D': continue
                    tasks.append(dict(tid=int(task.name), stat=current,
                        wchan=read_text(task/'wchan'), stack=read_text(task/'stack')))
                    if len(tasks) >= 16: break
                targets.append(dict(pid=int(proc.name), kind='model' if model else 'nvml', tasks=tasks))
            output.write(json.dumps(dict(monotonic=time.monotonic(), targets=targets,
                meminfo=read_text(Path('/proc/meminfo')), vmstat=read_text(Path('/proc/vmstat'))))+'\n')
            output.flush()
            require(output.tell() <= LIMITS['artifact_mib'] << 20, 'observer bytes')
            time.sleep(LIMITS['observer_interval'])


class Stage(http.Stage):
    def __init__(self, plan):
        require(not os.environ.get('PYTHONOPTIMIZE') and not os.environ.get('LD_PRELOAD')
            and not any(k.startswith('Q4T_ABLATE_') for k in os.environ), 'diagnostic environment')
        self.plan, self.out, self.started = plan, Path(plan['output']), time.monotonic()
        self.model = self.model_identity = self.fault_thread = None
        self.history = {}; self.counts = dict(starts=0, chats=0, controls=0)
        self.lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        http.lifecycle.verify_lease(self.lease, http.lifecycle.guard.common_lock())
        require(plan['limits'] == LIMITS, 'diagnostic bounds')
        require(defaults() == plan['defaults'], 'default drift')
        for p,h in plan['sources'].items(): require(sha(ROOT/p) == h, 'source drift: '+p)
        for row in plan['binaries'].values(): require(sha(row['path']) == row['sha256'], 'binary drift')
        http.check_assets(plan['model'], plan['model_assets'])

    def budget(self, kind):
        require(time.monotonic()-self.started < LIMITS['seconds']-40, 'diagnostic deadline')
        require(self.counts[kind] < LIMITS[kind], 'diagnostic attempts')
        self.counts[kind] += 1; save(self.out/'counts.json', self.counts)

    def run(self):
        result = dict(status='FAILED', actual_business_acceptance=False)
        observer = None
        previous = signal.getsignal(signal.SIGALRM)
        def expired(_signum,_frame): raise TimeoutError('bounded load diagnosis')
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
        try:
            observer = subprocess.Popen(['sudo','-n',sys.executable,'-B',__file__,
                'observe','--output',str(self.out)], stdout=(self.out/'observer.log').open('xb'),
                stderr=subprocess.STDOUT)
            self.start()
            hidden = self.out/'tts-assets/speech_tokenizer/config.json.withheld'
            hidden.rename(hidden.with_suffix(''))
            status,raw = self.post('speech', http.speech(0))
            require(status == 200 and len(raw) > 1024, 'real speech did not complete')
            self.drained('speech-drain')
            result.update(status='COMPLETED_LOAD_DIAGNOSIS', speech_bytes=len(raw))
        except BaseException:
            result['error'] = traceback.format_exc()
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            try:
                (self.out/'stop-observer').touch()
                if observer is not None:
                    result['observer_exit'] = observer.wait(5)
            finally:
                self.cleanup()
                result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                    defaults_unchanged=defaults() == self.plan['defaults'])
                save(self.out/'result.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare','run','observe'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-plan', type=Path)
    args = parser.parse_args()
    if args.mode == 'prepare':
        require(not subprocess.check_output(['git','status','--porcelain']), 'commit sources')
        plan = read(args.source_plan)
        plan.pop('continuation', None)
        plan.update(schema='q4t-speech-load-diagnosis-v1', limits=LIMITS,
            output=str(args.output.absolute()), requests=[http.speech(0)],
            scope='one model/one Speech, no ASR/performance acceptance, unchanged strict NVML; no cache/global/device mutation')
        for p in (Path(__file__), ROOT/'tools/verify/inference_metadata/diagnose_warm_start.py'):
            plan['sources'][str(p.resolve().relative_to(ROOT))] = sha(p)
        args.output.mkdir(parents=True, mode=0o700)
        save(args.output/'plan.json', plan)
    elif args.mode == 'observe': observe(args.output)
    else:
        plan = read(args.output/'plan.json')
        save(args.output/'runtime.json', dict(guard_pid=os.getppid(), driver_pid=os.getpid()))
        with http.lifecycle.child_subreaper(): Stage(plan).run()


if __name__ == '__main__': main()
