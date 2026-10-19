"""Independently validate capture provenance and recompute all comparisons."""
import json
import struct

from report import Record, compare, require


def validate_bundle(directory, identity):
    records = {p.parent.name: Record(p) for p in directory.glob('*/record.json')}
    expected = {'prefill-t1', 'prefill-t4', 't4', 't4-replay'}
    for row in range(4):
        tag = f't1-row{row}'
        expected.update(tag + suffix for suffix in
                        ['', '-replay', '-batch-ids', '-same-input-normal',
                         '-same-input-batch-ids'])
    require(set(records) == expected, 'missing or unexpected capture records')
    raw = (directory / 'tokens.i32').read_bytes()
    require(len(raw) % 4 == 0 and 20 <= len(raw) <= 1040, 'token file size')
    tokens = list(struct.unpack('<' + 'i' * (len(raw) // 4), raw))
    prompt = len(tokens) - 4
    done = json.loads((directory / 'completion.json').read_text())
    require(done == dict(status='PASS', prompt_tokens=prompt, forced_tokens=4,
                         same_shape_cases=5, model_invocations=7),
            'incomplete invocation ledger')
    for name, record in records.items():
        m = record.meta
        position = 0 if name.startswith('prefill') else (
            prompt if name.startswith('t4') else prompt + int(name[6]))
        rows = prompt if name.startswith('prefill') else (4 if name.startswith('t4') else 1)
        require(m['position'] == position and m['rows'] == rows and m['layer'] == 2,
                'incorrect boundary position/shape/layer')
        require(m['tokens'] == tokens[:position + rows], 'boundary token mapping')
        require(m['identity'] == identity, 'boundary execution identity')
    same_shape = [('prefill-t1', 'prefill-t4'), ('t4', 't4-replay')]
    same_shape += [(f't1-row{i}', f't1-row{i}-replay') for i in range(4)]
    for left, right in same_shape:
        require(records[left].bits == records[right].bits, f'bitexact mismatch: {left}')
    reports = {}
    for index in range(4):
        tag = f't1-row{index}'
        for suffix, parent, parent_row in [('-batch-ids', tag, 0),
                                          ('-same-input-batch-ids', 't4', index)]:
            target = records[tag + suffix]
            link = json.loads((target.path.parent / 'intervention.json').read_text())
            require(link == dict(kind='fixed_ids_from_explicit_row',
                    source_record_sha256=records['t4'].manifest_sha256,
                    source_row=index, target_record_sha256=target.manifest_sha256,
                    target_original_record_sha256=records[parent].manifest_sha256,
                    target_input_row=parent_row), 'intervention provenance')
            require(target.row('ids', 0, True) == records['t4'].row('ids', index, True),
                    'intervention did not use source IDs')
            require(target.row('hidden', 0, True) == records[parent].row('hidden', parent_row, True),
                    'intervention input mismatch')
        require(records[tag + '-same-input-normal'].row('hidden', 0, True) ==
                records['t4'].row('hidden', index, True), 'normal slice input mismatch')
        for label, left, right in [
                ('end_to_end', tag, 't4'),
                ('selection_intervention', tag, tag + '-batch-ids'),
                ('same_input_shape_normal', tag + '-same-input-normal', 't4'),
                ('same_input_shape_fixed_ids', tag + '-same-input-batch-ids', 't4')]:
            reports[f'{label}-row{index}'] = compare(records[left], records[right])
    return reports


def markdown(reports):
    lines = ['# Real-text MoE observations', '',
             '| Comparison | Position | Hidden changed | ID slots changed | Logits max abs | Common weights max abs | Output max abs |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for label, report in reports.items():
        for row in report['rows']:
            lines.append(f"| {label} | {row['position']} | "
                         f"{row['input_hidden']['bitwise_differences']} | "
                         f"{row['selection']['ordered_slot_differences']} | "
                         f"{row['all_logits']['max_abs']:.9g} | "
                         f"{row['aligned_common_experts']['weights']['max_abs']:.9g} | "
                         f"{row['moe_output']['max_abs']:.9g} |")
    lines += ['', 'One fixed text, layer 2, four forced tokens. No full-model quality or performance claim.',
              'Different boundary hidden includes upstream effects. Fixed IDs retain current logits and weights;',
              'remaining output differences cannot be assigned solely to routed expert arithmetic.', '']
    return '\n'.join(lines)
