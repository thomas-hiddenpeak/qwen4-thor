"""Audit completed evidence identities and the actual bounded request ledger."""
import hashlib
import json
from pathlib import Path
import re
import time

from run_stage import ROOT, save
from oracle import short_answer

BASE = ROOT / '.q4t-work/session-cache-20261027'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def require(ok, message):
    if not ok:
        raise ValueError(message)


def main():
    phases = [('phase-a',17,1),('phase-a-remaining',18,1),('phase-b',0,0),
              ('phase-c',12,1),('phase-c-fixed-cap',42,2),
              ('phase-c-subset',48,2),('phase-d',94,6)]
    archives = [BASE/name/'archive/source' for name in
                ['phase-a','phase-a-remaining','phase-c','phase-c-fixed-cap']]
    cache = {}
    def digest(path):
        path = Path(path)
        if path not in cache:
            cache[path] = sha(path)
        return cache[path]
    ledger = []
    bindings = {}
    for name, expected_requests, expected_starts in phases:
        phase = BASE/name
        plan = read(phase/'plan.json')
        root = Path(plan['output'])
        state,post,lease = (read(root/'state.json'),read(phase/'postflight.json'),
                            read(phase/'lease/exit.json'))
        require(state['status']!='RUNNING','stage still running')
        require(lease['status'] in ['COMPLETED','COMMAND_FAILED'], 'GPU environment abort')
        require(not any(post[k] for k in
            ['monitor_failures','cleanup_errors','final_monitor_errors']), 'monitor/cleanup failure')
        summary = read(root/'telemetry-summary.json')
        require(summary['sampler_failure'] is None and summary['unflushed_attempts']==0
                and summary['sample_attempts']==summary['flushed_samples']
                and summary['exit_pending']['active']==0,'unfinished telemetry')
        ended = []
        admitted = 0
        starts = list(root.glob('*/server-command.json'))
        for command in starts:
            raw = (command.parent/'server.log').read_text()
            get = lambda marker: [dict(re.findall(r'(\w+)=(\S+)',line))
                for line in raw.splitlines() if line.startswith('[q4t][request] ')
                and marker in line]
            admissions, ends = get('event=admitted '),get('event=end ')
            require({r['id'] for r in admissions}=={r['id'] for r in ends}
                    and len(admissions)==len(ends), 'request release coverage')
            require(all(r['released']=='1' and r['outcome'] in ['success','cancelled']
                        for r in ends),'request failed/unreleased')
            require(read(command.parent/'exit.json')['server']==0,'unclean server exit')
            admitted += len(admissions);ended += ends
        require(admitted==expected_requests and len(starts)==expected_starts,
                'actual phase ledger mismatch: '+name)
        resolved = {}
        for path, expected in plan['files'].items():
            source = Path(path)
            if source.is_file() and digest(source)==expected:
                resolved[path] = str(source)
                continue
            relative = source.relative_to(ROOT)
            matches = [a/relative for a in archives
                       if (a/relative).is_file() and digest(a/relative)==expected]
            require(bool(matches),'unresolved frozen input: '+path)
            resolved[path] = str(matches[0])
        bindings[name] = resolved
        ledger.append(dict(phase=name,requests=admitted,starts=len(starts),
            cancelled=sum(r['outcome']=='cancelled' for r in ended),
            original_state=state['status'],original_error=state.get('error'),
            monitor_samples=summary['flushed_samples'],
            explicit_final_monitor_barrier=bool(post['final_monitor_barrier']),
            gpu_guard=lease['status'],elapsed_seconds=state.get('elapsed_seconds'),
            plan_sha256=digest(phase/'plan.json'),bindings=len(resolved)))
    for name, head in [('phase-a','full'),('phase-a-remaining','subset')]:
        plan = read(BASE/name/'plan.json')
        directory = Path(plan['output'])/(head+'-http')
        rows = read(directory/'quality/responses.json')
        require(len(rows)==11,'quality count')
        for row in rows:
            oracle = plan['quality'][row['prompt_sha256']]
            require(row['success'] and row['text'].strip()==oracle['expected']
                    and row['input_tokens']==oracle['length'] and
                    row['finish']==['stop'],'actual HTTP quality mismatch')
        hit,fresh,miss = [read(directory/(case+'-response.json')) for case in
                         ['session-resume','fresh-reference','prefix-mismatch']]
        require(hit['text'].strip()==fresh['text'].strip()==miss['text'].strip()=='43',
                'lifecycle quality mismatch')
        require(hit['usage']['prompt_tokens_details']['cached_tokens']>=8192 and
                miss['usage']['prompt_tokens_details']['cached_tokens']==0,
                'lifecycle prefix match/miss')
        if head=='subset':
            require(read(directory/'after-cancel-response.json')['text'].strip()==short_answer(plan),
                    'subset post-cancel quality')
    for head in ['full','subset']:
        raw = (BASE/'phase-b/run'/(head+'-state-driver.log')).read_text()
        require('PASS full_state=1 reused_source_slot=0 restored_slot=2' in raw and
                'continuation=17 terminal_extend=1' in raw,'state reference incomplete')
    require(read(BASE/'recovery-adjudication.json')['passed'],'full post-cancel adjudication')
    performance = read(BASE/'performance-gate.json')
    protection = read(BASE/'protection-comparisons.json')
    require(performance['complete'],'transaction measurement incomplete')
    require(protection['complete'] and protection['all_outputs_identical'] and
            protection['cross_turn_recall_passed'],'protection/quality incomplete')
    requests = sum(r['requests'] for r in ledger)
    starts = sum(r['starts'] for r in ledger)
    require((requests,starts)==(231,13),'pre-health total budget')
    first = read(BASE/'phase-a/plan.json')
    deadline = first['task_deadline_unix']
    result = dict(passed=True, candidate_sha256=first['candidate_sha256'],
        installed_sha256=digest(ROOT/'build/q4t'),
        baseline_unchanged=digest(ROOT/'build/q4t')==first['parent_sha256'],
        cumulative_requests=requests,cumulative_starts=starts,gpu_fixtures=2,
        cancellations=sum(r['cancelled'] for r in ledger),
        resource_caps=dict(http=240,starts=14,fixtures=4,seconds=21600),
        reserved_health=dict(requests=2,starts=1),deadline_unix=deadline,
        wall_elapsed_seconds=time.time()-(deadline-21600),
        remaining_seconds=deadline-time.time(),env_abort=False,phases=ledger,
        binding_sources=bindings,
        gain_target_met=performance['passed'],
        protection_negative_observations=protection['negative_observations'],
        statistical_noninferiority='NOT_ESTABLISHED',
        engineering_acceptance='SEPARATE_DECISION_REQUIRED',
        raw_failures_retained=['A release-marker assertion','A subset eager-cache audit',
            'Original C natural EOS/equal-work assumption','Full C wrong prompt oracle'],
        limits=['Original stopped phases retain their missing final monitor barrier.',
                'The CPU full ON adjudication adds a derived audit file; original raw '
                'file hashes and failed exit/state are unchanged.',
                'Four-layer direct state reference does not prove arbitrary fresh-prefill '
                'bitwise output equivalence. Actual HTTP uses all 48 target layers.'])
    save(BASE/'final-audit.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='binding_sources'},indent=2))


if __name__=='__main__':
    main()
