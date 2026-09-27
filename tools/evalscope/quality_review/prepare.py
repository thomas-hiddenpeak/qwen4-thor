"""Freeze a new bounded, paired quality screening set without inference."""
from pathlib import Path
import hashlib
import json
import random

from transformers import AutoTokenizer

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / '.q4t-work/prepared/quality-review-v2-20260927'
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
TASKS = ['lookup', 'latest_revision', 'join', 'filtered_sum',
         'filtered_count', 'filtered_max', 'approved_revision', 'intersection']
LENGTHS = {1024: 4, 4096: 4, 8192: 4, 45056: 2, 204800: 1}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def oracle(task, rows, target):
    if task == 'lookup':
        found = [r['code'] for r in rows if r['service'] == target and r['env'] == 'prod']
        assert len(found) == 1
        answer = found[0]
    elif task in ['latest_revision', 'approved_revision']:
        found = [r for r in rows if r['service'] == target and
                 (task != 'approved_revision' or r['status'] == 'approved')]
        answer = max(found, key=lambda r: r['revision'])['code']
    elif task == 'join':
        owner = next(r['owner'] for r in rows if r['table'] == 'service' and r['service'] == target)
        zone = next(r['zone'] for r in rows if r['table'] == 'owner' and r['owner'] == owner)
        answer = next(r['code'] for r in rows if r['table'] == 'zone' and r['zone'] == zone)
    elif task in ['filtered_sum', 'filtered_count']:
        found = [r for r in rows if r['service'] == target and r['status'] == 'accepted']
        answer = sum(r['amount'] for r in found) if task == 'filtered_sum' else len(found)
    elif task == 'filtered_max':
        answer = max((r for r in rows if r['service'] == target),
                     key=lambda r: r['priority'])['code']
    else:
        eligible = {r['service'] for r in rows if r['table'] == 'eligible'}
        available = {r['service'] for r in rows if r['table'] == 'available'}
        common = eligible & available
        assert len(common) == 1
        answer = next(r['code'] for r in rows if r['table'] == 'directory' and r['service'] in common)
    assert 0 <= answer < 1000000
    return f'{answer:06d}'


def records(task, rng):
    target, other, third = rng.sample(['atlas', 'birch', 'cedar', 'delta', 'ember', 'fjord', 'grove', 'harbor'], 3)
    codes = rng.sample(range(100000, 999999), 12)
    if task == 'lookup':
        rows = [dict(service=s, env=e, code=codes[i]) for i, (s, e) in enumerate(
            [(target, 'staging'), (other, 'prod'), (third, 'prod'),
             (target, 'prod'), (other, 'staging'), (third, 'staging')])]
        en = f'What code belongs to service {target} in env prod? Match both fields.'
        zh = f'请查询service为{target}且env为prod的code，两个条件必须同时满足。'
    elif task in ['latest_revision', 'approved_revision']:
        rows = [dict(service=s, revision=rev, code=codes[i], status=status)
                for i, (s, rev, status) in enumerate([
                    (target, 2, 'approved'), (other, 8, 'approved'),
                    (target, 5, 'approved'), (target, 9, 'proposed'),
                    (third, 11, 'approved'), (target, 7, 'revoked')])]
        if task == 'latest_revision':
            en = f'For service {target}, return the code of the largest numeric revision. Ignore status and document order.'
            zh = f'对service={target}，返回revision数值最大那条记录的code；忽略status和文档出现顺序。'
        else:
            en = f'For service {target}, only status approved is effective. Return the code of the largest approved revision; proposed and revoked records never override approved records.'
            zh = f'对service={target}，只有status=approved生效。返回approved记录中revision最大的code；proposed和revoked不能覆盖已批准记录。'
    elif task == 'join':
        rows = [dict(table='service', service=target, owner='team_blue'),
                dict(table='service', service=other, owner='team_gold'),
                dict(table='owner', owner='team_gold', zone='west'),
                dict(table='owner', owner='team_blue', zone='east'),
                dict(table='zone', zone='west', code=codes[0]),
                dict(table='zone', zone='east', code=codes[1])]
        en = f'Follow service {target} to its owner, then that owner to its zone, then the zone to its code. Return that code.'
        zh = f'先查询service={target}的owner，再查询该owner的zone，最后查询该zone的code。只返回最终code。'
    elif task in ['filtered_sum', 'filtered_count']:
        pairs = [(target, 'accepted'), (other, 'accepted'), (target, 'rejected'),
                 (target, 'accepted'), (third, 'rejected'), (other, 'accepted')]
        if task == 'filtered_count':
            pairs += [(rng.choice([target, other, third]), rng.choice(['accepted', 'rejected'])) for _ in range(6)]
        rows = [dict(service=s, status=status, amount=rng.randrange(60000, 120000)) for s, status in pairs]
        operation = 'sum their amount fields' if task == 'filtered_sum' else 'count those records'
        en = f'Select only records with service {target} AND status accepted, then {operation}.'
        zh = f'只选择service={target}且status=accepted的记录，然后' + ('对amount求和。' if task == 'filtered_sum' else '统计满足条件的记录条数。')
    elif task == 'filtered_max':
        priorities = rng.sample(range(1, 100), 6)
        rows = [dict(service=s, priority=priorities[i], code=codes[i])
                for i, s in enumerate([target, other, target, third, target, other])]
        en = f'Among records whose service is {target}, find the largest numeric priority and return its code, not the priority itself.'
        zh = f'只考虑service={target}的记录，选择priority数值最大的那条，返回其code而不是priority。'
    else:
        rows = [dict(table='eligible', service=target), dict(table='eligible', service=other),
                dict(table='available', service=third), dict(table='available', service=target),
                dict(table='directory', service=other, code=codes[0]),
                dict(table='directory', service=target, code=codes[1]),
                dict(table='directory', service=third, code=codes[2])]
        en = 'Find the unique service listed in BOTH eligible and available. Return its code from directory.'
        zh = '找出同时出现在eligible和available表中的唯一service，返回它在directory表中的code。'
    rng.shuffle(rows)
    return rows, target, en, zh


