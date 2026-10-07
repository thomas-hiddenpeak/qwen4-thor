"""Fixed authored task switches and multi-turn requests for shadow observation.

Not a semantic benchmark or production sample. Owns only its launched processes.
"""
import argparse
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for key in ['binary', 'checker', 'model-dir', 'calibration', 'output']:
        ap.add_argument('--' + key, type=Path, required=True)
    ap.add_argument('--port', type=int, default=18084)
    ap.add_argument('--corpus-file', type=Path, action='append',
                    help='explicit document corpus; repeat to preserve old inputs')
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ('build', '.q4t-work')):
        ap.error('output must be under build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=False)
    paths = args.corpus_file or [ROOT / 'docs' / name for name in ['MODEL.md', 'ARCHITECTURE.md',
              'PHASES.md', 'EVALUATION.md', 'MOE_ROUTING_TRACE.md',
              'RELEASE_TEXT_V1_2026-09-28.md']]
    corpus = '\n\n'.join(p.name + '\n' + p.read_text() for p in paths)
    cases = [
        dict(name='python-retry', prompt='Design a Python retry function for a job queue. Requirements: at most four attempts, retry only TimeoutError, exponential delays starting at 0.2 seconds, preserve the final exception, inject the sleep function for tests. Explain the state transitions and show code.'),
        dict(name='ledger', prompt='A warehouse starts with 137 boxes. Monday ships 29 and receives 46; Tuesday ships 38 and receives 17; Wednesday scraps 9 and ships 24. Each remaining box contains 12 parts. Show a daily ledger, final box count and final number of parts. Explain how you would detect double counting.'),
        dict(name='python-followup', parent=0, prompt='Now change the function to accept a deadline. Explain which time source to use, and how to prevent sleeping past the deadline without masking the last failure.'),
        dict(name='chinese-editorial', prompt='请写一封社区图书馆给读者的通知：周三至周五更换书架，一层关闭，二层与还书箱照常开放；预约书可延期三天领取，儿童活动移至周六上午。语气平实，先说明最重要的信息，再列出读者需要采取的行动。不要添加没有提供的联系方式或日期。'),
        dict(name='long-project-review', prompt='阅读以下项目材料，列出五项已经实现的能力和五项尚未证明的结论。区分实现事实、历史证据和后续计划。\n\n' + corpus),
        dict(name='systems-incident', prompt='An append-only event service acknowledges writes after fsync. Following a restart, customers see duplicate notifications but no missing event IDs. Propose an investigation plan that distinguishes replay, producer retries, consumer checkpoint failures and duplicate delivery. State which evidence would falsify each hypothesis.'),
        dict(name='project-followup', parent=4, prompt='从上面的材料中，只挑出涉及资源生命周期的三个风险，分别说明需要观察的状态和最小验证方法。不要把未来设计当成已实现功能。'),
        dict(name='ledger-followup', parent=1, prompt='An audit finds that Tuesday receipts were actually 19 boxes, and Wednesday scrap was already included in Wednesday shipments. Correct the ledger and identify precisely which operations changed.')]
    save(out / 'workload.json', dict(cases=cases, max_tokens=128,
         source_files={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
         scope='authored deterministic task order; two short and one long multi-turn pair; no semantic score'))
    trace, observer_out = out / 'trace', out / 'observer'
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    command = [str(args.binary.resolve()), 'serve', '--model-dir', str(args.model_dir.resolve()),
               '--port', str(args.port), '--max-seq', '1', '--max-prefill', '8192',
               '--max-len', '208896', '--no-mtp', '--moe-trace-dir', str(trace),
               '--moe-trace-workload', str(out / 'workload.json'), '--moe-trace-max-mib', '1024']
    observer_command = [sys.executable, '-B', str(ROOT / 'tools/trace/shadow.py'),
        '--directory', str(trace), '--binary', str(args.binary.resolve()),
        '--checker', str(args.checker.resolve()), '--calibration', str(args.calibration.resolve()),
        '--output', str(observer_out)]
    save(out / 'command.json', dict(server=command, observer=observer_command))
    responses, conversations, failure = [], [], None
    with (out / 'server.log').open('w') as log, (out / 'observer.log').open('w') as shadow_log:
        observer = subprocess.Popen(observer_command, stdout=shadow_log, stderr=subprocess.STDOUT, env=env)
        server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        try:
            until = time.monotonic() + 180
            while 'serving on port' not in (out / 'server.log').read_text():
                if server.poll() is not None or time.monotonic() > until:
                    raise RuntimeError('service startup failed')
                time.sleep(1)
            for index, case in enumerate(cases, 1):
                messages = list(conversations[case['parent']]) if 'parent' in case else []
                messages.append(dict(role='user', content=case['prompt']))
                body = dict(model='qwen3.8-flash-next', messages=messages,
                            enable_thinking=False, temperature=0, max_tokens=128,
                            stream=True, stream_options=dict(include_usage=True))
                save(out / f'request-{index}.json', body)
                conn = HTTPConnection('127.0.0.1', args.port, timeout=600)
                try:
                    conn.request('POST', '/v1/chat/completions', json.dumps(body), {'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    data = response.read().decode()
                    (out / f'response-{index}.sse').write_text(data)
                    if response.status != 200:
                        raise RuntimeError('HTTP status ' + str(response.status))
                finally:
                    conn.close()
                lines = [line[6:] for line in data.splitlines() if line.startswith('data: ')]
                assert lines[-1] == '[DONE]'
                events = [json.loads(line) for line in lines[:-1]]
                assert not any('error' in e for e in events)
                choices = [c for e in events for c in e.get('choices', [])]
                finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
                assert len(finish) == 1 and finish[0] in ('stop', 'length')
                usages = [e['usage'] for e in events if e.get('usage')]
                assert len(usages) == 1 and 0 < usages[0]['completion_tokens'] <= 128
                text = ''.join(c.get('delta', {}).get('content', '') for c in choices)
                conversations.append(messages + [dict(role='assistant', content=text)])
                responses.append(dict(name=case['name'], http_id=events[0]['id'], usage=usages[0],
                                      finish=finish[0], text=text))
                save(out / 'responses.json', responses)
                print(index, case['name'], usages[0], flush=True)
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
                failure = failure or 'server shutdown timeout'
            try:
                observer.wait(timeout=180)
            except subprocess.TimeoutExpired:
                observer.terminate()
                observer.wait(timeout=10)
                failure = failure or 'observer shutdown timeout'
            save(out / 'exit.json', dict(failure=failure, server=server.returncode,
                                        observer=observer.returncode, requests=len(responses)))
    if failure or server.returncode or observer.returncode or len(responses) != 8:
        raise RuntimeError('study incomplete')
    return 0


if __name__ == '__main__':
    sys.exit(main())
