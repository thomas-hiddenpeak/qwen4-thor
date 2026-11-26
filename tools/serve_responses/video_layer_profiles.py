"""Qualify retained composited movie inputs across the primary and light profiles."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses import video_snapshot_bindings, video_track_grid_profiles as grid
from tools.serve_responses.video_track_grid_handoff_sdk import verify_text_stream
from tools.serve_service import package

KEY = 'video_layer_handoff'
ACTOR = grid.ACTOR
ROOT = lifecycle.ROOT / 'docs/evidence/serve-video-layer-composition-20261127'
PROOF = ROOT / 'business.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-chatkit-interrupted-retry-20261127/delivery.json'
COLORS = {'first_bottom_left': 'red', 'last_bottom_right': 'black'}


def snapshot_ids(proof, decode, controls):
    names = ('first-in-front', 'second-in-front')
    original = [decode['normal_decode'][name]['source_sha256'] for name in names]
    frozen = proof['frozen']
    require = package.require
    require(proof['sdk']['sources'] == original and len(set(original)) == 2 and
            set(frozen) == set(original), 'wrong uploaded or inline video')
    ids = {}
    for name, digest in zip(names, original, strict=True):
        saved = frozen[digest]
        conversion = saved['conversion']
        display = conversion['display_geometry']
        expected = controls['frames'][name]
        require(conversion['source_sha256'] == digest and conversion['format'] == 'q4t.cpu_video_png.v9'
                and [conversion['width'], conversion['height']] == decode['normal_decode'][name]['dimensions']
                and conversion['duration_ns'] == 3000000000 and conversion['scanned_frames'] == len(expected) == 6
                and display['policy'] == 'quicktime-active-layers-copy-bilinear16'
                and display['decoded_frames'] == 20 and display['decoded_pixels'] == 303104,
                'unexpected movie geometry, timeline or decode work')
        require(conversion['frames'] == [
            dict(source_index=f['source_index'], timestamp_ns=f['timestamp_ns'],
                 duration_ns=f['duration_ns'], sha256=f['png_sha256']) for f in expected]
                and saved['png'] == {str(f['source_index']): f['png_sha256'] for f in expected},
                'composed pixels or presentation intervals differ from independent controls')
        ordered = [2, 1] if name == 'first-in-front' else [1, 2]
        layers = display['layers']
        require([layer['source']['track_id'] for layer in layers] == ordered
                and [layer['source']['layer'] for layer in layers] == [3, -2],
                'source layer order changed')
        for track, layer in zip(ordered, layers, strict=True):
            count = 6 if track == 1 else 4
            step = 500000000
            intervals = [[i, i * step, (i + 1) * step] for i in range(count)]
            require(layer['scanned_frames'] == count and layer['duration_ns'] == count * step
                    and layer['source']['alternate_group'] == 0
                    and layer['samples'] == intervals + [None] * (6 - count),
                    'short track remained visible after its end, or source samples changed')
        ids[digest] = saved['record']
    require(len(set(ids.values())) == 2, 'two opposite layer orders shared a frozen snapshot')
    return ids


def prepare(out, build, deployment, interpreter, source):
    proof = package.read(PROOF)
    ids = snapshot_ids(proof, package.read(ROOT/'composition.json'), package.read(ROOT/'business-controls.json'))
    sdk = subprocess.check_output([interpreter, '-IB', '-c', 'import openai;print(openai.__version__)'],
                                  text=True, timeout=15).strip()
    package.require(proof['run']['status'] == 'PASS' and sdk == proof['sdk']['sdk'] and
                    proof['product'] == {'binary': package.sha(build/'q4t'), 'ingress': package.sha(build/'q4t-ingress')},
                    'native layered video business, product or SDK changed')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.INTERRUPTED_RETRY_REVIEWED, KEY,
                                [ACTOR, PROOF, RELEASE, ROOT/'composition.json', ROOT/'business-controls.json',
                                 Path(approvals.__file__), Path(video_snapshot_bindings.__file__),
                                 Path(grid.__file__), Path(__file__)])
    store = out/'state/application-objects'
    approvals.seed(source, store, proof)
    package.require(video_snapshot_bindings.check(store, proof['sdk'], ids) == proof['bindings'],
                    'copied Responses history lost the original composed inputs')
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        command = plan['command']
        package.require(Path(command[command.index('--object-store-dir') + 1]) == store,
                        'both profiles must reopen the seeded video inputs')
        plan[KEY]['interpreter'] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    grid.check_handoff(row, previous, package.read(PROOF)['sdk'], COLORS)
    for result in (row,) if previous is None else (previous, row):
        trace = result.get('stream', {})
        events = result['events']
        package.require(trace.get('response') == result['saved']['response']
                        and re.fullmatch(r'msg_[A-Za-z0-9_-]+', trace.get('item', ''))
                        and isinstance(trace.get('output_index'), int) and trace['output_index'] >= 0
                        and trace.get('events') == len(events) and events.count('response.output_text.done') == 1
                        and trace.get('deltas') == events.count('response.output_text.delta')
                        and re.fullmatch(r'[0-9a-f]{64}', trace.get('text_sha256', '')),
                        'missing or inconsistent streamed text receipt')


def exercise(params, interpreter, errors, captures, previous=None):
    row = grid.run_client(dict(params, layered=True), interpreter, errors, captures, previous, PROOF, check)
    stream = json.loads((captures/'stream.json').read_bytes())
    current = json.loads((captures/'current.json').read_bytes())
    package.require(verify_text_stream(stream, current) == row['stream']
                    and row['stream']['response'] == row['saved']['response']
                    and [event['type'] for event in stream] == row['events'],
                    'typed SDK stream does not reconstruct the retained response')
    return row
