"""Freeze the only continuation; no model, HTTP or test execution."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/suffix-admission-20261024'
OLD = BASE / 'acceptance-v1'
NEW = BASE / 'acceptance-v2'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def main():
    original = read(OLD / 'frozen-plan.json')
    prior = read(OLD / 'run/state.json')
    audit = read(BASE / 'partial-audit-v1.json')
    if (prior['status'] != 'STOPPED' or prior['completed'] !=
            ['quality-full-on', 'host-contracts'] or
            prior['requests_completed'] != 11 or prior['loads_started'] != 1 or
            audit['evidence_validity'] != 'VALID' or audit['measurement_complete']):
        raise RuntimeError('only the inspected v1 partial result is reusable')
    changed = {p:dict(before=d, after=sha(p)) for p,d in original['files'].items()
               if sha(p) != d}
    fixture = str(ROOT / 'tools/mtp_suffix_admission/test_stage_audit.py')
    if set(changed) != {fixture}:
        raise RuntimeError('only the synthetic log fixture may change: ' + repr(changed))
    frozen_fixture = OLD / 'source-snapshot/tools/mtp_suffix_admission/test_stage_audit.py'
    if sha(frozen_fixture) != original['files'][fixture]:
        raise RuntimeError('original fixture snapshot identity changed')
    expected = frozen_fixture.read_text().replace('event=admitted\\n',
                                                  'event=admitted stage=request\\n')
    if Path(fixture).read_text() != expected:
        raise RuntimeError('fixture fix differs from the reviewed one-line change')
    if sha(original['candidate']) != original['candidate_sha256']:
        raise RuntimeError('runtime candidate changed')
    NEW.mkdir(exist_ok=False)
    plan = copy.deepcopy(original)
    plan.update(source_commit=subprocess.check_output(
        ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        candidate_source_commit=original['source_commit'],
        source_note='same cb0fa73 runtime/build; only synthetic stage-test fixture fixed; v1 first HTTP and host reused explicitly',
        output=str(NEW/'run'), lease=str(NEW/'lease'), source_snapshot=str(NEW/'source-snapshot'))
    bindings = [OLD/'frozen-plan.json',OLD/'run/state.json',OLD/'run/files.json',
                OLD/'lease/exit.json',OLD/'lease/events.jsonl',
                OLD/'lease/nvml-queries.jsonl',
                BASE/'partial-audit-v1.json',BASE/'partial-audit-v1-summary.json',
                OLD/'run/quality-full-on/results.json',
                OLD/'run/quality-full-on/admission-audit.json',
                OLD/'run/host-contracts-driver.log',
                OLD/'run/admission-stage-contracts-driver.log']
    plan['resume'] = dict(original_stage=str(OLD),original_started=prior['started'],
        partial_audit=str(BASE/'partial-audit-v1.json'), used_requests=11,used_loads=1,
        remaining_requests=239,remaining_loads=17,
        absolute_deadline_epoch=prior['started']+14400,
        contract_charged_seconds=prior['contract_seconds']+60,
        contract_charge_note='successful host actual time plus the entire 60s failed-contract allowance; conservative shared1200s debit',
        bindings={str(p):sha(p) for p in bindings}, changed_files=changed,
        sole_retry='one CPU stage-audit retry after a one-line synthetic fixture fix; no HTTP or host replay',
        lease_boundary='v1 STOPPED and released; v2 single lease covers all remaining work; no continuous two-attempt lease claim')
    for index, step in enumerate(plan['steps']):
        if index < 2:
            step.update(inherited=True,evidence_output=str(OLD/'run'/step['name']),
                        evidence_driver_log=str(OLD/'run'/(step['name']+'-driver.log')),
                        inherited_plan_sha256=sha(OLD/'frozen-plan.json'))
        else:
            step['command'] = [value.replace(str(OLD/'run'),str(NEW/'run')).replace(
                str(OLD/'frozen-plan.json'),str(NEW/'frozen-plan.json'))
                for value in step['command']]
    plan['files'] = {p:sha(p) for p in original['files']}
    for path in bindings + [Path(__file__),Path(__file__).with_name('resume_stage.py'),
            ROOT/'docs/MTP_SUFFIX_ADMISSION_CONTINUATION_2026-10-24.md']:
        plan['files'][str(path.resolve())]=sha(path)
    snapshot = NEW/'source-snapshot'
    snapshot.mkdir()
    for name,digest in plan['files'].items():
        path=Path(name)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target=snapshot/path.relative_to(ROOT)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)
            if sha(target)!=digest:raise RuntimeError('snapshot changed during copy')
    (snapshot/'worktree.patch').write_bytes(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT))
    destination=NEW/'frozen-plan.json'
    destination.write_text(json.dumps(plan,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(plan=str(destination),sha256=sha(destination),
        candidate_sha256=plan['candidate_sha256'],runtime_source=plan['candidate_source_commit'],
        remaining_requests=239,remaining_loads=17,deadline=plan['resume']['absolute_deadline_epoch'],
        changed_files=changed),indent=2))


if __name__ == '__main__':
    main()
