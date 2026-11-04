"""Offline collector correction; retain the frozen audit and all raw failures."""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import operations_report as report
from tools.project_tasks import store, engine, migration, protocol, retention

s, pkg = report.s, report.pkg
require, sha = pkg.require, report.sha


def tasks(data, plan, ends):
    """Replay the same contracts, binding the reader to the actual frozen plan."""
    read = lambda name: json.loads(data[name])
    usage, ids = {}, set()
    parent = s.BASE/'offline'
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for client, tokens in (('alpha', 512), ('beta', 64)):
            known = read('execution/'+client+'-known-ledger.json')
            final = read('execution/'+client+'-ledger.json')
            require(known['meta'] == final['meta'] and known['events'] ==
                final['events'][:len(known['events'])] and known['events'][-1]['kind'] ==
                'tool_result', 'saved history changed')
            prefix = 'execution/'+client+'-completed-task/'
            folder = Path(temp)/final['meta']['task_id']
            folder.mkdir(mode=0o700)
            for name, raw in data.items():
                if name.startswith(prefix): store.write_new(folder/name[len(prefix):], raw)
            require(sha(data[prefix+'ledger.db']) == final['database_sha256'], 'ledger identity')
            db = store.connection(folder)
            try:
                ledger = store.Ledger(folder, db, final['meta']['store_id'])
                state = engine.replay(ledger)
                public = engine.public(ledger, state)
                require(ledger.meta == final['meta'] and ledger.events() == final['events'] and
                    ledger.spec['runtime_sources'] == plan['historical']['sources'] and
                    ledger.spec['binding'] == plan['bindings'][client+'-old'] and
                    state['execution_spec']['binding'] == plan['bindings'][client+'-new'] and
                    state['execution_spec']['runtime_sources'] ==
                    {n:plan['source_files'][n] for n in engine.RUNTIME_FILES}, 'runtime/protocol binding')
                expected = json.loads(read('execution/'+client+'-completed.json')['stdout'])
                require(public == expected and public['state'] == 'COMPLETED' and
                    public['outcome'] == 'ANSWERED' and public['requests'] == 2 and
                    public['tool_attempts'] == public['saved_tool_results'] == public['migrations'] == 1 and
                    public['max_tokens'] == tokens and 'Qwen4ExpForConditionalGeneration' in public['answer'] and
                    '[docs/MODEL.md:L1-L24]' in public['answer'], 'answer/budgets')
                by = lambda kind:[row['payload'] for row in ledger.events() if row['kind'] == kind]
                change = by('migration')[0]['plan']
                require(change['format'] == migration.PROTOCOL_FORMAT and
                    change['source_release'] == plan['historical']['release'] == 'srv11h-a4986d61' and
                    change['protocol_review'] == plan['protocol_review'], 'explicit reviewed pair')
                protocol.validate(change['protocol_review'], change['source_spec']['binding'],
                                  change['target_spec']['binding'])
                require([row['protocol_source_sha256'] for row in by('preflight_result')] ==
                        [v['protocol'] for v in plan['versions']], 'old/new actual preflight')
                require(by('tool_started')[0]['name'] == 'read_project_record' and
                    by('tool_started')[0]['arguments'] ==
                    dict(path='docs/MODEL.md', start_line=1, line_count=24), 'actual tool arguments')
                returned = json.loads(by('tool_result')[0]['content'])
                require(returned['ok'] and returned['source']['sha256'] ==
                        plan['source_files']['docs/MODEL.md'], 'actual source content')
                observations = [json.loads(raw) for raw in
                    data['execution/'+client+'-old/read-observations.jsonl'].splitlines()]
                require(len(observations) == 1 and observations[0]['invocation']['name'] ==
                        'read_project_record', 'exactly one real tool execution')
                usage[client] = [row['completion']['usage'] for row in by('response')]
                require(len(usage[client]) == 2 and all(0 < row['completion_tokens'] <= tokens
                    for row in usage[client]), 'output bounds')
                ids.update(row['completion']['id'] for row in by('response'))
                require(json.loads(read('execution/'+client+'-duplicate-apply.json')['stdout'])['already_applied'] and
                    json.loads(read('execution/'+client+'-duplicate-submit.json')['stdout']) ==
                    dict(public, created=False), 'active duplicate')
                key = read('execution/'+client+'-archive-key.json')
                archived = retention.key_state(key, read('tasks/store.json'), ledger.meta['task_id'])
                require(archived['binding'] == plan['bindings'][client+'-new'] and archived['rekeys'] == 0 and
                    archived['payload'] == ('retained' if client == 'alpha' else 'purged') and
                    key['base']['contract_sha256'] == retention.business_contract(state['execution_spec']) and
                    key['base']['answer_sha256'] == store.digest(public['answer']), 'permanent key identity')
                for label in ('archive-duplicate-submit', 'archive-resume'):
                    value = json.loads(read('execution/'+client+'-'+label+'.json')['stdout'])
                    require(value['state'] == 'ARCHIVED' and value['no_execution'] and
                            value['payload_state'] == archived['payload'], 'archive no execution')
                prefix = 'tasks/retention/payloads/'+ledger.meta['task_id']+'/'
                if client == 'alpha':
                    require(all(sha(data[prefix+name]) == row['sha256'] for name,row in archived['files'].items()),
                            'retained payload bytes')
                else:
                    require(not any(name.startswith(prefix) for name in data), 'pruned payload retained')
            finally:
                db.close()
    require(ids == {row['id'] for row in ends}, 'backend response correlation')
    return usage


