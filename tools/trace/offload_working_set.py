"""Conditional expert file working sets; never a Linux page-cache/SSD simulator.

Read only safetensors headers to freeze actual on-disk ranges. The observer
consumes the residency simulator's ordered NVMe *logical* read events. A read
may already be satisfied by Linux cache. All file-cache capacities below are
optimistic dedicated raw-expert pools, not host or whole-model RAM budgets.
"""
from array import array
from collections import Counter, OrderedDict, defaultdict
import argparse
import hashlib
import heapq
import json
from pathlib import Path
import re
import struct

EXPERT_RE = re.compile(r'^model\.language_model\.layers\.(\d+)\.mlp\.experts\.(\d+)\.')
PROJECTIONS = ('down_proj', 'gate_proj', 'up_proj')
SCALARS = tuple(f'{p}.{s}' for p in PROJECTIONS
                for s in ('input_scale', 'weight_scale_2'))
USED_SCALARS = ('gate_proj.weight_scale_2', 'gate_proj.input_scale',
                'down_proj.weight_scale_2', 'down_proj.input_scale')
DEFAULT_CAPACITIES = (8 << 30, 12 << 30, 16 << 30)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _span(tensor, files, tensors):
    shard, info = tensors[tensor]
    start, end = info['data_offsets']
    dtype_bytes = {'F64': 8, 'I64': 8, 'F32': 4, 'I32': 4, 'U32': 4,
                   'F16': 2, 'BF16': 2, 'I16': 2, 'U16': 2, 'I8': 1,
                   'U8': 1, 'BOOL': 1, 'F8_E4M3': 1, 'F8_E5M2': 1}
    elements = 1
    for dim in info['shape']:
        elements *= dim
    if end - start != elements * dtype_bytes[info['dtype']]:
        raise ValueError(f'tensor shape/range byte mismatch: {tensor}')
    offset = files[shard]['data_offset']
    if not (isinstance(start, int) and isinstance(end, int) and
            0 <= start <= end <= files[shard]['file_size'] - offset):
        raise ValueError(f'invalid tensor range: {tensor}')
    return dict(file=shard, offset=offset + start, length=end - start)


def _adjacent(spans):
    return all(a['file'] == b['file'] and
               a['offset'] + a['length'] == b['offset']
               for a, b in zip(spans, spans[1:]))


def _merge(spans, region):
    assert _adjacent(spans)
    return dict(file=spans[0]['file'], offset=spans[0]['offset'],
                length=sum(s['length'] for s in spans), region=region)


