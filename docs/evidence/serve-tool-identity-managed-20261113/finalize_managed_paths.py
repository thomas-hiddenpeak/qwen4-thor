"""Review equivalent argv paths without rewriting raw GPU evidence."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise ValueError(message)


p = SimpleNamespace(read=lambda path: json.loads(Path(path).read_bytes()),
    sha=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
    require=require)

WORK = Path(__file__).resolve().parent
DRIVER = WORK/'tool_managed_acceptance.py'
stage = runpy.run_path(str(DRIVER))
original = stage['accepted_run']


def normalized_guard(plan, guard):
    actual = guard['command']
    p.require(len(actual) == 6 and actual[1] == '-B' and
        actual[3:5] == ['run', '--plan'], 'unexpected actual command')
    executable = shutil.which(actual[0])
    p.require(executable is not None, 'interpreter not resolvable')
    actual_file = (ROOT/actual[2]).resolve(strict=True)
    actual_plan = (ROOT/actual[5]).resolve(strict=True)
    expected = plan['command']
    p.require(Path(executable).samefile(expected[0]) and
        actual_file == DRIVER and str(actual_file) == expected[2] and
        str(actual_plan) == expected[5] and p.read(actual_plan) == plan,
        'command identities differ')
    p.require(p.sha(actual_file) == plan['sources'][str(DRIVER.relative_to(ROOT))],
        'executed frozen driver changed')
    return dict(guard, command=expected)


def accepted_run(plan, result, guard, queries, events):
    normalized = normalized_guard(plan, guard)
    evidence = original(plan, result, normalized, queries, events)
    evidence['command_path_review'] = dict(raw=guard['command'],
        canonical=normalized['command'], cwd=str(ROOT), original_evidence_unchanged=True,
        interpreter_sha256=p.sha(plan['command'][0]),
        review_script_sha256=p.sha(Path(__file__)))
    return evidence


if __name__ == '__main__':
    path = Path(sys.argv[1]).resolve(strict=True)
    # The original recorder explicitly runs the guard with cwd=ROOT. Its
    # immutable receipt binds this exact guard argv and confirms unchanged
    # default identities. No model request, observation or stop is rewritten.
    receipt = p.read(WORK/'managed-retained-business-01.json')
    p.require(receipt['exit_code'] == 0 and receipt['default_unchanged'] and
        receipt['argv'][-6:] == p.read(path.parent/'lease/exit.json')['command'],
        'recorded guard launch differs')
    code = (WORK/'run.py').read_text()
    p.require('cwd=ROOT' in code and 'ROOT = Path(__file__).resolve().parents[2]' in code,
        'recorder working directory differs')
    stage['finalize'].__globals__['accepted_run'] = accepted_run
    stage['finalize'](path)
