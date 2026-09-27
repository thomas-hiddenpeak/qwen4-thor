"""Independently parse frozen prompts and verify new answers with SQL."""
from pathlib import Path
import hashlib
import json
import re
import sqlite3

from transformers import AutoTokenizer

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/quality-review-v2-20260927'
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def main():
    binding = json.loads((P / 'fixture-binding.json').read_text())
    for name, expected in binding.items():
        assert hashlib.sha256((P / name).read_bytes()).hexdigest() == expected
    manifest = json.loads((P / 'all/manifest.json').read_text())
    requests = [json.loads(x) for x in (P / 'all/requests.jsonl').read_text().splitlines()]
    tok = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
    columns = ['table_name', 'service', 'env', 'code', 'revision', 'status',
               'owner', 'zone', 'amount', 'priority']
    queries = {
        'lookup': "select code from r where service=? and env='prod'",
        'latest_revision': 'select code from r where service=? order by revision desc limit 1',
        'approved_revision': "select code from r where service=? and status='approved' order by revision desc limit 1",
        'join': "select c.code from r a join r b on a.owner=b.owner join r c on b.zone=c.zone where a.table_name='service' and b.table_name='owner' and c.table_name='zone' and a.service=?",
        'filtered_sum': "select sum(amount) from r where service=? and status='accepted'",
        'filtered_count': "select count(*) from r where service=? and status='accepted'",
        'filtered_max': 'select code from r where service=? order by priority desc limit 1',
        'intersection': "select d.code from r d where d.table_name='directory' and d.service in (select service from r where table_name='eligible' intersect select service from r where table_name='available')",
    }
    new_count = 0
    reports = []
    for meta, req in zip(manifest, requests):
        prompt = req['prompt']
        assert hashlib.sha256(prompt.encode()).hexdigest() == meta['prompt_sha256']
        assert len(tok.encode(prompt, add_special_tokens=False)) == meta['length']
        if meta['collection'] != 'new':
            continue
        blocks = re.findall(r'AUTHORITATIVE REGISTER / 权威登记表\n(.*?)\nEND REGISTER / 登记表结束', prompt, re.S)
        assert len(blocks) == 3
        rows = []
        for line in [line for block in blocks for line in block.splitlines()]:
            if line.startswith('{'):
                row = json.loads(line)
            else:
                assert line.startswith(('Registered record: ', '已登记记录：'))
                fields = line.removeprefix('Registered record: ').removeprefix('已登记记录：').rstrip('.。')
                row = dict(pair.split('=', 1) for pair in re.split(r'; |；', fields))
                for key in ['code', 'revision', 'amount', 'priority']:
                    if key in row:
                        row[key] = int(row[key])
            rows.append(row)
        normalize = lambda data: sorted(json.dumps(row, sort_keys=True) for row in data)
        assert normalize(rows) == normalize(meta['oracle_records'])
        with sqlite3.connect(':memory:') as db:
            db.execute('create table r (table_name text, service text, env text, code integer, revision integer, status text, owner text, zone text, amount integer, priority integer)')
            db.executemany('insert into r values (' + ','.join('?' for _ in columns) + ')',
                           [[row.get('table' if c == 'table_name' else c) for c in columns] for row in rows])
            params = () if meta['task'] == 'intersection' else (meta['target'],)
            result = db.execute(queries[meta['task']], params).fetchall()
        assert len(result) == 1 and f'{result[0][0]:06d}' == meta['expected']
        new_count += 1
        reports.append({'id': meta['id'], 'parsed_records': len(rows), 'sql_expected': f'{result[0][0]:06d}'})
    assert len(manifest) == len(requests) == 146 and new_count == 120
    halves = [json.loads((P / name / 'manifest.json').read_text()) for name in ['baseline-first', 'baseline-last']]
    assert set(x['id'] for x in halves[0]).isdisjoint(x['id'] for x in halves[1])
    assert {x['id'] for half in halves for x in half} == {x['id'] for x in manifest}
    out = P / 'fixture-audit.json'
    assert not out.exists()
    out.write_text(json.dumps({'new_answers_verified_from_rendered_prompts_using_sql': 120,
                              'all_prompt_lengths_verified': 146, 'halves_disjoint_complete': True,
                              'records': reports}, indent=2) + '\n')
    print('120 new answers verified independently with SQL; 146 exact prompt lengths; complete disjoint split.')


if __name__ == '__main__':
    main()