def build_layout(model_dir):
    """Read index/config and 8-byte lengths + JSON headers, never payload.

    Header-only file identity is deliberately not a payload digest. File stat
    identity plus header/index hashes binds the observed packing, not weights.
    """
    model_dir = Path(model_dir).resolve()
    index_raw = (model_dir / 'model.safetensors.index.json').read_bytes()
    index = json.loads(index_raw)['weight_map']
    config_raw = (model_dir / 'config.json').read_bytes()
    config = json.loads(config_raw)
    config = config.get('text_config', config)
    hs, moe_is = config['hidden_size'], config['moe_intermediate_size']
    w_bytes, s_bytes = hs * (moe_is // 2), hs * (moe_is // 16)
    selected = {name: shard for name, shard in index.items()
                if EXPERT_RE.match(name)}
    if not selected:
        raise ValueError('index contains no expert tensors')
    files, tensors = {}, {}
    for shard in sorted(set(selected.values())):
        path = (model_dir / shard).resolve()
        if not path.is_relative_to(model_dir):
            raise ValueError('shard escapes model directory')
        with path.open('rb') as source:
            before = path.stat()
            length_raw = source.read(8)
            if len(length_raw) != 8:
                raise ValueError('short safetensors length')
            length = struct.unpack('<Q', length_raw)[0]
            if length > 64 << 20 or length + 8 > before.st_size:
                raise ValueError('invalid or excessive safetensors header')
            raw = source.read(length)
            after = path.stat()
        if len(raw) != length or (before.st_dev, before.st_ino, before.st_size,
                                  before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('shard changed during metadata read')
        header = json.loads(raw)
        files[shard] = dict(data_offset=8 + length, file_size=before.st_size,
                           device=before.st_dev, inode=before.st_ino,
                           mtime_ns=before.st_mtime_ns,
                           header_sha256=_sha(length_raw + raw))
        for name, info in header.items():
            if selected.get(name) == shard:
                tensors[name] = shard, info
    experts = []
    keys = sorted({tuple(map(int, EXPERT_RE.match(n).groups()))
                   for n in selected})
    for layer, expert in keys:
        prefix = f'model.language_model.layers.{layer}.mlp.experts.{expert}.'
        weights = [_span(prefix + p + '.weight', files, tensors)
                   for p in PROJECTIONS]
        scales = [_span(prefix + p + '.weight_scale', files, tensors)
                  for p in PROJECTIONS]
        scalars = [_span(prefix + n, files, tensors) for n in SCALARS]
        fast = (all(s['length'] == w_bytes for s in weights) and
                all(s['length'] == s_bytes for s in scales) and
                _adjacent(weights) and _adjacent(scales))
        sc_fast = all(s['length'] == 4 for s in scalars) and _adjacent(scalars)
        # Runtime uses the down-weight shard for both fast ranges. Refuse
        # inconsistent arbitrary-shard layouts instead of inventing reads.
        if fast and len({s['file'] for s in weights + scales}) != 1:
            raise ValueError('runtime fast ranges refer to different shards')
        if fast and sc_fast and scalars[0]['file'] != weights[0]['file']:
            raise ValueError('runtime scalar fast range refers to wrong shard')
        if fast:
            spans = [_merge(weights, 'weights'), _merge(scales, 'sf')]
            if sc_fast:
                spans += [_merge(scalars, 'scalars')]
            else:
                spans += [dict(_span(prefix + n, files, tensors), region=n)
                          for n in USED_SCALARS]
        else:
            spans = [dict(s, region='weight') for s in weights]
            spans += [dict(s, region='sf') for s in scales]
            spans += [dict(_span(prefix + n, files, tensors), region=n)
                      for n in USED_SCALARS]
        experts.append(dict(layer=layer, expert=expert, fast=fast,
                            sc_fast=sc_fast, spans=spans,
                            logical_bytes=sum(s['length'] for s in spans),
                            contig_next=False))
    lookup = {(e['layer'], e['expert']): e for e in experts}
    for a in experts:
        b = lookup.get((a['layer'], a['expert'] + 1))
        a['contig_next'] = bool(b and a['fast'] and b['fast'] and
                                a['sc_fast'] and b['sc_fast'] and
                                all(_adjacent([x, y]) for x, y in
                                    zip(a['spans'], b['spans'])))
    return dict(schema_version=1, model_dir=str(model_dir),
                model_index_sha256=_sha(index_raw), config_sha256=_sha(config_raw),
                dimensions=dict(hidden_size=hs, moe_intermediate_size=moe_is),
                files=files, experts=experts,
                scope='headers only; payload contents were not read or hashed')


def validate_layout(layout, expected_layers=48, expected_experts=512, check_files=True):
    """Validate complete expert keys and frozen metadata; never read payload."""
    expected = {(layer, expert) for layer in range(expected_layers)
                for expert in range(expected_experts)}
    entries = layout['experts']
    keys = [(e['layer'], e['expert']) for e in entries]
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError('layout expert keys are incomplete or duplicated')
    for entry in entries:
        if sum(s['length'] for s in entry['spans']) != entry['logical_bytes']:
            raise ValueError('layout logical bytes mismatch')
        for span in entry['spans']:
            meta = layout['files'][span['file']]
            if not (meta['data_offset'] <= span['offset'] and span['length'] > 0
                    and span['offset'] + span['length'] <= meta['file_size']):
                raise ValueError('layout file range invalid')
    checks = dict(complete_keys=True, expert_count=len(entries), ranges_valid=True,
                  files_checked=0, payload_read=False)
    if not check_files:
        checks['source_identity'] = 'NOT_CHECKED'
        return checks
    model_dir = Path(layout['model_dir']).resolve()
    for filename, field in [('model.safetensors.index.json', 'model_index_sha256'),
                            ('config.json', 'config_sha256')]:
        if _sha((model_dir / filename).read_bytes()) != layout[field]:
            raise ValueError(f'layout source hash changed: {filename}')
    for name, meta in layout['files'].items():
        path = (model_dir / name).resolve()
        if not path.is_relative_to(model_dir):
            raise ValueError('layout shard escapes model directory')
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if identity != tuple(meta[k] for k in ('device', 'inode', 'file_size', 'mtime_ns')):
            raise ValueError(f'layout source stat changed: {name}')
        data_offset = meta['data_offset']
        if not 8 <= data_offset <= (64 << 20) + 8:
            raise ValueError('layout header extent invalid')
        with path.open('rb') as source:
            header = source.read(data_offset)
        if len(header) != data_offset or _sha(header) != meta['header_sha256']:
            raise ValueError(f'layout source header changed: {name}')
        if struct.unpack('<Q', header[:8])[0] + 8 != data_offset:
            raise ValueError('layout header length mismatch')
        checks['files_checked'] += 1
    checks['source_identity'] = 'CURRENT_INDEX_CONFIG_STAT_HEADER_MATCH'
    return checks


def contiguous_next_by_layer(layout):
    result = defaultdict(set)
    for e in layout['experts']:
        result[e['layer']]
        if e['contig_next']:
            result[e['layer']].add(e['expert'])
    return dict(result)


def _union_length(intervals):
    end, total = -1, 0
    for lo, hi in sorted(intervals):
        total += max(0, hi - max(lo, end))
        end = max(end, hi)
    return total


def interval_ledger(experts, page_bytes=4096):
    """Union actual file intervals/pages; never a claim of cache residency."""
    by_file = defaultdict(list)
    for e in experts:
        for s in e['spans']:
            by_file[s['file']].append((s['offset'], s['offset'] + s['length']))
    rows = []
    for name, spans in sorted(by_file.items()):
        pages = [(lo // page_bytes, (hi + page_bytes - 1) // page_bytes)
                 for lo, hi in spans if hi > lo]
        rows.append(dict(file=name, unique_logical_bytes=_union_length(spans),
                         unique_page_cover_bytes=_union_length(pages) * page_bytes))
    return dict(files=rows,
                unique_logical_bytes=sum(r['unique_logical_bytes'] for r in rows),
                unique_page_cover_bytes=sum(r['unique_page_cover_bytes'] for r in rows))


def _cache_counts(stream, object_bytes, capacity, groups=None):
    """Equal-size fully associative cold cache; mandatory admission.

    Belady minimizes misses only for this fixed object stream/model. It does
    not model shared pages, readahead, async order, admission bypass or Linux.
    """
    slots = capacity // object_bytes
    lru_groups, optimal_groups = Counter(), Counter()
    if slots == 0:
        counts = dict(Counter(groups)) if groups is not None else {}
        return dict(slots=0, lru_misses=len(stream), belady_misses=len(stream),
                    lru_misses_by_group=counts, belady_misses_by_group=counts)
    lru, misses = OrderedDict(), 0
    for i, item in enumerate(stream):
        if item in lru:
            lru.move_to_end(item)
        else:
            misses += 1
            if groups is not None:
                lru_groups[groups[i]] += 1
            if len(lru) == slots:
                lru.popitem(last=False)
            lru[item] = None
    future, last = array('q', [len(stream)]) * len(stream), {}
    for i in range(len(stream) - 1, -1, -1):
        item = stream[i]
        future[i] = last.get(item, len(stream))
        last[item] = i
    resident, heap, optimal = {}, [], 0
    for i, item in enumerate(stream):
        if item not in resident:
            optimal += 1
            if groups is not None:
                optimal_groups[groups[i]] += 1
            if len(resident) == slots:
                while heap:
                    negative, victim = heapq.heappop(heap)
                    if resident.get(victim) == -negative:
                        del resident[victim]
                        break
        resident[item] = future[i]
        heapq.heappush(heap, (-future[i], item))
        # Bound stale heap entries even for long runs of repeated hits.
        if len(heap) > max(16, 3 * slots):
            heap = [(-position, key) for key, position in resident.items()]
            heapq.heapify(heap)
    return dict(slots=slots, lru_misses=misses, belady_misses=optimal,
                lru_misses_by_group=dict(lru_groups),
                belady_misses_by_group=dict(optimal_groups))


class WorkingSetObserver:
    """Streaming replay observer; retains only compact IDs for the oracle."""
    def __init__(self, layout, capacities_bytes=DEFAULT_CAPACITIES, *,
                 schedule_label, page_bytes=4096):
        if not schedule_label or page_bytes <= 0 or any(c < 0 for c in capacities_bytes):
            raise ValueError('invalid explicit schedule/capacities/page size')
        self.layout = layout
        self.entries = list(layout['experts'])
        self.ids = {(e['layer'], e['expert']): i for i, e in enumerate(self.entries)}
        if len(self.ids) != len(self.entries):
            raise ValueError('duplicate layout expert')
        self.capacities = tuple(capacities_bytes)
        self.schedule_label, self.page_bytes = schedule_label, page_bytes
        self.stream = array('I')
        self.stream_groups = array('I')
        self.group_ids = {}
        self.total, self.groups = Counter(), defaultdict(Counter)
        self.group_seen = defaultdict(set)
        self.trace_groups = defaultdict(Counter)
        self.seen, self.last = set(), {}
        self.last_phase = {}
        self.forward_seen, self.forward_key = {}, {}
        self.closed_forwards = set()
        self.last_event_index = -1
        self.finished = False

    def __call__(self, event):
        if self.finished:
            raise ValueError('observer already finalized')
        get = event.__getitem__ if isinstance(event, dict) else lambda key: getattr(event, key)
        index = get('event_index')
        if index <= self.last_event_index:
            raise ValueError('events must have strictly increasing indices')
        self.last_event_index = index
        if get('kind') != 'nvme_read':
            self.total['non_read_events'] += 1
            return
        item = self.ids[(get('layer'), get('expert'))]
        phase = get('runtime_phase')
        if phase == 'initial_hot':
            phase = 'startup'
        if phase not in ('startup', 'prefill', 'decode'):
            raise ValueError('unknown runtime phase')
        trace_phase = (event.get('trace_phase', get('runtime_phase')) if isinstance(event, dict)
                       else getattr(event, 'trace_phase', get('runtime_phase')))
        if trace_phase == 'initial_hot':
            trace_phase = 'startup'
        if trace_phase not in ('startup', 'prefill', 'decode'):
            raise ValueError('unknown trace phase')
        request = str(get('request_id'))
        forward = (request, str(get('forward_id')), get('layer'))
        layer = get('layer')
        if self.forward_key.get(layer) != forward:
            if forward in self.closed_forwards:
                raise ValueError('forward revisited after another forward on same layer')
            if layer in self.forward_key:
                self.closed_forwards.add(self.forward_key[layer])
            self.forward_key[layer] = forward
            self.forward_seen[layer] = set()
        if item not in self.seen:
            category = 'first_read'
        elif item in self.forward_seen[layer]:
            category = 'same_forward_repeat'
        elif self.last_phase[item] == 'startup' and phase != 'startup':
            category = 'startup_reuse'
        elif self.last[item][0] == request:
            category = 'cross_forward_same_request_repeat'
        else:
            category = 'cross_request_repeat'
        self.forward_seen[layer].add(item)
        self.seen.add(item)
        self.last[item] = forward
        self.last_phase[item] = phase
        self.stream.append(item)
        group = (request, phase)
        if group not in self.group_ids:
            self.group_ids[group] = len(self.group_ids)
        self.stream_groups.append(self.group_ids[group])
        self.group_seen[group].add(item)
        size = self.entries[item]['logical_bytes']
        for counter in (self.total, self.groups[group],
                        self.trace_groups[(request, trace_phase, phase)]):
            counter['logical_reads'] += 1
            counter['logical_bytes'] += size
            counter[category + '_reads'] += 1
            counter[category + '_bytes'] += size

    def finish(self):
        if self.finished:
            raise ValueError('observer already finalized')
        self.finished = True
        sizes = {e['logical_bytes'] for e in self.entries}
        models = []
        for cap in self.capacities:
            model = dict(capacity_bytes=cap)
            if len(sizes) == 1 and next(iter(sizes)) > 0:
                size = next(iter(sizes))
                model.update(_cache_counts(self.stream, size, cap, self.stream_groups))
                model['groups'] = [dict(request_id=key[0], phase=key[1],
                    lru_misses=model['lru_misses_by_group'].get(index, 0),
                    belady_misses=model['belady_misses_by_group'].get(index, 0))
                    for key, index in self.group_ids.items()]
                del model['lru_misses_by_group'], model['belady_misses_by_group']
                model.update(status='CONDITIONAL_OBJECT_MODEL', object_bytes=size,
                             lru_read_bytes=model['lru_misses'] * size,
                             belady_read_bytes=model['belady_misses'] * size)
            else:
                model.update(status='INDETERMINATE', reason='objects are not equal sized')
            models.append(model)
        groups = []
        for (request, phase), values in self.groups.items():
            keys = self.group_seen[(request, phase)]
            ledger = interval_ledger((self.entries[i] for i in keys), self.page_bytes)
            groups.append(dict(request_id=request, phase=phase, runtime_phase=phase, **values,
                               distinct_experts=len(keys), **ledger))
        startup = any(g['phase'] == 'startup' for g in groups)
        ledger = interval_ledger((self.entries[i] for i in self.seen), self.page_bytes)
        return dict(schema_version=1, schedule_label=self.schedule_label,
                    totals=dict(self.total), distinct_experts=len(self.seen),
                    page_bytes=self.page_bytes, working_set=ledger, groups=groups,
                    group_phase_semantics='phase and runtime_phase use runtime batching; see trace_phase_totals for original routing phase',
                    trace_phase_totals=[dict(request_id=req, trace_phase=trace,
                        runtime_phase=runtime, **values)
                        for (req, trace, runtime), values in self.trace_groups.items()],
                    startup_reads_observed=startup,
                    initial_state=('empty before supplied startup reads' if startup else
                                   'counterfactual empty cache; startup reads not supplied'),
                    ideal_expert_object_models=models,
                    physical_storage_read_bytes=None, total_physical_memory_bytes=None,
                    physical_memory_54gb_target='NOT_ESTABLISHED',
                    caveats=[
                        'Logical read events are conditional on the supplied worker/cache schedule.',
                        'The objects are actual raw file payloads, excluding staging scratch and GPU padding.',
                        '8/12/16 GiB is a dedicated optimistic expert pool, not a host or total RAM budget.',
                        'LRU and mandatory-admission Belady use a global fully associative equal-object pool.',
                        'The stream stays warm across forward/request boundaries; startup is separately counted.',
                        'Page cover is an interval union, not observed residency or a physical-I/O lower bound.',
                        'No Linux reclaim, readahead, shared charges, concurrent reads, or other model data is modeled.',
                        'Source and interval peaks must not be added to infer total physical RAM.'])


def _write_new(path, value):
    path = Path(path).resolve()
    root = Path(__file__).resolve().parents[2]
    if not any(path.is_relative_to(root / base) for base in ('build', '.q4t-work')):
        raise ValueError('output must be under build/ or .q4t-work/')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as destination:
        json.dump(value, destination, indent=2, sort_keys=True)
        destination.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--layout', type=Path)
    parser.add_argument('--events', type=Path)
    parser.add_argument('--schedule-label')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.model_dir and not args.layout and not args.events:
        _write_new(args.output, build_layout(args.model_dir))
    elif args.layout and args.events and args.schedule_label and not args.model_dir:
        observer = WorkingSetObserver(json.loads(args.layout.read_text()),
                                      schedule_label=args.schedule_label)
        with args.events.open() as source:
            for line in source:
                observer(json.loads(line))
        _write_new(args.output, observer.finish())
    else:
        parser.error('use --model-dir to freeze layout, or --layout --events --schedule-label')


if __name__ == '__main__':
    main()
