"""Forensic recorded intervals with unresolved errors, never trace qualification."""
import argparse
import copy
import json
from pathlib import Path
import time

import nvlog_stream as codec
import recorded_gpu as recorded
import synchronous

require = recorded.require
UNKNOWN = 'NVLOG contains unknown actual-target error/fatal records or unbound backend failure'


def error_scope(terminal, report_path):
    """Exactly the frozen six diagnostics; no claims about unobserved events."""
    require(terminal['qualification_errors'] == [UNKNOWN], 'new terminal rejection')
    require(not terminal['explicit_loss'] and terminal['late_filter_pair_observed'],
            'loss or missing per-capture terminal pair')
    errors = terminal['forensic_error_classifications']
    require(len(errors) == 6 and all(r['resolution'] == 'UNRESOLVED' and
            r['qualification_allowed'] is False for r in errors), 'six unresolved errors required')
    kinds = [r for r in errors if r['classification'] == 'AUXILIARY_ACTIVITY_CONTEXT_DISABLE_REJECTED']
    storage = [r for r in errors if r['classification'] == 'POST_REPORT_DESTRUCTOR_BALANCED_COUNTERS']
    require(len(kinds) == 4 and sorted(r['activity_kind'] for r in kinds) == [33,34,35,36]
            and len(storage) == 2, 'diagnostic type/count differs')
    producer = terminal['producer_finish']
    require(len(producer['CUDAFinish']) == len(producer['ReportCUDADone']) == 2 and
            len(producer['NVTXFinish']) == 1, 'capture and shutdown producer counts differ')
    lower = max(producer[k][0]['line'] for k in ('CUDAFinish','ReportCUDADone'))
    upper = producer['NVTXFinish'][0]['line']
    require(all(lower < r['record']['line'] < upper for r in kinds), 'disable diagnostic ordering differs')
    reports = terminal['observed_report_save_markers']
    require(len(reports) == 1 and reports[0]['raw'].endswith(
        '[SaveReport]: Report file saved: '+str(report_path)), 'saved report identity differs')
    require(all(reports[0]['line'] < producer[k][1]['line'] < min(
        r['record']['line'] for r in storage) for k in ('CUDAFinish','ReportCUDADone')),
        'shutdown producer completion is not after report and before destructor')
    require(all(r['counters_equal'] and r['allocated_count'] == r['returned_count'] and
        r['preceding_report_save_count'] == 1 and r['record']['line'] > reports[0]['line']
        for r in storage), 'destructor count/order differs')
    return dict(status='FORENSIC_ONLY_UNRESOLVED', errors_retained=errors,
        no_loss_proven=False, qualification_allowed=False,
        report_path=str(report_path),
        scope='Restricts this descriptive comparison to observed records; does not excuse errors or establish complete collection.')


def structure_gate(audit, missing, terminal, pid):
    # Preserve the original rejection before checking the independent structure
    # conditions. The copied terminal omits only the already recorded error
    # condition for this sub-check, never for the original qualification result.
    try:
        recorded.qualify(audit, missing, terminal, pid)
    except ValueError as error:
        original = str(error)
    else:
        raise ValueError('expected unresolved original qualification was not preserved')
    structural = copy.deepcopy(terminal)
    structural['qualification_errors'] = []
    return dict(original_qualification_error=original,
        recorded_structure_only=recorded.qualify(audit, missing, structural, pid),
        original_qualification_passed=False)


def require_forensic(result):
    require(result.get('status') == 'FORENSIC_RECORDED_GPU_ONLY' and
        result.get('scope_gate_passed') is True and result.get('errors') == [] and
        result.get('qualification_passed') is False and
        result.get('full_trace_integrity_proven') is False and
        result.get('performance_acceptance') is False,
        'forensic result is incomplete or promoted to acceptance')


def analyze(output, pid, wrapper):
    started = time.monotonic()
    deadline = started + recorded.MAX_SECONDS
    output = Path(output).resolve()
    result = dict(schema=1, status='INVALID', errors=[], scope_gate_passed=False,
        qualification_passed=False, full_trace_integrity_proven=False,
        performance_acceptance=False, stage_environment_verified=False,
        limitations=recorded.LIMITS + ['Six target errors remain unresolved and original qualification remains rejected.'])
    try:
        report = synchronous.bound_report(output)
        scratch = output/'audit-scratch'
        scratch.mkdir()
        raw, source = recorded._read(output/'server.log')
        export, export_source = recorded._read(output/'export.log')
        audit = recorded.strict.audit_database(output/'trace.sqlite', pid,
            server_text=raw.decode('utf-8'), export_text=export.decode('utf-8'),
            scratch_dir=scratch, mode='model', wrapper_pid=wrapper)
        recorded.save(output/'strict-audit.json',audit)
        result['strict_audit'] = dict(path=str(output/'strict-audit.json'),
            sha256=recorded.strict.sha(output/'strict-audit.json'),trace_integrity=audit['trace_integrity'])
        receipt = codec._json(output/'nvlog-stream/receipt.json')
        require(receipt == codec._json(output/'nvlog-stream-finish.json'), 'completion receipt changed')
        terminal = recorded.terminal_evidence_lines(codec.iter_lines(
            output/'nvlog-stream/nvlog.zst',receipt=receipt),pid)
        result['terminal_evidence'] = terminal
        result['stream_receipt'] = receipt
        policy = error_scope(terminal,report)
        missing = recorded.all_missing_memory(output/'trace.sqlite',pid,audit,scratch,deadline)
        result['all_unmatched_memory'] = missing
        gates = structure_gate(audit,missing,terminal,pid)
        require(recorded.strict.sha(output/'trace.sqlite') == audit['source']['sha256'], 'SQLite changed')
        recorded._check_time(deadline)
        result.update(status='FORENSIC_RECORDED_GPU_ONLY',scope_gate_passed=True,
            error_scope=policy,structure_gate=gates,source_logs=dict(server=source,export=export_source))
    except (OSError,ValueError,KeyError,TypeError,TimeoutError) as error:
        result['errors'].append(repr(error))
    result['elapsed_seconds'] = time.monotonic()-started
    recorded.save(output/'recorded-gpu-audit.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--target-pid',type=int,required=True)
    parser.add_argument('--wrapper-pid',type=int,required=True)
    args=parser.parse_args()
    result=analyze(args.output,args.target_pid,args.wrapper_pid)
    print(json.dumps({k:result[k] for k in ('status','scope_gate_passed','qualification_passed','errors')}))
    return 0 if result['scope_gate_passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
