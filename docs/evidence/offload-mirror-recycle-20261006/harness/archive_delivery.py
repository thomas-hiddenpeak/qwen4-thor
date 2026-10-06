"""Archive bounded completed-stage metadata; never read raw inference evidence.

Prepared during execution. Invoke only after root has stopped all owned work.
This is evidence packaging, not acceptance, rerunning tests or proof recursion.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time

R = Path(__file__).resolve().parent
W = R / 'source'
EVIDENCE = W / '.q4t-work/evidence'
DEST = W / 'docs/evidence/offload-mirror-recycle-20261006'
MAX_INPUT_BYTES = 32 << 20
MAX_INPUT_TOTAL = 96 << 20
MAX_ARCHIVE_BYTES = 16 << 20
RAW_NAMES = {'resource-samples.jsonl', 'memory.csv', 'endpoints.jsonl',
             'server.log', 'responses.json', 'requests.jsonl'}
COMPACT_NAMES = (
    'entry.json', 'model-entry.json', 'scope-plan.json', 'source-design.json',
    'source-implementation.json', 'candidate-source-identity.json',
    'build-identity.json', 'dependency-admission.json', 'execution-plan.json',
    'protocol-draft.json', 'protocol-design.md', 'required-numerical-tests.txt',
    'numerical-design.json', 'resource-dependencies.json',
    'final-protection-preparation.json', 'independent-review-plan.json',
    'comparison-handoff.json', 'audit-static-handoff.json',
    'delivery-tools-static-findings.json', 'delivery-tools-static-review.json',
    'protocol-static-handoff.json', 'history-decision.json',
    'performance-decision.json', 'host-decision.json', 'numerical-decision.json',
    'independent-result-review.json', 'final-protection.json',
    'storage-dedup-plan.json', 'storage-dedup-checklist.md',
    'storage-compression-policy.json', 'storage-compression-plan.json',
    'storage-compression-checklist.md', 'storage-reserve-repair-policy.json',
    'storage-reserve-repair-plan.json', 'storage-reserve-repair-checklist.md')
SCRIPTS = ('build_candidate.py', 'prepare_execution.py', 'recycle_common.py',
           'run_stage.py', 'audit_stage.py', 'run_direct_contracts.py',
           'compare_results.py', 'resource_audit.py',
           'independent_result_review.py', 'final_protection.py',
           'storage_dedup.py', 'storage_compress.py',
           'storage_reserve_repair.py', 'archive_delivery.py')
STORAGE_FILES = (
    'storage-dedup/operation.json', 'storage-dedup/preflight.json',
    'storage-dedup/journal.jsonl', 'storage-dedup/summary.json',
    'storage-dedup/failure.json', 'storage-compression/preflight.json',
    'storage-compression/compression.json',
    'storage-compression/compression.json.pending',
    'storage-compression/journal.jsonl', 'storage-compression/summary.json',
    'storage-reserve-repair/preflight.json',
    'storage-reserve-repair/compression.json',
    'storage-reserve-repair/compression.json.pending',
    'storage-reserve-repair/journal.jsonl', 'storage-reserve-repair/summary.json')
ARCHIVE_EXTENSIONS = ('archive-storage-extension.json',
                      'archive-reserve-extension.json')
COUNTERS = ('plans', 'attempts', 'preferred', 'changed', 'fallback',
            'unavailable', 'published')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) +
            '\n').encode()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def compact_private(value, field=''):
    """Redact payload-like JSON fields, never follow paths or source ledgers."""
    if field in ('text', 'prompt', 'body', 'content', 'messages',
                 'responses', 'response_text', 'prompt_text'):
        raw = encode(value)
        return {'redacted': True, 'canonical_json_sha256': digest(raw),
                'canonical_json_bytes': len(raw)}
    if isinstance(value, dict):
        return {key: compact_private(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_private(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--mode', choices=('complete', 'partial'), required=True)
    parser.add_argument('--reason', default='', help='Required for partial delivery')
    parser.add_argument('--output-report', type=Path,
                        default=R / 'archive-delivery.json')
    args = parser.parse_args()
    output = args.output_report.resolve()
    require(output.parent == R and output.suffix == '.json' and not output.exists(),
            'archive report must be new JSON directly under R')
    require(args.mode != 'partial' or args.reason.strip(),
            'partial archive needs the concrete stop/failure reason')
    report = {'schema': 1, 'passed': False, 'mode': args.mode,
              'reason': args.reason, 'started_t': time.time(), 'failure': None,
              'new_tests': 0, 'model_runs': 0, 'raw_evidence_reads': 0,
              'acceptance_changed_by_archival': False}
    read_cache, source_records, pending, manifest = {}, {}, {}, []
    raw_bindings, missing = {}, []
    consumed = 0

    def load(path):
        nonlocal consumed
        original_path = Path(path)
        require(not original_path.is_symlink(), 'refuse symlink source')
        path = original_path.resolve()
        root_compact = (path.parent == R and
                        path.suffix in ('.json', '.py', '.md', '.txt'))
        storage_compact = path in {R / name for name in STORAGE_FILES}
        require((root_compact or storage_compact) and path.name not in RAW_NAMES,
                'refuse non-compact, external or raw source: ' + str(path))
        if str(path) not in read_cache:
            before = path.stat()
            require(before.st_size <= MAX_INPUT_BYTES and
                    consumed + before.st_size <= MAX_INPUT_TOTAL,
                    'metadata read bound exceeded')
            raw = path.read_bytes()
            after = path.stat()
            signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            require(signature(before) == signature(after), 'source changed while read')
            read_cache[str(path)] = raw
            source_records[str(path)] = {'sha256': digest(raw), 'bytes': len(raw),
                                         'stat': signature(before)}
            consumed += len(raw)
        return read_cache[str(path)]

    def obj(path):
        return json.loads(load(path))

    def add(source, relative, value=None):
        source = Path(source).resolve()
        raw = load(source)
        output_bytes = raw if value is None else encode(value)
        require(relative not in pending, 'duplicate archive target: ' + relative)
        pending[relative] = output_bytes
        manifest.append({'archive_path': relative, 'source_path': str(source),
            'source_sha256': digest(raw), 'source_bytes': len(raw),
            'sha256': digest(output_bytes), 'bytes': len(output_bytes),
            'copy_kind': 'exact' if output_bytes == raw else 'projection'})

    def raw_ledger(ledger, owner):
        # Only carry already-recorded digests. Do not stat/read/hash any target.
        for path, value in ledger.items():
            source = Path(path)
            if not source.is_absolute() or not source.is_relative_to(EVIDENCE):
                continue
            if source.name not in RAW_NAMES and source.suffix != '.log':
                continue
            recorded = value if isinstance(value, dict) else {'sha256': value}
            item = {key: recorded[key] for key in
                    ('sha256', 'bytes_consumed', 'complete_file', 'stat_before',
                     'stat_after', 'bytes', 'stat') if key in recorded}
            item['binding_source'] = owner
            item['not_reread_during_archival'] = True
            if path in raw_bindings:
                require(raw_bindings[path].get('sha256') == item.get('sha256'),
                        'existing raw digest ledgers disagree')
            raw_bindings[path] = item

    try:
        plan_path = R / 'execution-plan.json'
        plan = obj(plan_path)
        require(digest(load(plan_path)) == args.plan_sha256 and
                plan['service_count'] == 17 and plan['http_count'] == 131 and
                [group['id'] for group in plan['groups']] == plan['group_ids'],
                'wrong frozen execution plan')
        groups = plan['groups']
        owned_starts = list(R.glob('*-controller-start.json'))
        expected_names = {group['id'] for group in groups}
        require(all(path.name.removesuffix('-controller-start.json') in
                    expected_names for path in owned_starts),
                'unrecognized controller start; manual terminal audit needed')
        group_summary = []
        for group in groups:
            name = group['id']
            started = (R / (name + '-controller-start.json')).exists()
            stage_path = R / (name + '-stage.json')
            decision_path = R / (name + '-decision.json')
            summary = {**group, 'started': started, 'status': 'NOT_RUN',
                       'accepted_request_count': 0}
            if started:
                require(stage_path.exists(), name + ': controller has no terminal stage')
                stage = obj(stage_path)
                require(finite(stage.get('ended_t')) and
                        stage['ended_t'] <= report['started_t'] and
                        (stage.get('returncode') is not None or stage.get('failure')),
                        name + ': controller may still be running')
                summary.update(status='TERMINAL_NO_DECISION',
                    controller_returncode=stage.get('returncode'),
                    controller_failure=stage.get('failure'),
                    cleanup_complete=stage.get('cleanup_complete'))
            if decision_path.exists():
                require(started, name + ': decision without owned start')
                decision = obj(decision_path)
                summary.update(status=decision.get('status'), passed=decision.get('passed'),
                    failure=decision.get('failure'),
                    accepted_request_count=(decision.get('request_count', 0)
                                            if decision.get('passed') is True else 0))
                projected = {key: value for key, value in decision.items()
                             if key != 'requests'}
                projected['requests'] = []
                for request in decision.get('requests', []):
                    row = {key: request.get(key) for key in ('response_id',
                        'prompt_sha256', 'actual_input', 'actual_output', 'finish')}
                    if isinstance(request.get('text'), str):
                        text = request['text'].encode()
                        row.update(text_sha256=digest(text), text_utf8_bytes=len(text))
                    projected['requests'].append(row)
                projected['archive_projection'] = {
                    'source_sha256': digest(load(decision_path)),
                    'request_text_omitted': True, 'prompt_body_never_copied': True}
                add(decision_path, 'groups/' + name + '-summary.json',
                    compact_private(projected))
                raw_ledger(decision.get('source_sha256', {}), str(decision_path))
            group_summary.append(summary)
        accepted = sum(row['accepted_request_count'] for row in group_summary)
        if args.mode == 'complete':
            require(accepted == 131 and all(row.get('passed') is True
                    for row in group_summary), 'complete archive lacks full accepted coverage')
            require(all((R / name).exists() for name in
                        ('history-decision.json', 'performance-decision.json',
                         'resource-audit.json', 'independent-result-review.json',
                         'final-protection.json')),
                    'complete archive lacks final result/review/protection artifacts')

        names = set(COMPACT_NAMES) | set(ARCHIVE_EXTENSIONS)
        for pattern in ('*finding*.json', '*first-attempt*.json', '*failure*.json',
                        '*review*.json', '*admission*.json', '*-exit.json',
                        '*-start.json', '*-stage.json', '*-command.json'):
            names.update(path.name for path in R.glob(pattern))
        # Never include an earlier packaging attempt recursively.
        names = {name for name in names if not name.startswith('archive-') or
                 name in ARCHIVE_EXTENSIONS}
        for name in sorted(names):
            path = R / name
            if not path.exists():
                missing.append(name)
                continue
            if path.suffix == '.json':
                original = obj(path)
                sanitized = compact_private(original)
                add(path, name, None if original == sanitized else sanitized)
            else:
                add(path, name)
        for name in SCRIPTS:
            if (R / name).exists():
                add(R / name, 'harness/' + name)
            else:
                missing.append(name)

        # Explicit finite metadata allowlist; never glob/read gzip payloads.
        storage_summary, compressed_raw = {}, []
        for name in STORAGE_FILES:
            path = R / name
            if not path.exists():
                continue
            add(path, name)
            if name.endswith('/summary.json'):
                value = obj(path)
                storage_summary[name] = {key: value.get(key) for key in
                    ('passed', 'ended_t', 'plan_sha256', 'free_before', 'free_after',
                     'minimum_free_bytes', 'startup_space_gate_met',
                     'storage_reserve_bytes', 'storage_target_free_bytes',
                     'storage_target_met', 'prior_storage_evidence_unchanged',
                     'original_ledgers_unchanged',
                     'all_evidence_paths_and_byte_sha_preserved',
                     'all_removed_originals_roundtrip_verified',
                     'shared_replacements', 'released_duplicate_allocated_bytes',
                     'released_rebuildable_allocated_bytes',
                     'compressed_count', 'originals_removed', 'unprocessed_count',
                     'removed_original_allocated_bytes',
                     'compressed_allocated_bytes_total',
                     'net_payload_allocated_bytes_freed',
                     'original_paths_require_restoration', 'compression_map_sha256')}
            if name in ('storage-compression/compression.json',
                        'storage-reserve-repair/compression.json'):
                mapping = obj(path)
                require(len(mapping.get('files', [])) <= 260,
                        'compression mapping exceeds frozen storage scope')
                for item in mapping.get('files', []):
                    original = Path(item['original_path'])
                    zipped = Path(item['compressed_path'])
                    require(original.is_absolute() and original.is_relative_to(EVIDENCE)
                            and zipped.is_absolute() and zipped.is_relative_to(
                                R / Path(name).parent / 'payload')
                            and zipped.suffix == '.gz', 'compression locator escapes scope')
                    # Carry recorded locators/SHA only. Archive does not stat,
                    # rehash, decompress, or claim independent validation.
                    compact = {key: item.get(key) for key in
                        ('original_path', 'original_sha256', 'original_bytes',
                         'compressed_path', 'compressed_sha256', 'compressed_bytes',
                         'compressed_allocated_bytes', 'gzip_level',
                         'roundtrip_verified', 'state')}
                    compact.update(binding_source=str(path),
                                   not_reread_during_archival=True)
                    compressed_raw.append(compact)

        resource_path = R / 'resource-audit.json'
        if resource_path.exists():
            resource = obj(resource_path)
            compact = {key: resource.get(key) for key in ('schema', 'scope', 'status',
                'failure', 'started_t', 'ended_t', 'audit_complete', 'raw_resources_read',
                'plan_sha256', 'runtime_source_commit', 'runtime_binary_sha256',
                'method', 'http_request_count', 'client_envelope_count',
                'interpretation_limits', 'raw_bytes_consumed', 'metadata_bytes_consumed',
                'whole_physical_RAM_54GB', 'physical_union_peak_bytes')}
            compact['groups'] = {}
            for name, value in resource.get('groups', {}).items():
                compact['groups'][name] = {key: value.get(key) for key in
                    ('request_count', 'client_envelope_count', 'memory', 'PSI_status',
                     'cgroup_IO_agreement', 'whole_physical_RAM_54GB', 'interpretation')}
                inspection = value.get('inspection', {})
                compact['groups'][name]['inspection'] = {key: inspection.get(key) for key in
                    ('all_summary_checks_match', 'identity_contracts_passed', 'counts',
                     'phases', 'gpu_status_counts', 'PSI_status', 'summary_checks',
                     'window_statistics', 'observed_limit_values',
                     'memory_and_swap_event_maxima', 'observed_swap_peak_bytes',
                     'raw_resource_peaks_bytes', 'sampled_NVIDIA_peak_bytes',
                     'identity_findings', 'counter_discontinuities')}
                compact['groups'][name]['client_envelopes'] = value.get('client_envelopes', [])
            compact['archive_projection'] = {'source_sha256': digest(load(resource_path)),
                'terminal_HTTP_decisions_and_full_inspection_details_omitted': True}
            add(resource_path, 'resource-summary.json', compact)
            raw_ledger(resource.get('source_sha256', {}), str(resource_path))
        else:
            missing.append('resource-audit.json')
        require(not DEST.is_symlink() and not DEST.parent.is_symlink(),
                'refuse symlink archive root')
        acceptance_stages = {}
        for stage_name in ('host-decision.json', 'numerical-decision.json',
                           'history-decision.json', 'performance-decision.json',
                           'resource-audit.json', 'independent-result-review.json',
                           'final-protection.json'):
            stage_path = R / stage_name
            if stage_path.exists():
                record = obj(stage_path)
                acceptance_stages[stage_name] = {key: record.get(key) for key in
                    ('status', 'passed', 'audit_complete', 'decision', 'failure',
                     'request_count', 'accepted_http_request_count')}
            else:
                acceptance_stages[stage_name] = {'status': 'NOT_RECORDED'}
        summary = {'schema': 1, 'mode': args.mode, 'stop_reason': args.reason,
            'execution_plan_sha256': args.plan_sha256,
            'runtime_source_commit': plan['runtime_source_commit'],
            'runtime_binary_sha256': plan['runtime_binary_sha256'],
            'expected_services': 17, 'expected_HTTP': 131,
            'accepted_HTTP': accepted, 'groups': group_summary,
            'acceptance_stages': acceptance_stages,
            'attempted_HTTP_if_no_passed_decision': None,
            'attempted_count_limit': 'Planned requests are not treated as attempted or '
                'accepted HTTP. Incomplete service counts remain unknown here.',
            'missing_optional_or_not_run_artifacts': sorted(set(missing)),
            'local_raw_root': str(EVIDENCE), 'existing_raw_digest_bindings': raw_bindings,
            'storage_operations': storage_summary,
            'local_compressed_raw_root': str(R / 'storage-compression/payload'),
            'local_compressed_raw_roots': [str(R / name / 'payload') for name in
                                          ('storage-compression', 'storage-reserve-repair')],
            'lossless_compression_mappings': compressed_raw,
            'compression_interruption_limit': 'Only durable map entries carry verified gzip '
                'SHA. Interrupted outputs without a map entry are identified by the archived '
                'journal; their hashes remain unknown and are not read during packaging. '
                'Recorded map state is not revalidated against filesystem contents.',
            'unknown_raw_hashes': 'Unconsumed/unaudited raw files have no new hash here; '
                'raw remains local at its original path or recorded lossless gzip locator. '
                'Removed compressed originals require restoration from the map. '
                'No payload or raw stream was reread.',
            'excluded': ['raw logs', 'prompt/response bodies', 'raw resource streams',
                         'route traces', 'model payloads', 'build binaries',
                         'candidate source tar', 'gzip payloads', 'entry-main.diff'],
            'archive_is_not_acceptance': True, 'default_enabled': False,
            'physical_RAM54GB': 'INDETERMINATE'}
        pending['delivery-summary.json'] = encode(summary)
        pending['README.md'] = (
            '# Mirror GPU 回收阶段证据\n\n'
            '本目录保存有界元数据与带源 SHA 的摘要。归档不改变任何验收结论。\n\n'
            '- `delivery-summary.json` 列出全部 17 组的完成、失败及未运行状态。\n'
            '- `groups/` 省略请求正文，保留输入身份、输出 SHA、计数与计时。\n'
            '- `resource-summary.json` 保留资源边界与已记录的缺失；没有复算资源。\n'
            '- `archive-manifest.json` 区分逐字副本与投影，绑定本地原件 SHA。\n'
            '- 首次失败、静态发现和审查记录按原文件保留，不能称审查全部首过。\n'
            '- raw 路径及现有 SHA 见交付摘要；缺少哈希不在归档时补读。\n\n'
            '- 存储计划、独审、首失败、map/journal/summary 与 owned receipts 保留。\n'
            '- gzip 只留本地；原路径需按 durable map 还原，归档不读取压缩正文。\n\n'
            '完整本地证据：`' + str(EVIDENCE) + '`。模型 payload、raw 日志、\n'
            'prompts/responses、大型资源流及 build 不进入 Git。`harness/` 按原\n'
            'R/W 布局运行，归档路径不构成新的执行入口。本阶段默认关闭；\n'
            '性能、质量、资源与整体物理 54 GB 的结论分别记录。\n').encode()
        manifest_data = {'schema': 1, 'recorded_t': time.time(),
            'execution_plan_sha256': args.plan_sha256, 'copies': manifest,
            'generated': [{'archive_path': name, 'bytes': len(pending[name]),
                           'sha256': digest(pending[name])}
                          for name in ('delivery-summary.json', 'README.md')],
            'local_source_metadata': source_records,
            'source_ledger_paths_are_not_recursively_read': True}
        pending['archive-manifest.json'] = encode(manifest_data)
        require(sum(len(raw) for raw in pending.values()) <= MAX_ARCHIVE_BYTES,
                'compact archive exceeds 16MiB; refine projection before writing')
        # Preflight every collision and ancestor before any archive mutation.
        # A regular-looking child below a symlink directory can otherwise
        # escape the archive even when the child itself is not a symlink.
        archive_root = DEST.resolve()
        require(archive_root.is_relative_to(W.resolve() / 'docs/evidence'),
                'resolved archive root escapes workspace evidence directory')
        for relative, raw in pending.items():
            relative_path = Path(relative)
            require(not relative_path.is_absolute() and
                    '..' not in relative_path.parts,
                    'archive target must be a relative descendant')
            target = DEST / relative_path
            for ancestor in (target, *target.parents):
                require(not ancestor.is_symlink(),
                        'refuse symlink archive ancestor: ' + str(ancestor))
                if ancestor == W:
                    break
            require(target.resolve().is_relative_to(archive_root),
                    'resolved archive target escapes destination')
            if target.exists():
                require(target.is_file() and target.stat().st_size == len(raw) and
                        digest(target.read_bytes()) == digest(raw),
                        'existing archive differs: ' + relative)
        for relative, raw in pending.items():
            target = DEST / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as destination:
                destination.write(raw)
        report.update(passed=True, status='ARCHIVE_PREPARED_FOR_REVIEW',
            archive_root=str(DEST), file_count=len(pending),
            archive_bytes=sum(len(raw) for raw in pending.values()),
            accepted_HTTP=accepted,
            archive_manifest_sha256=digest(pending['archive-manifest.json']),
            source_sha256={path: value['sha256'] for path, value in source_records.items()})
    except BaseException as error:
        report.update(status='ARCHIVE_FAILED', failure=type(error).__name__ + ': ' + str(error))
    finally:
        report.update(ended_t=time.time(), metadata_bytes_read=consumed)
        with output.open('x') as destination:
            json.dump(report, destination, ensure_ascii=False, indent=2, allow_nan=False)
            destination.write('\n')
        print({key: report.get(key) for key in
               ('status', 'passed', 'file_count', 'accepted_HTTP', 'failure')})
    return int(not report['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
