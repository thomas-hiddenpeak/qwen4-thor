"""Verify sealed model arms, exact cache vectors, and actual lifetime ledgers."""
import hashlib
import json
from pathlib import Path
import subprocess
from cache_start import check_vector
from trace_audit import audit

ROOT=Path(__file__).resolve().parents[3]
BASE=ROOT/'.q4t-work/vmm-compare-20261020'
NAMES=['capture-v1','hot-runtime-v1','hot-vmm-v1','hot-vmm-v2',
       'bounded-runtime-v1','bounded-vmm-v1']


def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    results={};ledgers={}
    for name in NAMES:
        stage=BASE/name
        sealed=read(stage/'sealed-files.json')
        for rel,value in sealed.items():assert sha(stage/rel)==value,rel
        run=stage/'run'
        for rel,value in read(run/'sources.json').items():
            assert sha(run/'source'/rel)==value,rel
        guard=read(stage/'lease/exit.json')
        queries=[json.loads(s) for s in (stage/'lease/nvml-queries.jsonl').read_text().splitlines()]
        row=dict(guard=guard['status'],files=len(sealed),query_count=len(queries),max_query_seconds=max(q['seconds'] for q in queries),
                 query_statuses=dict((status,sum(q['status']==status for q in queries))
                                     for status in sorted(set(q['status'] for q in queries))))
        server=run/'normal/http/server.log'
        if server.exists():
            ledger=audit(server);assert not ledger['errors'],name
            ledgers[name]=ledger
            (BASE/f'{name}-ledger.json').write_text(json.dumps(ledger,indent=2)+'\n')
            for key in ('records','allocations','frees','async_free_submissions',
                        'peak_logical_bytes','live_logical_bytes','ready_snapshot',
                        'incomplete_calls','failed_calls','remaining'):
                row[key]=ledger[key]
            assert read(run/'plan.json')['binary']==(run/'normal/http/binary.sha256').read_text().strip()
            check_vector(read(run/'cache-launch.json'),read(run/'cache-expected.json'))
            row['payload_at_launch']=sum(read(run/'cache-expected.json').values())
            row['startup_allocations']=sum(r[0]=='alloc' and r[-1] for r in ledger['normalized'])
            row['startup_frees']=sum(r[0]=='free' and r[-1] for r in ledger['normalized'])
            if guard['status']=='COMPLETED':
                wrapper=read(run/'normal/wrapper-exit.json')
                assert wrapper['runner_rc']==wrapper['monitor_rc']==0
                assert not wrapper['cleanup_failed'] and wrapper['failure'] is None
                assert wrapper['unit_after_cleanup']['LoadState']=='not-found'
                exit_info=read(run/'normal/http/exit.json')
                assert exit_info['http_output_checks_passed'] and exit_info['server']==0
                quality=read(run/'normal/http/results.json');assert len(quality)==1
                assert quality[0]['exact_match'] and quality[0]['actual_input']==1024
                assert quality[0]['text']=='710003' and quality[0]['actual_output']==7
                row['quality']={k:quality[0][k] for k in ('text','actual_input','actual_output','prompt_sha256','exact_match')}
                assert not ledger['incomplete_calls'] and not ledger['failed_calls']
                assert len(ledger['remaining'])==2
                assert all(r['bytes']==993280 and r['backend']=='runtime' and r['kind']=='device' for r in ledger['remaining'])
                plan=read(run/'plan.json');binary=BASE/('vmm' if plan['arm']=='vmm' else 'runtime')/'q4t'
                symbols=[subprocess.check_output(['addr2line','-Cf','-e',str(binary),hex(r['caller'])],text=True) for r in ledger['remaining']]
                assert all('ArgmaxBf16Rows' in s for s in symbols)
                row['remaining_reason']='two process-lifetime ArgmaxBf16Rows static buffers; no explicit free'
                row['vmm_allocations']=sum(r['backend']=='vmm' and r['phase']=='end' and r['api']=='cudaMalloc' and r['rc']==0
                    for r in (json.loads(line.split('[memory-trace] ',1)[1]) for line in server.read_text().splitlines() if '[memory-trace] ' in line))
        else:
            assert name in ('hot-vmm-v1','hot-vmm-v2')
            assert guard['status']=='COMMAND_FAILED'
            assert 'cache vector differs' in read(run/'state.json')['error']
            row['model_started']=False
        if row['guard']!='COMPLETED' and 'remaining' in row:
            row['remaining_count']=len(row.pop('remaining'))
        results[name]=row
    assert results['capture-v1']['guard']=='COMPLETED'
    assert results['hot-runtime-v1']['guard']=='ENV_ABORT'
    for name in ('bounded-runtime-v1','bounded-vmm-v1'):
        assert results[name]['guard']=='COMPLETED'
    a=BASE/'bounded-runtime-v1/run';b=BASE/'bounded-vmm-v1/run'
    assert read(a/'cache-expected.json')==read(b/'cache-expected.json')
    assert results['bounded-runtime-v1']['quality']==results['bounded-vmm-v1']['quality']
    # Compare allocation/free identities at startup, independent of addresses.
    a_start=[r for r in ledgers['bounded-runtime-v1']['normalized'] if r[-1]]
    b_start=[r for r in ledgers['bounded-vmm-v1']['normalized'] if r[-1]]
    assert a_start==b_start
    assert ledgers['bounded-runtime-v1']['normalized']==ledgers['bounded-vmm-v1']['normalized']
    pair=read(BASE/'bounded-pair-v1/state.json')
    assert pair['status']=='COMPLETED_BOUNDED_COMPARISON'
    assert all(read(BASE/f'bounded-{arm}-v1/lease/exit.json')['external_lease'] for arm in ('runtime','vmm'))
    default=sha(ROOT/'build/q4t')
    assert default=='19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
    report=dict(status='BOUNDED_COMPARISON_COMPLETE_NO_FULL_WARM_FIX_CLAIM',stages=results,
        exact_bounded_cache_vector=True,startup_allocation_sequence_equal=True,
        full_logical_alloc_free_sequence_equal=True,
        default_sha256=default,total_model_loads=4,total_http_successes=3,
        full_hot_vmm_not_tested=True,physical_fragmentation_equal=False,
        production_accepted=False,performance_acceptance=False)
    (BASE/'audit.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('status','total_model_loads','total_http_successes')}))

if __name__=='__main__':main()
