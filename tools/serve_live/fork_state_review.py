"""Review the retained real recording whose verifier expected unframed ASR text."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live.recording_acceptance import LIMITS, observe, verify_conversation
from tools.serve_service.package import read, require, sha


def counterexamples(state, receipt):
    rejected = []
    for name in ('wrong_source', 'wrong_transcript', 'missing_result',
                 'foreign_result', 'incorrect_project', 'missing_answer'):
        changed = deepcopy(state)
        history = changed['frontend_history']
        if name in ('wrong_source','wrong_transcript'):
            item = next(v for v in history if v.get('role') == 'user' and
                        'Audio input transcript:' in v['content'][0].get('text', ''))
            part = item['content'][0]
            old = item['id'] if name == 'wrong_source' else receipt['expected_input']
            part['text'] = part['text'].replace(old, 'different')
        elif name in ('missing_result','foreign_result'):
            result = next(v for v in history if v['type']=='function_call_output')
            if name == 'missing_result':
                history.remove(result)
            else:
                result['call_id'] = 'foreign_call'
        elif name == 'incorrect_project':
            part = next(v['content'][0] for v in history if v.get('role')=='user'
                        and receipt['record']['sha256'] in v['content'][0]['text'])
            part['text'] = part['text'].replace(receipt['record']['sha256'], '0'*64)
        else:
            history.pop(next(i for i,v in enumerate(history) if v.get('role')=='assistant'
                             and v['content'][0].get('type')=='output_audio'))
        try:
            verify_conversation(changed, receipt)
        except RuntimeError:
            rejected.append(name)
        else:
            raise ValueError('damaged record accepted: '+name)
    return rejected


def review(out):
    plan, original = read(out/'plan.json'), read(out/'result.json')
    require(original['status']=='FAIL' and original['error'].splitlines()[-1] ==
            'RuntimeError: spoken input and real tool data', 'expected verifier failure')
    require(plan['verify_fork_state'] and original['counts']==read(out/'counts.json')
            and 0 < original['seconds'] <= LIMITS['seconds'] and original['default_unchanged']
            and all(original['counts'][k]==v for k,v in LIMITS.items() if k not in ('controls','seconds'))
            and original['counts']['controls']<=LIMITS['controls'], 'bounded execution')
    require(original['binary_sha256']==plan['binary_sha256']==sha(plan['binary']) and
            sha(plan['ingress'])==plan['ingress_sha256'] and
            all(sha(p)==digest for p,digest in plan['protected'].items()), 'binaries and default')
    # The code that emitted, recorded, stored and downloaded the source has not changed.
    require(all(sha(ROOT/name)==digest for name,digest in plan['sources'].items()
                if name.startswith(('src/','include/','cmake/'))), 'runtime identity')
    lease, cleanup = read(out/'lease/exit.json'), read(out/'cleanup.json')
    require(lease['status']=='COMMAND_FAILED' and lease['child_returncode']==1 and
            lease['compute_backend']=='nvml' and lease['lease_inherited_by_child'] and
            cleanup['remaining']==[] and cleanup['model_exit']==0 and cleanup['ports_released']
            and cleanup['lease_continuously_held'], 'lease and cleanup')
    queries = [json.loads(line) for line in (out/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(queries and all(q['status']=='OK' and q['returncode']==0 for q in queries), 'NVML')
    values = read(out/'final-drained.json')
    require(all(values[k]==0 for k in ('q4t_active_chats','q4t_realtime_sessions',
            'q4t_realtime_host_reserved_bytes')) and values['q4t_seq_slots_free']==1, 'drain')
    choice = read(out/'core-after.wire')['choices'][0]
    require(choice['finish_reason']=='stop' and choice['message']['content'].strip()=='42', 'core Chat')
    observation = observe(out)
    receipt = read(out/'sdk.json')
    scope = hashlib.sha256(b'client:alpha').hexdigest()
    key = hashlib.sha256(receipt['recording']['id'].encode()).hexdigest()
    path = out/'objects'/f'{scope}.live_fork_state.fork_{key}.json'
    rejected = counterexamples(read(path)['record']['data'],receipt)
    originals = ['result.json','plan.json','sdk.json','events.jsonl','core-after.wire',
                 'recording.wav','lease/exit.json','lease/nvml-queries.jsonl','cleanup.json',
                 'live-drained.json','final-drained.json',str(path.relative_to(out))]
    return dict(review='PASS',original_status=original['status'],lease_status=lease['status'],
                original_error=original['error'].splitlines()[-1],seconds=original['seconds'],
                counts=original['counts'],nvml_queries=len(queries),cleanup_complete=True,
                default_unchanged=True,core='42',additional_model_starts=0,
                fork=observation['fork_state'],wave_frames=observation['frames'],
                input_track=observation['left'],output_track=observation['right'],
                counterexamples=rejected,raw_sha256={name:sha(out/name) for name in originals})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    print(json.dumps(review(parser.parse_args().output),ensure_ascii=False,indent=2))
