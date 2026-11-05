"""Offline additive completion resources and independently verified archive."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def counters(raw):
    return {k: int(v) * 1024 for k, v in
            re.findall(r'^(\w+):\s+(\d+) kB', raw, re.M)}


def resources(folder):
    plan = read(folder / 'plan.json')
    base = Path(plan['prior_plan']).parents[3]
    old = read(base / 'protection-windows-partial.json')
    windows = old['windows']
    if len(windows) != 47:
        raise RuntimeError('expected 47 previous complete performance windows')
    source = Path(plan['output']) / plan['steps'][0]['name']
    case, = read(source / 'results.json')
    events = [json.loads(line) for line in (source / 'events.jsonl').read_text().splitlines()]
    bounds = {e['event']: e['monotonic_seconds'] for e in events
              if e.get('case') == case['id']}
    value = dict(samples=0, minimum_mem_available=None,
        maximum_total_minus_available=0, maximum_q4t_rss=0,
        maximum_q4t_swap=0, clocks={}, temperatures={})
    entry = dict(mode='single-plain', arm='parent', case=case['id'],
        source=str(source), reused=False, separate_service_start=True,
        telemetry=str(source.parent / 'unified-memory.jsonl'),
        begin=bounds['client_before'], end=bounds['client_after'],
        resources=value, rows=[])
    for row in case['rows']:
        entry['rows'].append(dict(response_id=row['response_id'],
            prompt_sha256=row['prompt_sha256'], input_tokens=row['input_tokens'],
            output_sha256=row['output_sha256'], output_tokens=row['output_tokens'],
            ttft=row['ttft'], latency=row['latency'],
            decode_tps=(row['output_tokens'] - 1) / (row['latency'] - row['ttft']),
            acceptance=None, suffix=None))
    for line in Path(entry['telemetry']).open():
        sample = json.loads(line)
        if (sample['monotonic'] < entry['begin'] or
                sample['sample_finished_monotonic'] > entry['end']):
            continue
        value['samples'] += 1
        mem = counters(sample['meminfo'])
        available = mem['MemAvailable']
        value['minimum_mem_available'] = min(value['minimum_mem_available'] or available, available)
        value['maximum_total_minus_available'] = max(value['maximum_total_minus_available'],
                                                     mem['MemTotal'] - available)
        for process in sample['processes'].values():
            if '(q4t)' not in process.get('stat', ''):
                continue
            mem = counters(process.get('status', ''))
            for key, field in [('maximum_q4t_rss', 'VmRSS'),
                               ('maximum_q4t_swap', 'VmSwap')]:
                value[key] = max(value[key], mem.get(field, 0))
        for name, reading in sample['source_values'].items():
            kind = ('clocks' if name.endswith(('cur_freq', 'cur_rate')) else
                    'temperatures' if name.endswith('.temp') else None)
            if kind is None or reading['status'] != 'OK' or not reading['raw'].strip().isdigit():
                continue
            number = int(reading['raw'])
            observed = value[kind].setdefault(name, dict(minimum=number, maximum=number))
            observed['minimum'] = min(observed['minimum'], number)
            observed['maximum'] = max(observed['maximum'], number)
    if not value['samples']:
        raise RuntimeError('no completed resource observations in new case')
    windows.append(entry)
    write(folder / 'protection-windows.json', dict(windows=windows,
        prior_partial_sha256=sha(base / 'protection-windows-partial.json'),
        source_sha256=sha(Path(__file__)),
        note='48 complete performance case windows. Physical memory proxy is '
             'MemTotal minus MemAvailable, never added to RSS. Plain parent 200K '
             'is a separate cold start. Observations are not causal attribution.'))
    print('Recorded 48 complete performance windows; 47 reused without modification')


def create(folder):
    plan = read(folder / 'plan.json')
    paths = set()
    for name, digest in plan['files'].items():
        if sha(name) != digest:
            raise RuntimeError('frozen dependency changed: ' + name)
        paths.add(Path(name))
    paths.update(p for p in folder.rglob('*') if p.is_file())
    paths.add(Path(__file__).resolve())
    base = Path(plan['prior_plan']).parents[3]
    for name in ('archive-receipt.json', 'archive-index.json', 'baseline.json',
                 'protected-worktree.json'):
        paths.add(base / name)
    index, objects = {}, {}
    for path in sorted(paths):
        digest = sha(path)
        index[str(path)] = dict(sha256=digest, bytes=path.stat().st_size,
                               member='objects/' + digest)
        objects.setdefault(digest, path)
    index_path = folder / 'archive-index.json'
    write(index_path, index)
    archive_path = folder / 'completion-evidence.tar.gz'
    with tarfile.open(archive_path, 'x:gz', compresslevel=3) as archive:
        archive.add(index_path, arcname='archive-index.json', recursive=False)
        for digest, path in objects.items():
            archive.add(path, arcname='objects/' + digest, recursive=False)
    print(json.dumps(dict(paths=len(index), objects=len(objects),
                         bytes=archive_path.stat().st_size)))


def verify(folder):
    index_path = folder / 'archive-index.json'
    archive_path = folder / 'completion-evidence.tar.gz'
    index = read(index_path)
    expected = {r['member']: r for r in index.values()}
    seen = set()
    with tarfile.open(archive_path, 'r:gz') as archive:
        for member in archive:
            if not member.isfile() or member.name in seen:
                raise RuntimeError('invalid archive member')
            seen.add(member.name)
            digest = hashlib.file_digest(archive.extractfile(member), 'sha256').hexdigest()
            if member.name == 'archive-index.json':
                if digest != sha(index_path):
                    raise RuntimeError('index mismatch')
            elif (member.name not in expected or
                  expected[member.name]['sha256'] != digest or
                  expected[member.name]['bytes'] != member.size):
                raise RuntimeError('archived file mismatch')
    if seen != set(expected) | {'archive-index.json'}:
        raise RuntimeError('archive missing objects')
    for name, item in index.items():
        if sha(name) != item['sha256']:
            raise RuntimeError('source changed after archiving: ' + name)
    result = dict(passed=True, archive=str(archive_path),
        archive_sha256=sha(archive_path), archive_bytes=archive_path.stat().st_size,
        index_sha256=sha(index_path), paths=len(index), objects=len(expected),
        scope='Completion raw evidence, frozen dependencies, additive review and '
              'decision. Prior archive remains separately preserved; model weights '
              'are identified by metadata, not copied.')
    write(folder / 'archive-receipt.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('resources', 'create', 'verify'))
    parser.add_argument('--folder', required=True, type=Path)
    args = parser.parse_args()
    globals()[args.action](args.folder.resolve())
