"""Freeze the bounded MoE business routing set v1 (2026-09-30).

Produces a frozen plan plus single-turn request files for the NEW synthetic
entries. Multi-turn entries freeze the PLAN (groups, turn tasks, seeds,
output caps, history rule); the realized assistant content is the actual
greedy output of the previous turn and is recorded after collection.

Splits (by source/template/dialogue group, never by individual request):
  calibration    - per-layer static hot lists + initial slot sizing
  policy         - static vs LRU vs hybrid + per-layer capacity comparison
  final_validation - capacity-miss curve validation ONLY; excluded from
                     list, capacity and policy selection by construction

All entries are synthetic and marked as such. Reuse entries bind existing
traces by file SHA and request identity; no rerun.
"""
import argparse
import hashlib
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED = 20260930
OUTPUT_CAP = 128
LONG_OUTPUT_CAP = 512
ACCEPT_OUTPUT_CAP = 256

MAIN_STUDY = ROOT / '.q4t-work/moe-router-study-20260928/chat'
SCENARIOS = ROOT / '.q4t-work/moe-evalscope-scenarios-20260928'
QUALITY = ROOT / '.q4t-work/prepared/quality-review-v2-20260927/all'

# Main study: 16 unique chat requests (4 domains x 2 seeds x {1K, 8K});
# trace request ids 1..16 (17..20 are the four exact repeats, excluded).
MAIN_UNIQUE = [
    'python-s2-1024', 'math-s2-1024', 'chinese-s2-8192', 'chinese-s2-1024',
    'systems-s2-1024', 'math-s1-8192', 'python-s1-1024', 'python-s1-8192',
    'python-s2-8192', 'systems-s1-8192', 'systems-s2-8192', 'systems-s1-1024',
    'chinese-s1-8192', 'chinese-s1-1024', 'math-s2-8192', 'math-s1-1024']
MAIN_DOMAIN = {'python': 'code', 'systems': 'code', 'math': 'structured',
               'chinese': 'writing'}

# Scenario trace request ids by category (variant 1..4 -> 4 sizes).
SCENARIO_CAL = {'support': [1, 7, 13, 19], 'orders': [2, 8, 14, 20],
                'debug': [3, 9, 15, 21], 'writing': [4, 10, 16, 22]}
SCENARIO_POL = {'incident': [5, 11, 17, 23], 'retrieval': [6, 12, 18, 24]}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def notes_block(rng, count, topic):
    lines = []
    for i in range(count):
        lines.append(f'Record {i}: {topic} {i % 7} '
                     f'value={rng.randrange(1000)} state={i % 3}.')
    return '\n'.join(lines)


def multiturn_groups():
    """Four dialogue groups x 3 turns, one domain each (synthetic)."""
    rng = random.Random(SEED)
    doc = notes_block(rng, 60, 'the storage worker reads selected rows')
    records = notes_block(rng, 20, 'service order')
    directory = '\n'.join(
        f'Service S{i:02d}: code=C{i:03d}, region={i % 4}.' for i in range(10))
    approved = 'Approved services: S01, S03, S07, S09.'
    return [
        dict(group='mt-code', domain='code', turns=[
            'Implement a Python function parse_log(lines) that parses service '
            'log lines of the form "ts level service message" and returns a '
            'dict of per-service error counts. Include input validation.',
            'Add unit tests for parse_log covering empty input, malformed '
            'lines, and duplicate services.',
            'Refactor parse_log to stream lines from a file path instead of a '
            'list, keeping the same contract for the returned count dict.']),
        dict(group='mt-support', domain='support', turns=[
            'You are an e-commerce support agent. Customer says order O1001 '
            'has not arrived after 10 days. Records: O1001 region=2 units=3 '
            'price=49 status=shipped carrier=REG. Write a reply with '
            'confirmable facts and next steps. Do not invent an ETA.',
            'The customer escalates: "I want a refund." Policy: refunds are '
            'only available within 7 days of shipment. Write the reply '
            'applying the policy and offer alternatives.',
            'Summarize this conversation into five bullet points for the ops '
            'log: issue, facts, policy applied, resolution, follow-up.']),
        dict(group='mt-structured', domain='structured', turns=[
            f'Count records per region where state=0.\n{records}',
            f'Now join with this directory and report total records per '
            f'service code for state=0 records.\n{directory}',
            f'Find services appearing in both the state=0 set and the '
            f'approved set; return their codes sorted.\n{approved}']),
        dict(group='mt-retrieval', domain='retrieval', turns=[
            f'Document:\n{doc}\nFind all statements about the storage worker '
            'and list them with record numbers.',
            'Which records describe the worker reading selected rows? Quote '
            'the relevant sentences.',
            'Summarize the document\'s claims about storage in three '
            'sentences.']),
    ]


