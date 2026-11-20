"""Independent checks of actual Realtime MCP wire, peer reads and settlement."""
import json
from tools.serve_realtime.conversation_acceptance import response_usage
from tools.serve_responses import mcp_business_peer as peer
from tools.serve_service.package import require

MODES = ('direct-deny-import', 'approve-unknown')
ORDINARY_PROMPT = 'Compute 6 * 7. Reply only with the decimal number.'


def observe(receipt, journal, remote, source, before, after):
    mode = receipt['mode']
    require(mode in MODES and receipt['status'] in ('PASS', 'QUALITY_FAIL') and
            receipt['transport'] == 'official-openai-wss' and
            receipt['connections'] == 1 and receipt['close_code'] == 1000 and
            receipt['automatic_replay'] is False, 'SDK connection contract')
    events = receipt['events']
    require(events == journal and not any(e['type'] == 'error' for e in events),
            'actual event journal missing or protocol error')
    responses, tools = receipt['responses'], receipt['tools']
    require(len(responses) == 4 and len(tools) == 2 and
            len({r['id'] for r in responses}) == 4 and
            all(r['status'] == 'completed' for r in responses[:3]) and
            responses[3]['status'] in ('completed', 'incomplete') and
            [e['response'] for e in events if e['type'] == 'response.done'] == responses,
            'actual generation terminals missing or duplicated')
    if responses[3]['status'] == 'incomplete':
        require(responses[3]['status_details']['reason'] == 'max_output_tokens' and
                responses[3]['usage']['output_tokens'] == 256,
                'incomplete response did not reach its actual frozen budget')
    tokens = sum(response_usage(r) for r in responses)
    require(after['admitted'] - before['admitted'] == 4 and
            after['settled'] - before['settled'] == tokens and
            all(after[k] == 0 for k in ('reserved', 'active', 'sessions', 'host_bytes')) and
            after['free_slots'] == 1, 'native MCP usage or resource settlement')
    for response, actual in zip((responses[0], responses[2]), tools):
        generated = [i for i in response['output'] if i['type'] == 'mcp_call']
        require(len(generated) == 1, 'one generated call required')
        call = generated[0]
        require(not call.get('output') and not call.get('error'),
                'model completion fabricated a tool outcome')
        for item in (call, actual):
            require(item['name'] == 'read_project_record' and
                    item['server_label'] == 'project' and
                    json.loads(item['arguments']) == peer.ARGUMENTS and
                    item['id'] == call['id'], 'MCP call identity changed')
        deltas = [e['delta'] for e in events if
                  e['type'] == 'response.mcp_call_arguments.delta' and
                  e.get('response_id') == response['id'] and e.get('item_id') == call['id']]
        require(''.join(deltas) == call['arguments'], 'MCP stream differs')
        terminal = [e for e in events if e['type'] == 'response.output_item.done' and
                    e.get('item', {}).get('id') == call['id']]
        require(len(terminal) == 1 and terminal[0]['item'] == actual and
                events.index(terminal[0]) > next(i for i, e in enumerate(events) if
                    e['type'] == 'response.done' and e['response']['id'] == response['id']),
                'tool terminal must follow its actual generation terminal')
        require(any(e['type'] == 'conversation.item.done' and e.get('item') == actual
                    for e in events), 'actual tool result not retained in conversation')
    reads = remote['calls']
    require(len(reads) == (1 if mode == MODES[0] else 2) and
            all(r['ok'] and r['source'] == source for r in reads) and
            remote['methods'].count('tools/call') == len(reads),
            'actual read/dispatch count or source differs; replay detected')
    result = json.loads(tools[0]['output'])
    require(not tools[0].get('error') and result['structuredContent'] == reads[0] and
            result['isError'] is False, 'SDK did not receive the actual project data')
    expected = 'mcp_approval_denied' if mode == MODES[0] else 'mcp_result_unknown'
    require(not tools[1].get('output') and expected in json.dumps(tools[1]['error']),
            'denial/unknown result was lost or relabelled as success')
    approval = [e['item'] for e in events if e['type'] == 'conversation.item.done' and
                e.get('item', {}).get('type') == 'mcp_approval_request']
    decision = [e['item'] for e in events if e['type'] == 'conversation.item.done' and
                e.get('item', {}).get('type') == 'mcp_approval_response']
    require(len(approval) == len(decision) == 1 and
            decision[0]['approval_request_id'] == approval[0]['id'] and
            decision[0]['approve'] is (mode == MODES[1]), 'actual approval boundary')
    approved_call = tools[1 if mode == MODES[0] else 0]
    require(approved_call['approval_request_id'] == approval[0]['id'] and
            approval[0]['arguments'] == approved_call['arguments'] and
            approval[0]['name'] == approved_call['name'] and
            approval[0]['server_label'] == approved_call['server_label'],
            'approval was rebound to another call')
    sent = [e for e in events if e['type'] == 'response.mcp_call.in_progress' and
            e.get('item_id') == approved_call['id']]
    if mode == MODES[0]:
        require(not sent, 'denied tool was dispatched')
    else:
        require(len(sent) == 1 and events.index(sent[0]) > next(
            i for i, e in enumerate(events) if e['type'] == 'conversation.item.done'
            and e.get('item') == decision[0]), 'tool dispatched before approval')
    if mode == MODES[0]:
        require(any(e['type'] == 'mcp_list_tools.failed' for e in events),
                'actual catalog fault not exercised')
    question = [e for e in events if e['type'] == 'conversation.item.done' and
                e.get('item', {}).get('id') == 'user_mcp_after_fault']
    require(len(question) == 1 and question[0]['item']['role'] == 'user' and
            question[0]['item']['content'] == [dict(type='input_text', text=ORDINARY_PROMPT)],
            'ordinary task needs its own actual user turn')
    created = [e for e in events if e['type'] == 'response.created' and
               e.get('response', {}).get('id') == responses[3]['id']]
    require(len(created) == 1 and events.index(question[0]) < events.index(created[0]),
            'new user task was not present when the ordinary response started')
    text = lambda r: ''.join(p['text'] for i in r['output'] if i['type'] == 'message'
                            for p in i['content'] if p['type'] == 'output_text').strip()
    title = source['text'].splitlines()[0].split(': ', 1)[1].lstrip('# ')
    quality = [dict(case='document', answer=text(responses[1]), status='PASS' if
                    title in text(responses[1]) and source['citation'] in text(responses[1]) else 'FAIL'),
               dict(case='ordinary_after_fault', answer=text(responses[3]),
                    status='PASS' if responses[3]['status'] == 'completed' and
                    text(responses[3]) == '42' else 'FAIL')]
    require(quality == receipt['quality'] and receipt['status'] ==
            ('PASS' if all(r['status'] == 'PASS' for r in quality) else 'QUALITY_FAIL'),
            'model quality evidence was changed or misclassified')
    return dict(protocol='PASS', quality=quality, model_jobs=4, settled_tokens=tokens,
                actual_reads=len(reads), automatic_tool_replays=0, resources_released=True)


