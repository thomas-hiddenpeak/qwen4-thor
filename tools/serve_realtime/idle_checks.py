"""Independent idle-only protocol and fixed-input diagnostic evidence checks."""
import copy
from tools.serve_realtime import duplex_checks as c
from tools.serve_service.package import require

OLD_CUE = '[No speech was detected during the idle interval. Invite the user to continue.]'
NEW_CUE = '[No speech was detected during the idle interval.]'


def prompt_pair(before, after):
    """Require the production reconstruction to change only the native fact."""
    require(before['request']['messages'][-1]['content'][0]['text'] == OLD_CUE
            and after['request']['messages'][-1]['content'][0]['text'] == NEW_CUE,
            'original/candidate native idle input differs')
    candidate = copy.deepcopy(before)
    candidate['request']['messages'][-1]['content'][0]['text'] = NEW_CUE
    candidate['snapshot'][-1]['content'][0]['text'] = NEW_CUE
    require(before['prompt'].count(OLD_CUE) == 1, 'original prompt cue count')
    candidate['prompt'] = before['prompt'].replace(OLD_CUE, NEW_CUE)
    require(candidate == after, 'other instructions/history/sampling changed')
    require(before['request']['temperature'] == 1, 'original sampling changed')


def observe(value, phrase):
    require(value['mode'] == 'idle' and value['status'] == 'PASS' and
            value['transport'] == 'official-openai-wss' and
            value['connections'] == 1 and value['close_code'] == 1000 and
            value['automatic_replay'] is False and value['response_creates'] == 0,
            'native idle SDK boundary')
    events = value['events']
    require(len({e['event_id'] for e in events}) == len(events) and
            not any(e['type'] == 'error' for e in events), 'idle event failure')
    responses, tokens = c.response_rows(value)
    starts = [e for e in events if e['type'] == 'input_audio_buffer.speech_started']
    commits = [e for e in events if e['type'] == 'input_audio_buffer.committed']
    idle = [e for e in events if e['type'] == 'input_audio_buffer.timeout_triggered']
    require(len(starts) == 1 and len(commits) == 2 and len(idle) == 1 and
            starts[0]['item_id'] == commits[0]['item_id'] and
            idle[0]['item_id'] == commits[1]['item_id'] and
            idle[0]['audio_start_ms'] == idle[0]['audio_end_ms'],
            'actual idle and speech input ownership')
    at = events.index(idle[0])
    first_done = next(i for i,e in enumerate(events) if e['type'] == 'response.done'
                      and e['response']['id'] == responses['vad_auto']['id'])
    next_created = next(i for i,e in enumerate(events) if e['type'] == 'response.created'
                       and e['response']['id'] == responses['idle']['id'])
    require(first_done < at < next_created, 'idle did not follow real response')
    require(not any(e['type'].startswith('conversation.item.input_audio_transcription.')
                    and e['item_id'] == idle[0]['item_id'] for e in events),
            'zero-input ASR fabricated')
    asr_tokens, asr_quality = c.input_rows(events, [(commits[0]['item_id'],0)], phrase)
    quality = [c.judgment('vad_auto', c.text(responses['vad_auto']), phrase),
               c.judgment('idle', c.text(responses['idle']), 'READY')]
    require(value['quality'] == quality, 'original idle quality changed')
    return dict(visible_tokens=tokens+asr_tokens, private_asr=0, admitted=3,
                quality=quality+asr_quality)
