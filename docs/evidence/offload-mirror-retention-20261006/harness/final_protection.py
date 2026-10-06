"""Bounded final metadata/source protection, without replaying closed evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

R = Path(__file__).resolve().parent
W = R/'source'
MAIN = R.parent.parent
OLD = R.parent/'offload-supply-20261006'


def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def git(path, *args):
    return subprocess.check_output(['git', *args], cwd=path)


def main():
    result = dict(schema=1, started_t=time.time(), passed=False, failure=None,
                  source_sha256={}, new_HTTP=0, model_runs=0)
    def bind(p):
        p=Path(p).resolve(); result['source_sha256'][str(p)]=sha(p); return read(p)
    with (R/'final-protection.json').open('x') as output:
        try:
            assert sha(R/'entry.json')=='38568d818c01ea6eadca2f356c6680eb4b6d85d3b695c7dd1691b43ed5f3d484'
            assert sha(R/'model-entry.json')=='31cfb5f2cae36d75f84dcd1b27d7a11ce5972e2b6ba9637b9eb8493cd91dd0f4'
            entry=bind(R/'entry.json'); model=bind(R/'model-entry.json')
            assert git(MAIN,'rev-parse','HEAD').decode().strip()==entry['main_head']
            assert git(MAIN,'status','--porcelain').decode().splitlines()==entry['main_status']
            assert hashlib.sha256(git(MAIN,'diff','--binary','HEAD')).hexdigest()==entry['main_diff_sha256']
            assert sha(entry['main_binary']['path'])==entry['main_binary']['sha256']
            assert git(MAIN,'status','--porcelain','--untracked-files=no','--','reference').decode().splitlines()==entry['reference_tracked_status']
            files=[]
            root=Path(model['files'][0]['path']).parent
            for p in sorted(root.rglob('*')):
                if p.is_file():
                    s=p.stat();files.append(dict(path=str(p),size=s.st_size,inode=s.st_ino,device=s.st_dev,mtime_ns=s.st_mtime_ns))
            assert files==model['files']
            assert {p:sha(p) for p in model['config_index_sha256']}==model['config_index_sha256']
            assert model['weight_payload_hashed'] is False
            result['protected_MAIN']=entry
            result['model_metadata_records']=len(files)
            for path,head in [(OLD/'source','8ea3d329a236e35dd0aba35ad619912e3e76cc28'),
                              (OLD/'observer-source',entry['base'])]:
                assert git(path,'rev-parse','HEAD').decode().strip()==head
                assert not git(path,'status','--porcelain').strip()
            plan=bind(R/'execution-plan.json'); proof=bind(R/'validation-results.json')
            summary=bind(R/'retention-summary.json'); review=bind(R/'result-independent-review.json')
            assert proof['passed'] and proof['first_batch'] and proof['total_tests']==45
            assert review['passed'] and not review.get('blocking_findings')
            for name in ['retention-summary.json','retention-inputs.json','execution-plan.json','validation-results.json']:
                assert review['source_sha256'][str(R/name)]==sha(R/name)
            assert review['source_sha256'][str(R/'retention-analysis.json')]==sha(R/'retention-analysis.json')
            assert review['source_sha256'][str(R/'decode-plans.json')]==sha(R/'decode-plans.json')
            assert summary['source_sha256'][str(R/'retention-analysis.json')]==review['source_sha256'][str(R/'retention-analysis.json')]
            assert summary['reconstructed_decode_layer_plans']==97920 and len(summary['requests'])==8
            assert summary['model_runs']==summary['HTTP_requests']==0 and not summary['runtime_changes']
            assert git(W,'rev-parse','HEAD').decode().strip()==plan['runner_source_commit']
            assert not git(W,'status','--porcelain').strip()
            allowed={'tools/trace/'+n for n in ['mirror_retention.py','test_mirror_retention.py','analyze_mirror_retention.py','test_analyze_mirror_retention.py']}
            changes=git(W,'diff','--name-only',entry['base'],'HEAD').decode().splitlines()
            assert all(n.startswith('docs/') or n in allowed for n in changes)
            manifest=bind(R/'retention-inputs.json'); reused={}
            for g in manifest['groups']:
                for q in g['requests']:
                    for name,b in q['validation']['bindings'].items():
                        s=Path(name).stat();assert [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]==b['stat']
                        reused[name]=b
            for name,h in plan['source_sha256'].items():
                if name in reused:
                    assert reused[name]['sha256']==h
                else:
                    assert sha(name)==h,name
            result['raw_trace_identity_reused']=reused
            terminals=[]
            for label in ['input-extract-01','contracts-01','retention-analysis-01']:
                d=bind(R/(label+'-exit.json'))
                assert d['returncode']==0 and d['failure'] is None and d['cleanup_complete']
                assert not any(d['group_after_cleanup'][k] for k in ['live_pids','zombie_pids','errors'])
                terminals.append(label)
            result.update(passed=True, status='FINAL_PROTECTION_PASS',
                tool_commit=plan['runner_source_commit'],decision=summary['decision'],
                closed_owned_labels=terminals,
                analysis_sha256=review['source_sha256'][str(R/'retention-analysis.json')],
                review_sha256=sha(R/'result-independent-review.json'))
        except BaseException as error:
            result.update(status='FINAL_PROTECTION_FAILED',failure=type(error).__name__+': '+str(error))
        result['source_sha256'][str(Path(__file__).resolve())]=sha(__file__)
        result['limits']=['No model payload hashing; metadata/config/index equality is not byte equality or proof against transient writes.',
            'Reference scope is tracked Git status; raw trace SHA is reused with unchanged stat, without reparse/re-hash; same-stat byte substitution is not independently excluded.',
            'Owned historical exit receipts are reused; this checker own exit and later documentation-only Git delivery still require closure; no global GPU idle claim.',
            'No new performance or physical RAM measurement; old NO_GO/default-off and physical54GB INDETERMINATE remain.']
        result['ended_t']=time.time();json.dump(result,output,ensure_ascii=False,indent=2);output.write('\n')
    print(json.dumps({k:result[k] for k in ['passed','status','failure']}))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
