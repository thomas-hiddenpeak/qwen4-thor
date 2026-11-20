"""Two failed tasks, explicit caller instructions, unchanged native defaults."""
from tools.serve_realtime import duplex_checks as c
from tools.serve_service.package import require

POLICY = 'original_language_v1'
INSTRUCTIONS = ('逐字抄写本条用户消息中第一段语音的原始转写，仅输出其中第一个完整句子一次。'
    '保持原语言，不翻译，不包含前后的文字说明或第二段语音，不增加任何解释。'
    '如果收到的是原生空闲未检测到语音的提示，只输出 READY。')


def observe(value, phrase):
    mode=value['mode'];events=value['events']
    require(mode in ('mixed','vad_once') and value['status']=='PASS' and
        value['transport']=='official-openai-wss' and value['connections']==1 and
        value['close_code']==1000 and value['automatic_replay'] is False and
        value['response_creates']==int(mode=='mixed') and
        value['instruction_policy']==POLICY, 'targeted actual WSS scope')
    require(len(events)<=8192 and len({e['event_id'] for e in events})==len(events) and
        not any(e['type']=='error' for e in events), 'targeted replay/error')
    updates=[e['session'] for e in events if e['type']=='session.updated']
    require(len(updates)==2 and all(s['instructions']==INSTRUCTIONS for s in updates),
            'effective caller instruction differs')
    responses,total=c.response_rows(value)
    label='mixed' if mode=='mixed' else 'vad_auto'
    if mode=='mixed':
        require(value['retrievals'] and len(value['retrievals'])==1,'mixed retrieval missing')
        item=value['retrievals'][0]['item'];parts=item['content']
        require(item['id']=='mixed_input' and len(parts)==4 and
            parts[0]==dict(type='input_text',text='Repeat the first audio sentence only.') and
            parts[2]==dict(type='input_text',text='Second audio follows.') and
            parts[1]['transcript']=='REFERENCE_ONLY_NEVER_SPOKEN' and
            all(c.speech_checks.decode64(parts[i]['audio']) for i in (1,3)),
            'mixed real content or reference changed')
        expected=[('mixed_input',1),('mixed_input',3)]
    else:
        commits=[e for e in events if e['type']=='input_audio_buffer.committed']
        starts=[e for e in events if e['type']=='input_audio_buffer.speech_started']
        require(len(commits)==len(starts)==1 and commits[0]['item_id']==starts[0]['item_id'] and
            updates[-1]['audio']['input']['turn_detection']['create_response'] is True and
            updates[-1]['audio']['input']['turn_detection']['idle_timeout_ms'] is None,
            'automatic VAD scope changed')
        expected=[(commits[0]['item_id'],0)]
    tokens,quality=c.input_rows(events,expected,phrase)
    answer=c.judgment(label,c.text(responses[label]),phrase)
    require(value['quality']==[answer],'quality differs from original output')
    return dict(admitted=1+len(expected),visible_tokens=total+tokens,
                quality=[answer,*quality],private_asr=0)
