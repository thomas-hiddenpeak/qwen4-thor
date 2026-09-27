"""Finish the predeclared review after the collector's real process termination."""
from pathlib import Path
import argparse
import datetime
import json
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/quality-review-v2-20260927'
OUT = ROOT / '.q4t-work/e2e/quality-review-v2-20260927'
TOOLS = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-pid', type=int, required=True)
    args = parser.parse_args()
    proc = Path('/proc') / str(args.collector_pid)
    if proc.exists():
        assert b'quality_review/run.py' in (proc / 'cmdline').read_bytes()
        identity = (proc / 'stat').read_text().split()[21]
    else:
        identity = None
    deadline = datetime.datetime.fromisoformat(json.loads((P / 'plan.json').read_text())['deadline_utc'])
    while proc.exists() and identity is not None:
        try:
            stat = (proc / 'stat').read_text().split()
        except FileNotFoundError:
            break
        if stat[21] != identity or stat[2] == 'Z':
            break
        if datetime.datetime.now(datetime.timezone.utc) > deadline + datetime.timedelta(seconds=40):
            raise RuntimeError('deadline reached; incomplete review, do not accept')
        time.sleep(1)
    assert json.loads((OUT / 'collection.json').read_text())['complete']
    for step in ['analyze', 'repeat', 'seal']:
        if step == 'repeat':
            analysis = json.loads((OUT / 'analysis.json').read_text())
            if not analysis['bounded_repeat_ids']:
                continue
        label = 'analysis' if step == 'analyze' else step
        log_path = P.parent / ('quality-review-v2-' + label + '-20260927.log')
        assert not log_path.exists()
        with log_path.open('w') as log:
            subprocess.run(['python3', str(TOOLS / (step + '.py'))], cwd=ROOT,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        print('Completed ' + step, flush=True)
    print((OUT / 'summary.json').read_text(), flush=True)


if __name__ == '__main__':
    main()
