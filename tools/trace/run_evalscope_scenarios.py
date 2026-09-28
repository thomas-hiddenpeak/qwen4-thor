"""Bounded authored scenario sampling through local EvalScope (not acceptance)."""
import argparse
import base64
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import subprocess
import sys
import time

from analyze import sha
from run_shadow_study import save

ROOT = Path(__file__).resolve().parents[2]


def cases():
    result = []
    for variant, count in enumerate([4, 32, 128, 512], 1):
        records = '\n'.join(
            f'Order O{i:04d}: region={i % 5}, units={i % 17 + 1}, '
            f'price={i % 23 + 10}, status={"delayed" if i % 7 == 0 else "shipped"}.'
            for i in range(count))
        logs = '\n'.join(
            f't={i} service=worker-{i % 4} queue={i % 31} '
            f'event={"timeout" if i % 13 == 0 else "ack"} attempt={i % 3 + 1}'
            for i in range(count))
        prompts = {
            'support': f'你是电商客服。客户订单 O{count-1:04d} 尚未收到。根据以下记录写回复，说明可确认的信息和需要进一步查询的信息，不编造到货时间。再列出客服处理步骤。\n{records}',
            'orders': f'Analyze these order records. Explain how to compute revenue by region excluding delayed orders, provide a SQL query and identify data-quality checks. Distinguish order count from units.\n{records}',
            'debug': f'以下 Python 消费逻辑会重复处理订单：\nfor item in queue:\n    send_notification(item)\n    save_checkpoint(item.id)\n进程可能在任意语句后崩溃。解释以下日志能够和不能够证明什么，并设计幂等修复及故障测试。\n{logs}',
            'writing': f'请将以下运营记录改写为面向内部团队的中文交接说明。先概述情况，再说明异常和下一步。只引用记录支持的事实，不臆测原因。\n{records}',
            'incident': f'Review these worker logs. Separate observed facts from hypotheses, explain retry and checkpoint risks, and propose three discriminating diagnostics without claiming causality from repetition.\n{logs}',
            'retrieval': f'阅读订单记录。分别摘录 O0000 和 O{count-1:04d} 的全部字段，解释 status 字段能否证明已经签收，然后给出核验建议。\n{records}',
        }
        for scenario, prompt in prompts.items():
            result.append(dict(scenario=scenario, variant=variant,
                               records=count, request=dict(
                messages=[dict(role='user', content=prompt)],
                chat_template_kwargs=dict(enable_thinking=False),
                temperature=0, max_tokens=128, stream=True,
                stream_options=dict(include_usage=True))))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ['output', 'model-dir', 'checker', 'calibration']:
        ap.add_argument('--' + name, type=Path, required=True)
    ap.add_argument('--binary', type=Path, default=ROOT / 'build/q4t')
    ap.add_argument('--port', type=int, default=18085)
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        if exe.name.startswith('q4t') and b'serve' in argv:
            raise RuntimeError('another q4t service is running')
    out.mkdir(parents=True, exist_ok=False)
    manifest = cases()
    save(out / 'workload.json', manifest)
    (out / 'requests.jsonl').write_text(''.join(
        json.dumps(c['request'], ensure_ascii=False) + '\n' for c in manifest))
    shutil.copy2(__file__, out / Path(__file__).name)
    shutil.copy2(args.calibration, out / 'frozen-calibration.json')
    save(out / 'identity.json', dict(binary_sha256=sha(args.binary),
         calibration_sha256=sha(args.calibration), checker_sha256=sha(args.checker),
         commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
         tools={p.name: sha(p) for p in (ROOT / 'tools/trace').glob('*.py')}))
    (out / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    server_cmd = [str(args.binary.resolve()), 'serve', '--model-dir', str(args.model_dir.resolve()),
        '--port', str(args.port), '--max-seq', '1', '--max-prefill', '8192',
        '--max-len', '208896', '--no-mtp', '--moe-trace-dir', str(out / 'trace'),
        '--moe-trace-workload', str(out / 'workload.json'), '--moe-trace-max-mib', '1024']
    observer_cmd = [sys.executable, '-B', str(ROOT / 'tools/trace/shadow.py'),
        '--directory', str(out / 'trace'), '--binary', str(args.binary.resolve()),
        '--checker', str(args.checker.resolve()), '--calibration', str(out / 'frozen-calibration.json'),
        '--output', str(out / 'observer')]
    client_cmd = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
        '--model', 'qwen3.8-flash-next', '--url', f'http://127.0.0.1:{args.port}/v1/chat/completions',
        '--api', 'openai', '--tokenizer-path', str(args.model_dir.resolve()),
        '--dataset', 'line_by_line', '--dataset-path', str(out / 'requests.jsonl'),
        '--min-prompt-length', '1', '--max-prompt-length', '200000',
        '--no-apply-chat-template', '--max-tokens', '128', '--temperature', '0',
        '--seed', '20260928', '--parallel', '1', '--number', str(len(manifest)),
        '--warmup-num', '0', '--no-test-connection', '--stream', '--connect-timeout', '30',
        '--read-timeout', '600', '--total-timeout', '3600', '--outputs-dir', str(out / 'client')]
    save(out / 'commands.json', dict(server=server_cmd, observer=observer_cmd, client=client_cmd))
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    server = observer = None
    failure = None
    try:
        with (out / 'observer.log').open('w') as log:
            observer = subprocess.Popen(observer_cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        with (out / 'server.log').open('w') as log:
            server = subprocess.Popen(server_cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        until = time.monotonic() + 240
        while 'serving on port' not in (out / 'server.log').read_text():
            if server.poll() is not None or time.monotonic() > until:
                raise RuntimeError('startup failed')
            time.sleep(1)
        with (out / 'client.log').open('w') as log:
            run = subprocess.run(client_cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=3600)
        save(out / 'client-exit.json', dict(code=run.returncode))
        if run.returncode:
            raise RuntimeError('evalscope failed')
        database, = (out / 'client').rglob('benchmark_data.db')
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('select * from result order by start_time').fetchall()
        parsed = []
        for row, case in zip(rows, manifest):
            request = json.loads(row['request'])
            assert all(request[k] == v for k, v in case['request'].items()), 'request reordered/changed'
            messages = pickle.loads(base64.b64decode(row['response_messages']))
            choices = [c for m in messages for c in m.get('choices', [])]
            text = ''.join(c.get('delta', c.get('message', {})).get('content', '') for c in choices)
            finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
            parsed.append(dict(scenario=case['scenario'], variant=case['variant'],
                success=bool(row['success']), prompt_tokens=row['prompt_tokens'],
                completion_tokens=row['completion_tokens'], text=text, finish=finish,
                http_id=messages[0]['id']))
        save(out / 'responses.json', parsed)
        assert len(rows) == len(manifest)
        assert all(r['success'] and r['text'] and r['prompt_tokens'] > 0 and
                   0 < r['completion_tokens'] <= 128 and r['finish'] in [['stop'], ['length']]
                   for r in parsed), 'HTTP/output failure'
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        if observer is not None:
            try:
                observer.wait(timeout=180)
            except subprocess.TimeoutExpired:
                observer.terminate()
                observer.wait(timeout=10)
        save(out / 'exit.json', dict(failure=failure,
            server=server.returncode if server else None,
            observer=observer.returncode if observer else None))
    assert server.returncode == observer.returncode == 0
    assert sha(args.binary) == json.loads((out / 'identity.json').read_text())['binary_sha256']


if __name__ == '__main__':
    main()