def public_bytes(raw, secrets):
    # A literal regex/string in a source file is not a PEM block. Actual PEM
    # headers occupy a complete line. Raw credential bytes are always denied.
    return all(secret not in raw for secret in secrets) and not re.search(
        rb'(?m)^-----BEGIN [A-Z ]*PRIVATE KEY-----\r?$', raw)


def collect():
    data = {}
    for name in ('plan.json', 'run.stdout', 'run.stderr'):
        data[name] = (s.ATTEMPT/name).read_bytes()
    for name in ('cpu-import-01.json', 'python-dependencies.json'):
        data[name] = (s.BASE/name).read_bytes()
    for folder in ('cpu', 'cpu-entry-fix', 'execution', 'postflight', 'plans'):
        root = s.BASE if folder.startswith('cpu') else s.ATTEMPT
        for path in sorted((root/folder).rglob('*')):
            if path.is_file():
                pkg.regular(path)
                data[str(path.relative_to(root))] = path.read_bytes()
    plan = s.check()
    for name in plan['prior_attempt']:
        data['original/'+name] = (s.BASE/name).read_bytes()
    for name in plan['historical']['sources']:
        data['historical-source/'+name] = (s.HISTORICAL/name).read_bytes()
    for path in sorted(s.TASKS.rglob('*')):
        if path.is_file():
            pkg.regular(path)
            data['tasks/'+str(path.relative_to(s.TASKS))] = path.read_bytes()
    for name in report.READERS:
        data['current-source/'+name] = (ROOT/name).read_bytes()
    front = pkg.read(plan['frontend'])
    secrets = [c['api_key'].encode() for c in front['clients']]
    secrets.append(pkg.read(plan['policy'])['gateway_token'].encode())
    original = data['cpu-entry-fix/source/tools/project_tasks/operations_report.py']
    pkg.require(public_bytes(original, secrets), 'source literal mistaken for key')
    pkg.require(not public_bytes(b'-----BEGIN PRIVATE KEY-----\nfixture\n', secrets),
                'PEM header accepted')
    pkg.require(not public_bytes(b'{"secret":"'+secrets[0]+b'"}', secrets),
                'credential bytes accepted')
    data['finish/collector-checks.json'] = json.dumps(dict(status='PASS_OFFLINE_COLLECTOR',
        assertions=3, original_source_sha256=report.sha(original),
        model_starts=0, http_requests=0)).encode()+b'\n'
    data['finish/archive-failure-01.json'] = (s.BASE/'archive-failure-01.json').read_bytes()
    data['finish/archive-failure-02.json'] = (s.BASE/'archive-failure-02.json').read_bytes()
    data['finish/archive-failure-02-source.py'] = (s.BASE/'archive-failure-02-source.py').read_bytes()
    data['finish/operations_finish.py'] = Path(__file__).read_bytes()
    for name, raw in data.items():
        pkg.require(public_bytes(raw, secrets), 'private credential or PEM block: '+name)
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('seal', 'verify'))
    args = parser.parse_args()
    report.prior.tasks = tasks
    if args.action == 'seal':
        report.collect = collect
        report.seal()
    result = report.verify()
    manifest = pkg.read(report.DEST/'manifest.json')
    pkg.require(pkg.sha(Path(__file__)) == manifest['records']['finish/operations_finish.py'],
                'offline collector identity changed')
    print(json.dumps(result))