def taskswitch_groups():
    """Four groups x 3 turns; the task switches between turns (synthetic)."""
    rng = random.Random(SEED + 1)
    doc = notes_block(rng, 40, 'the scheduler processes pending requests')
    invoices = '\n'.join(
        f'INV-{i:03d} amount={rng.randrange(100, 900)} status='
        f'{"duplicate" if i % 5 == 0 else "paid"}' for i in range(15))
    policy = notes_block(rng, 30, 'refund and billing policy')
    return [
        dict(group='ts-code-support-structured', turns=[
            ('code', 'Write a function that validates ISO8601 timestamps and '
                     'returns the parsed components.'),
            ('support', 'A customer asks why their export failed with '
             '"invalid timestamp". Using the validation rules you defined, '
             'write a customer-facing explanation.'),
            ('structured', 'Given these 15 export records, count failures per '
             'error code: ' + ' '.join(
                 f'E{i}={i % 4}' for i in range(15)) + '.')]),
        dict(group='ts-retrieval-code-support', turns=[
            ('retrieval', f'Document:\n{doc}\nFind all mentions of the '
             'scheduler and list them with record numbers.'),
            ('code', 'Write a Python function that estimates scheduler state '
                     'bytes for a given number of pending requests, given a '
                     'per-request byte cost.'),
            ('support', 'A user asks why long queues are slower. Write a '
                        'support reply using the estimate logic.'),
        ]),
        dict(group='ts-structured-writing-code', turns=[
            ('structured', 'Count orders per status from these 20 records: '
             + ' '.join(f'O{i}={"delayed" if i % 3 == 0 else "shipped"}'
                        for i in range(20)) + '.'),
            ('writing', 'Write a 150-word internal memo summarizing the '
                        'order status distribution.'),
            ('code', 'Write a function that generates such a memo from a '
                     'status dict.'),
        ]),
        dict(group='ts-support-structured-retrieval', turns=[
            ('support', f'Customer reports duplicate billing. Records:\n'
                        f'{invoices}\nDraft a reply.'),
            ('structured', 'From the invoice records, compute the total '
                           'overcharge across duplicate entries.'),
            ('retrieval', f'Policy document:\n{policy}\nFind the clause '
                          'governing duplicate-billing refunds and quote it.'),
        ]),
    ]


def long_output_cases():
    rng = random.Random(SEED + 2)
    design = notes_block(rng, 200, 'the service design document describes')
    policy = notes_block(rng, 200, 'the support policy record states')
    records = notes_block(rng, 200, 'the engineering record logs')
    longdoc = notes_block(rng, 200, 'the long document discusses memory and '
                                    'scheduling')
    return [
        dict(id='lo-code', domain='code',
             prompt=f'Write a detailed technical review (at least 500 words) '
                    f'of this service design document: cover measurement, '
                    f'storage, scheduling, correctness, and risks.\n{design}'),
        dict(id='lo-support', domain='support',
             prompt=f'Write a detailed customer service SOP (at least 500 '
                    f'words) for order-delay escalations, based on these '
                    f'policy records.\n{policy}'),
        dict(id='lo-structured', domain='structured',
             prompt=f'Produce a detailed structured analysis (at least 500 '
                    f'words) of these engineering records: distributions, '
                    f'anomalies, joins, and conclusions.\n{records}'),
        dict(id='lo-retrieval', domain='retrieval',
             prompt=f'Write a detailed retrieval report (at least 500 words) '
                    f'over this document: locate, quote, and synthesize all '
                    f'claims about memory and scheduling.\n{longdoc}'),
    ]