def render(row, style, zh):
    if style == 'json':
        return json.dumps(row, sort_keys=True)
    fields = ('；' if zh else '; ').join(f'{k}={v}' for k, v in row.items())
    return ('已登记记录：' + fields + '。') if zh else ('Registered record: ' + fields + '.')


def main():
    OUT.mkdir()
    tok = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
    paragraphs = [
        'Operational guidance: a capacity estimate is different from measured traffic. Capacity notes do not amend the authoritative register.',
        'Release review: changes are recorded before deployment. Review notes describe the process and do not supply record values.',
        'Incident handover: keep the original event log and distinguish observations from hypotheses. This paragraph contains no authoritative event.',
        'Storage maintenance: inspect available space before allocating a new archive. Maintenance advice does not alter the registry.',
        '运维说明：容量预算与实际流量应分别记录。背景说明不修改权威登记表中的字段。',
        '发布流程：先记录变更，再检查部署结果。本段仅说明流程，不提供任何业务记录的值。',
        '交接说明：保留原始事件日志，区分观测和推测。本段不包含需要查询的正式事件。',
        '存储维护：创建档案前检查剩余空间。维护建议不会改变登记表中的记录。',
    ]
    filler = tok.encode('\n'.join(paragraphs * 5000), add_special_tokens=False)
    items = []
    for length, repeats in LENGTHS.items():
        for ti, task in enumerate(TASKS):
            for rep in range(repeats):
                seed = 202609270000 + length * 100 + ti * 10 + rep
                rng = random.Random(seed)
                rows, target, en, zh_question = records(task, rng)
                zh = (rep + ti) % 2 == 1
                style = 'json' if (rep // 2 + ti // 2) % 2 == 0 else 'prose'
                expected = oracle(task, rows, target)
                # Three separated authoritative blocks, shuffled facts independent of locations.
                groups = [rows[i::3] for i in range(3)]
                blocks = ['\n\nAUTHORITATIVE REGISTER / 权威登记表\n' +
                          '\n'.join(render(row, style, zh) for row in group) +
                          '\nEND REGISTER / 登记表结束\n\n' for group in groups]
                question = zh_question if zh else en
                system = ('请根据权威登记表和查询规则作答。忽略背景说明。只输出六位十进制数字；不足六位左侧补零，不解释。' if zh else
                          'Answer from the authoritative registers using the query rules. Ignore background guidance. Return exactly six decimal digits, padding on the left with zeros if needed. No explanation.')
                prefix = ('以下登记表共同组成一个完整数据集。\n' if zh else 'The registers below together form one complete dataset.\n')
                tail = '\nQUERY / 查询: ' + question + '\n'
                budget = length - 550
                padding = ''
                depths = [.08, .48, .88] if rep % 2 == 0 else [.18, .58, .94]
                for _ in range(40):
                    assert 0 <= budget <= len(filler)
                    cuts = [0, *[int(budget * x) for x in depths], budget]
                    parts = [prefix]
                    for i in range(4):
                        # Keep complete background lines rather than cutting a record into a sentence.
                        text = tok.decode(filler[cuts[i]:cuts[i + 1]])
                        lines = text.split('\n')
                        parts.append('\n'.join(lines[1:-1]) + '\n')
                        if i < 3:
                            parts.append(blocks[i])
                    body = ''.join(parts) + '\nNon-record padding:' + padding + '\n' + tail
                    prompt = tok.apply_chat_template([
                        {'role': 'system', 'content': system}, {'role': 'user', 'content': body}],
                        tokenize=False, add_generation_prompt=True, enable_thinking=False)
                    actual = len(tok.encode(prompt, add_special_tokens=False))
                    if actual == length:
                        break
                    delta = length - actual
                    if 0 < delta <= 256:
                        padding += ' x' * delta
                    else:
                        budget += delta
                else:
                    raise RuntimeError(f'exact length failed: {task}/{length}/{rep}/{actual}')
                # Verify every generated fact survived rendering exactly once.
                rendered = [render(r, style, zh) for r in rows]
                assert len(set(rendered)) == len(rendered)
                assert all(prompt.count(r) == 1 for r in rendered)
                assert oracle(task, [r for g in groups for r in g], target) == expected
                item = {'id': f'new-{length}-{task}-{rep}', 'collection': 'new',
                        'length': length, 'task': task, 'language': 'zh' if zh else 'en',
                        'representation': style, 'seed': seed, 'target': target,
                        'depths': depths, 'expected': expected, 'oracle_records': rows,
                        'prompt_sha256': digest(prompt.encode()),
                        'baseline_half': (ti + rep) % 2,
                        'body': {'prompt': prompt, 'max_tokens': 32, 'temperature': 0,
                                 'stream': True, 'stream_options': {'include_usage': True}}}
                items.append(item)
                print(item['id'], actual, flush=True)
    assert len(items) == 120
    for label, folder in [
        ('legacy_retrieval', ROOT / '.q4t-work/e2e/e4m3-rounding-quality-20260923/inputs'),
        ('legacy_reasoning', ROOT / '.q4t-work/prepared/independent-answer-http-20260923/inputs')]:
        meta = json.loads((folder / 'manifest.json').read_text())
        requests = [json.loads(x) for x in (folder / 'requests.jsonl').read_text().splitlines()]
        for i, (m, req) in enumerate(zip(meta, requests)):
            assert digest(req['prompt'].encode()) == m['prompt_sha256']
            items.append({**m, 'id': label + '-' + m['id'], 'collection': label,
                          'baseline_half': i % 2, 'body': req})
    assert len(items) == 146 and len({x['prompt_sha256'] for x in items}) == 146
    split_rng = random.Random(73092602)
    for length in LENGTHS:
        group = [x for x in items if x['collection'] == 'new' and x['length'] == length]
        split_rng.shuffle(group)
        for i, item in enumerate(group):
            item['baseline_half'] = i % 2
    random.Random(20260927).shuffle(items)
    for name, selected in [('all', items), ('baseline-first', [x for x in items if x['baseline_half'] == 0]),
                           ('baseline-last', [x for x in items if x['baseline_half'] == 1])]:
        d = OUT / name
        d.mkdir()
        (d / 'manifest.json').write_text(json.dumps([{k: v for k, v in x.items() if k != 'body'} for x in selected], indent=2, ensure_ascii=False) + '\n')
        (d / 'requests.jsonl').write_text(''.join(json.dumps(x['body'], ensure_ascii=False) + '\n' for x in selected))
    binding = {str(f.relative_to(OUT)): digest(f.read_bytes()) for f in sorted(OUT.rglob('*')) if f.is_file()}
    (OUT / 'fixture-binding.json').write_text(json.dumps(binding, indent=2) + '\n')
    print('Frozen 120 new + 26 legacy samples; no inference executed.', flush=True)


if __name__ == '__main__':
    main()