def observe_repair(receipt, journal, remote, retained, before, after):
    """One new user turn after an actual fault, using sealed retained history."""
    require(receipt['mode']=='ordinary-repair' and receipt['status'] in ('PASS','QUALITY_FAIL') and
            receipt['transport']=='official-openai-wss' and receipt['connections']==1 and
            receipt['close_code']==1000 and receipt['automatic_replay'] is False and
            receipt['tools']==[] and len(receipt['responses'])==1 and receipt['events']==journal,
            'bounded repair SDK identity')
    events=journal;response=receipt['responses'][0]
    require(not remote['calls'] and 'tools/call' not in remote['methods'] and
            not any(e['type'] in ('error','response.mcp_call.in_progress') for e in events) and
            any(e['type']=='mcp_list_tools.failed' for e in events),
            'history repair dispatched a tool or skipped the real catalog fault')
    require([e['response'] for e in events if e['type']=='response.done']==[response] and
            response['status'] in ('completed','incomplete'), 'one actual repair terminal')
    if response['status']=='incomplete':
        require(response['status_details']['reason']=='max_output_tokens' and
                response['usage']['output_tokens']==256,'actual repair budget exhausted')
    require(len(retained)==2 and retained[0].get('output') and retained[1].get('error'),
            'exact successful and denied history required')
    for item in retained:
        copied=[e['item'] for e in events if e['type']=='conversation.item.done' and
                e.get('item',{}).get('id')==item['id']]
        require(copied==[item], 'actual sealed MCP history was altered')
    question=[e for e in events if e['type']=='conversation.item.done' and
              e.get('item',{}).get('id')=='user_mcp_after_fault']
    created=[e for e in events if e['type']=='response.created' and
             e.get('response',{}).get('id')==response['id']]
    require(len(question)==len(created)==1 and question[0]['item']['role']=='user' and
            question[0]['item']['content']==[dict(type='input_text',text=ORDINARY_PROMPT)] and
            events.index(question[0])<events.index(created[0]),'new user task absent or late')
    tokens=response_usage(response)
    require(after['admitted']-before['admitted']==1 and after['settled']-before['settled']==tokens and
            all(after[k]==0 for k in ('reserved','active','sessions','host_bytes')) and
            after['free_slots']==1,'actual repair settlement or release differs')
    answer=''.join(p['text'] for i in response['output'] if i['type']=='message'
                   for p in i['content'] if p['type']=='output_text').strip()
    quality=[dict(case='ordinary_after_fault',answer=answer,
        status='PASS' if response['status']=='completed' and answer=='42' else 'FAIL')]
    require(receipt['quality']==quality and receipt['status']==
            ('PASS' if quality[0]['status']=='PASS' else 'QUALITY_FAIL'), 'repair quality misclassified')
    return dict(protocol='PASS',quality=quality,model_jobs=1,settled_tokens=tokens,
        actual_reads=0,automatic_tool_replays=0,retained_history_authenticated=True,resources_released=True)
