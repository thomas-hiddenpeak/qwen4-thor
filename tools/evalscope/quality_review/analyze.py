"""Report all frozen paired outcomes, without selecting a new acceptance rule."""
from pathlib import Path
import collections
import json

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/quality-review-v2-20260927'
OUT = ROOT / '.q4t-work/e2e/quality-review-v2-20260927'


def metrics(rows):
    return {'n': len(rows), 'baseline_correct': sum(r['baseline_correct'] for r in rows),
            'fixed_correct': sum(r['fixed_correct'] for r in rows),
            'gains': sum(not r['baseline_correct'] and r['fixed_correct'] for r in rows),
            'losses': sum(r['baseline_correct'] and not r['fixed_correct'] for r in rows),
            'both_wrong': sum(not r['baseline_correct'] and not r['fixed_correct'] for r in rows),
            'changed_text': sum(r['baseline_text'] != r['fixed_text'] for r in rows)}


def main():
    assert json.loads((OUT / 'collection.json').read_text())['complete']
    manifest = {r['id']: r for r in json.loads((P / 'all/manifest.json').read_text())}
    baseline = {}
    for stage in ['baseline-first', 'baseline-last']:
        for row in json.loads((OUT / stage / 'results.json').read_text()):
            assert row['id'] not in baseline
            baseline[row['id']] = row
    fixed = {r['id']: r for r in json.loads((OUT / 'fixed/results.json').read_text())}
    assert baseline.keys() == fixed.keys() == manifest.keys()
    pairs = []
    structural = []
    for key, meta in manifest.items():
        b, f = baseline[key], fixed[key]
        for label, row in [('baseline', b), ('fixed', f)]:
            assert row['prompt_sha256'] == meta['prompt_sha256']
            if not row['success'] or row['actual_input'] != meta['length'] or row['finish'] != ['stop']:
                structural.append({'id': key, 'version': label, 'success': row['success'],
                                   'length': row['actual_input'], 'finish': row['finish']})
        pairs.append({'id': key, 'collection': meta['collection'], 'task': meta.get('task', 'legacy_lookup'),
                      'length': meta['length'], 'language': meta.get('language', 'legacy_en'),
                      'representation': meta.get('representation', 'legacy'),
                      'expected': meta['expected'], 'baseline_text': b['text'], 'fixed_text': f['text'],
                      'baseline_correct': b['text'].strip() == meta['expected'],
                      'fixed_correct': f['text'].strip() == meta['expected']})
    new = [r for r in pairs if r['collection'] == 'new']
    assert len(new) == 120
    groups = {}
    for field in ['task', 'length', 'language', 'representation']:
        data = collections.defaultdict(list)
        for row in new:
            data[str(row[field])].append(row)
        groups[field] = {k: metrics(v) for k, v in sorted(data.items())}
    total = metrics(new)
    declined = [{'group': field, 'value': key, **value}
                for field in ['task', 'length'] for key, value in groups[field].items()
                if value['fixed_correct'] < value['baseline_correct']]
    ready = (not structural and total['fixed_correct'] >= total['baseline_correct'] and not declined)
    # Predeclared bounded repeat: one loss per task/length cell, then fill to eight.
    losses = sorted((r for r in new if r['baseline_correct'] and not r['fixed_correct']),
                    key=lambda r: (r['length'], r['task'], r['id']))
    selected = []
    seen = set()
    if not ready and not structural:
        for row in losses:
            cell = row['task'], row['length']
            if cell not in seen and len(selected) < 8:
                selected.append(row['id'])
                seen.add(cell)
        for row in losses:
            if row['id'] not in selected and len(selected) < 8:
                selected.append(row['id'])
    report = {'new': total, 'groups': groups, 'declined_primary_groups': declined,
              'legacy': {label: metrics([r for r in pairs if r['collection'] == label])
                         for label in ['legacy_retrieval', 'legacy_reasoning']},
              'structural_failures': structural, 'eligible_for_performance_screening': ready,
              'decision': 'eligible_for_performance_screening' if ready else 'not_eligible_pending_bounded_repeat' if selected else 'insufficient_or_structural_failure',
              'bounded_repeat_ids': selected, 'runtime_accepted': False,
              'scope': 'Frozen synthetic engineering-record screening; empirical comparison, not population noninferiority, general model quality or a performance result.'}
    assert not (OUT / 'analysis.json').exists()
    (OUT / 'pairs.json').write_text(json.dumps(pairs, indent=2, ensure_ascii=False) + '\n')
    (OUT / 'analysis.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