def quality_subset():
    manifest = json.loads((QUALITY / 'manifest.json').read_text())
    lines = (QUALITY / 'requests.jsonl').read_text().splitlines()
    by_sha = {}
    for line in lines:
        prompt = json.loads(line)['prompt']
        by_sha[hashlib.sha256(prompt.encode()).hexdigest()] = prompt
    entries = [e for e in manifest if e['collection'] == 'new']
    items = sorted(entries, key=lambda e: e['id'])
    pick = {'8k-policy': [e for e in items if e['length'] == 8192],
            '44k-fv': [e for e in items if e['length'] == 45056],
            '200k-fv': [e for e in items if e['length'] == 204800],
            '8k-fv': [e for e in items if e['length'] == 8192][:8]}
    out = {}
    for key, selected in pick.items():
        out[key] = []
        for entry in selected:
            prompt = by_sha[entry['prompt_sha256']]
            out[key].append(dict(id=entry['id'], length=entry['length'],
                                 prompt_sha256=entry['prompt_sha256'],
                                 prompt=prompt))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=False)

    entries = []
    # Reuse: calibration.
    workload = json.loads((MAIN_STUDY / 'workload.json').read_text())
    by_name = {r['name']: r for r in workload['requests']}
    for i, name in enumerate(MAIN_UNIQUE, 1):
        r = by_name[name]
        entries.append(dict(
            id=f'reuse-main-{i:02d}', split='calibration',
            source='main-study-20260928', domain=MAIN_DOMAIN[r['domain']],
            synthetic=True, mode='reuse',
            trace_dir=str(MAIN_STUDY / 'trace'), trace_request=i,
            name=name, seed=r['seed'], target_length=r['target_length'],
            output_cap=128,
            prompt_sha256=hashlib.sha256(r['prompt'].encode()).hexdigest()))
    scenario_reqs = [json.loads(l) for l in
                     (SCENARIOS / 'requests.jsonl').read_text().splitlines()]
    scenario_resp = json.loads((SCENARIOS / 'responses.json').read_text())
    for split, table in (('calibration', SCENARIO_CAL),
                         ('policy', SCENARIO_POL)):
        for domain, ids in table.items():
            for j, rid in enumerate(ids, 1):
                req = scenario_reqs[rid - 1]
                resp = scenario_resp[rid - 1]
                entries.append(dict(
                    id=f'reuse-scen-{domain}-{j}', split=split,
                    source='scenarios-20260928', domain=domain,
                    synthetic=True, mode='reuse',
                    trace_dir=str(SCENARIOS / 'trace'), trace_request=rid,
                    variant=resp['variant'], prompt_tokens=resp['prompt_tokens'],
                    output_cap=128,
                    prompt_sha256=hashlib.sha256(
                        json.dumps(req, sort_keys=True,
                                   ensure_ascii=False).encode()).hexdigest()))
    # New: multi-turn and task-switching (plan frozen; realized on run).
    for g in multiturn_groups():
        entries.append(dict(
            id=g['group'], split='policy', source='business-set-v1',
            domain=g.get('domain', 'mixed'), synthetic=True, mode='multiturn',
            turns=g['turns'], output_cap=OUTPUT_CAP,
            history_rule='actual greedy output of previous turn'))
    for g in taskswitch_groups():
        entries.append(dict(
            id=g['group'], split='policy', source='business-set-v1',
            domain='mixed', synthetic=True, mode='taskswitch',
            turns=[dict(task=t, domain=d) for d, t in g['turns']],
            output_cap=OUTPUT_CAP,
            history_rule='actual greedy output of previous turn'))
    # New: long output.
    for case in long_output_cases():
        entries.append(dict(
            id=case['id'], split='policy', source='business-set-v1',
            domain=case['domain'], synthetic=True, mode='single',
            prompt=case['prompt'], output_cap=LONG_OUTPUT_CAP,
            prompt_sha256=hashlib.sha256(case['prompt'].encode()).hexdigest()))
    # New: quality-review subsets + acceptance tier.
    qs = quality_subset()
    for key, split, cap in (('8k-policy', 'policy', OUTPUT_CAP),
                            ('44k-fv', 'final_validation', OUTPUT_CAP),
                            ('200k-fv', 'final_validation', OUTPUT_CAP),
                            ('8k-fv', 'final_validation', OUTPUT_CAP)):
        for item in qs[key]:
            entries.append(dict(
                id=f'quality-{key}-{item["id"]}', split=split,
                source='quality-review-20260927', domain='structured',
                synthetic=True, mode='single', prompt=item['prompt'],
                output_cap=cap, length=item['length'],
                prompt_sha256=item['prompt_sha256']))
    entries.append(dict(
        id='accept-261888', split='final_validation',
        source='acceptance-tier-20260930', domain='technical-notes',
        synthetic=True, mode='accept-tier', output_cap=ACCEPT_OUTPUT_CAP,
        generation='prepare_inputs.py algorithm, seed 20260920, length 261888'))

    plan = dict(
        schema=1, date='2026-09-30', seed=SEED,
        scope=('Bounded business routing set v1 for MoE residency analysis. '
               'All entries synthetic and marked. Single Thor, single stream, '
               'max_seq=1, text greedy, MTP/media off, max_prefill=8192, '
               'enable_thinking=false. New collection at max_len=262144; '
               'reuse entries were collected at 208896 (routing is '
               'capacity-independent; one cross-check request verifies). '
               'final_validation is excluded from list, capacity and policy '
               'selection by construction.'),
        conditions=dict(max_seq=1, max_prefill=8192, max_len_new=262144,
                        max_len_reuse=208896, mtp=False, media=False,
                        greedy=True, enable_thinking=False),
        binary_sha256_target='3b414633df3f68190afa41787beb19b991fce34e5657e3f06da0617462c75ace',
        entries=entries)
    (out / 'plan.json').write_text(json.dumps(plan, indent=1, ensure_ascii=False))

    # Single-turn request files for new entries (chat format, no template).
    requests = []
    for e in entries:
        if e['mode'] not in ('single', 'accept-tier'):
            continue
        prompt = e.get('prompt')
        if e['mode'] == 'accept-tier':
            prompt = None  # generated at run time by prepare_inputs.py
        requests.append(dict(entry=e['id'], prompt=prompt,
                             output_cap=e['output_cap']))
    (out / 'single-turn.json').write_text(
        json.dumps(requests, indent=1, ensure_ascii=False))
    print(f'plan: {len(entries)} entries '
          f'({sum(e["split"] == "calibration" for e in entries)} calibration, '
          f'{sum(e["split"] == "policy" for e in entries)} policy, '
          f'{sum(e["split"] == "final_validation" for e in entries)} '
          f'final_validation); '
          f'{sum(e["mode"] == "reuse" for e in entries)} reuse, '
          f'{sum(e["mode"] != "reuse" for e in entries)} new')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
