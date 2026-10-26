"""Offline audit of the delivered single-slot parent, which predates batch logs.

This preserves the failed original audit. It does not resume a GPU stage, create
missing batch observations, or qualify the candidate by the legacy exception.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[2]
PARENT_SHA = '90f24f49ea93e7ddbb359e817f05a9d39f87a54f66433c01a8d46f198f489bd4'
spec = importlib.util.spec_from_file_location(
    'agent_parent_stage', ROOT / 'tools/mtp_suffix_agent/run_stage.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def audit(output, expected_requests):
    step = json.loads((output / 'step.json').read_text())
    argv = json.loads((output / 'server-command.json').read_text())['argv']
    if (step['layout'] != 'single' or step['head'] not in ['full', 'subset']
            or stage.legacy.sha(Path(step['binary'])) != PARENT_SHA
            or argv[0] != step['binary'] or '--seq-capacities' in argv
            or argv[argv.index('--max-seq') + 1] != '1'):
        raise RuntimeError('legacy exception requires the exact single-slot parent')
    raw = (output / 'server.log').read_text()
    if '[mtp-batches]' in raw:
        raise RuntimeError('unexpected emitted batch ledger in legacy parent')
    stage.lazy.audit_server(output, expected_requests)
    fields = lambda line: dict(re.findall(r'(\w+)=(\S+)', line))
    hist = [fields(line) for line in raw.splitlines()
            if line.startswith('[mtp-accept] ')]
    ended = [fields(line) for line in raw.splitlines() if 'event=end ' in line]
    if (len(hist) != expected_requests
            or {r['request'] for r in hist} != {r['id'] for r in ended}
            or any(r['seq'] != '0' or r['outcome'] != 'success' for r in ended)
            or any(len(r['draft_counts'].split(',')) != 4 for r in hist)):
        raise RuntimeError('legacy acceptance/lifecycle coverage')
    result = dict(passed=True, parent_sha256=PARENT_SHA,
                  requests=expected_requests, acceptance=hist,
                  lifecycle=ended, batches=[], streamed_prefill=[],
                  suffix=json.loads((output / 'server-audit.json').read_text())['suffix'],
                  cache=[fields(line) for line in raw.splitlines()
                         if line.startswith('[suffix-classes] ')],
                  emitted_batch_ledger='UNAVAILABLE_IN_DELIVERED_PARENT',
                  independent_batch_work_audit=False,
                  scope='HTTP/output/lifecycle/cache/startup ownership and request acceptance; no invented batch observations')
    stage.save(output / 'parent-compat-audit.json', result)
    return result


def copy_evidence(source, output):
    output.mkdir(parents=True, exist_ok=False)
    for name in ['step.json', 'server-command.json', 'server.log',
                 'exit.json', 'results.json']:
        shutil.copy2(source / name, output / name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--requests', type=int, required=True)
    args = parser.parse_args()
    copy_evidence(args.source, args.output)
    print(json.dumps(audit(args.output, args.requests)))
