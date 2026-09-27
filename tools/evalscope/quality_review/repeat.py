"""One bounded repeat of preselected losses; never replaces the original scores."""
from pathlib import Path
import datetime
import hashlib
import json
import os
import signal
import subprocess

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/quality-review-v2-20260927'
OUT = ROOT / '.q4t-work/e2e/quality-review-v2-20260927'


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def main():
    analysis = json.loads((OUT / 'analysis.json').read_text())
    ids = analysis['bounded_repeat_ids']
    assert not analysis['eligible_for_performance_screening'] and 0 < len(ids) <= 8
    plan = json.loads((P / 'plan.json').read_text())
    deadline = datetime.datetime.fromisoformat(plan['deadline_utc'])
    manifest = json.loads((P / 'all/manifest.json').read_text())
    requests = [json.loads(x) for x in (P / 'all/requests.jsonl').read_text().splitlines()]
    by_id = {m['id']: (m, r) for m, r in zip(manifest, requests)}
    fixtures = P / 'repeat-inputs'
    fixtures.mkdir()
    save(fixtures / 'manifest.json', [by_id[key][0] for key in ids])
    (fixtures / 'requests.jsonl').write_text(''.join(json.dumps(by_id[key][1], ensure_ascii=False) + '\n' for key in ids))
    save(fixtures / 'binding.json', {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                                   for f in fixtures.iterdir() if f.is_file()})
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    results = {}
    for label in ['baseline', 'fixed']:
        assert datetime.datetime.now(datetime.timezone.utc) < deadline
        binary = P / ('q4t-' + label)
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == plan[label + '_sha256']
        out = OUT / ('repeat-' + label)
        cmd = ['python3', 'tools/evalscope/run_acceptance.py', '--mode', 'quality',
               '--output', str(out), '--binary', str(binary), '--model-dir', plan['model_dir'],
               '--fixtures', str(fixtures), '--startup-timeout', '600']
        save(OUT / ('repeat-' + label + '-command.json'), cmd)
        print('Starting bounded repeat ' + label + ': ' + str(len(ids)), flush=True)
        with (OUT / ('repeat-' + label + '-driver.log')).open('w') as log:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=(deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                save(OUT / 'repeat-deadline.json', {'stage': label, 'decision': 'incomplete_not_accepted'})
                raise
        save(OUT / ('repeat-' + label + '-driver-exit.json'), {'exit': code})
        terminal = json.loads((out / 'exit.json').read_text())
        assert code in (0, 1) and terminal['server'] == 0 and terminal['completed'] == len(ids)
        assert terminal['failure'] in (None, 'RuntimeError: quality acceptance failed')
        rows = json.loads((out / 'results.json').read_text())
        results[label] = {r['id']: r for r in rows}
        assert set(results[label]) == set(ids)
    original = {r['id']: r for r in json.loads((OUT / 'pairs.json').read_text())}
    pairs = []
    for key in ids:
        old = original[key]
        b, f = results['baseline'][key], results['fixed'][key]
        for row in [b, f]:
            assert row['success'] and row['length_match'] and row['finish'] == ['stop']
            assert row['prompt_sha256'] == by_id[key][0]['prompt_sha256']
        stable = b['text'] == old['baseline_text'] and f['text'] == old['fixed_text']
        repeated_loss = b['exact_match'] and not f['exact_match']
        pairs.append({'id': key, 'baseline_text': b['text'], 'fixed_text': f['text'],
                      'same_original_texts': stable, 'loss_repeated': repeated_loss})
    save(OUT / 'repeat.json', {'pairs': pairs,
         'original_scores_unchanged': True,
         'stable_losses': sum(r['same_original_texts'] and r['loss_repeated'] for r in pairs),
         'decision': 'sample_regression_reproduced' if any(r['same_original_texts'] and r['loss_repeated'] for r in pairs) else 'insufficient_or_unstable',
         'runtime_accepted': False, 'population_quality_proven': False})
    print(json.dumps(pairs, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
