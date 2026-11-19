"""Reuse only the completed primary Chat/tool component of the known failure.

The failed Prompt component must run anew, as must the unexecuted light
profile. Original files are read-only, and the failure is retained verbatim.
"""
import copy
import json
from pathlib import Path

from tools.serve_recovery import ledger, profile_evidence as evidence
from tools.serve_recovery import profile_prompt_reuse as reuse
from tools.serve_recovery.profile_primary_review import RecordedPrimary
from tools.serve_service.package import read, require, save, sha

ROOT = Path(__file__).resolve().parents[2]
RESULT = 'ce67b8474cfaffa2ef8138e97040ef6e13cdb87013d8de19b8daff6d1fb124aa'
FAILED_PROMPT = '44b30834d26b06446e6b5c52fa61c47a57dbfdba89ce101f01e6718b51ec6bcc'


def review(source):
    from tools.serve_recovery import profile_prompt_targeted as driver
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(ROOT / '.q4t-work'), 'private original stage required')
    actor = RecordedPrimary(source); plan = actor.plan
    result = read(source / 'result.json'); failed = read(source / 'primary-prompt.sdk.json')
    require(ledger.digest(result) == RESULT and ledger.digest(failed) == FAILED_PROMPT and
        result['status'] == 'FAILED' and result['counts']['starts'] == 1 and
        result['binary_sha256'] == plan['binary_sha256'] == reuse.REVIEW['candidate_binary'] and
        plan['requests'] == driver.requests(), 'unknown original partial stage')
    lease = read(source / 'lease/exit.json'); cleanup = read(source / 'cleanup.json')
    queries = [json.loads(line) for line in (source / 'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(lease['status'] == 'COMMAND_FAILED' and lease['child_returncode'] == 1 and
        lease['compute_backend'] == 'nvml' and lease['lease_inherited_by_child'] is True and
        queries and all(q['status'] == 'OK' and q['returncode'] == 0 for q in queries) and
        queries[-1]['pids'] == [] and cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] is True and cleanup['lease_continuously_held'] is True,
        'partial stage environment or cleanup not accepted')
    rows = plan['requests']; schema = actor.request('schema', rows['schema'])
    call = actor.request('tool', rows['tool']); message = evidence.message(call, 'tool_calls')
    calls = message['tool_calls']; document = ROOT / driver.full.DOCUMENT
    require(len(calls) == 1 and calls[0]['function']['name'] == 'project_document' and
        json.loads(calls[0]['function']['arguments']) == dict(path=driver.full.DOCUMENT) and
        sha(document) == plan['sources'][driver.full.DOCUMENT], 'original actual tool differs')
    reply = dict(role='tool', tool_call_id=calls[0]['id'], content=json.dumps(dict(
        path=driver.full.DOCUMENT, sha256=sha(document), answer=sha(document)[:12])))
    follow = copy.deepcopy(rows['tool']); follow['tool_choice'] = 'none'
    follow['messages'] += [message, reply]
    conversation = read(source / 'primary-conversation-business.json')
    require(conversation['session'] == read(source / 'primary-conversation.sdk.json') and
        conversation['source'] == plan['conversation']['source'], 'original conversation projection changed')
    evidence.conversation_business(conversation)
    actor.metrics('conversation-before', conversation['before'])
    actor.after_metrics('conversation', conversation['after'])
    fresh = dict(schema=schema, tool_call=call, tool_result=actor.request('tool-result', follow),
        tool_reply=reply, tool_source=dict(path=driver.full.DOCUMENT, content=document.read_text()),
        conversation=conversation)
    # Validate completed text/schema/tool business using the corresponding old
    # backend observations and legacy conversation declaration. No Prompt
    # success is invented for the failed/incomplete component.
    _, previous = driver.original(plan, 'primary')
    combined = copy.deepcopy(previous['business']); combined.update(fresh)
    capability = actor.value('capabilities')
    require(capability == read(source / 'primary-capabilities.json') and
        actor.value('config') == read(source / 'primary-config.json'), 'original projection differs')
    old_declaration = copy.deepcopy(capability)
    old_declaration['realtime']['conversation'] = previous['capabilities']['realtime']['conversation']
    evidence.business(combined, plan['identity'], plan['profiles']['primary'], old_declaration)
    return dict(format='q4t-prompt-primary-text-continuation-v1',
        source_plan_sha256=sha(source / 'plan.json'), source_commit=plan['source_commit'],
        identity=plan['identity'], capacity=plan['profiles']['primary'], capabilities=capability,
        business=fresh, original_result=result, original_failed_prompt=failed,
        original_environment=dict(lease=lease, queries=queries, cleanup=cleanup),
        original_failure_preserved=True, reused_actual_generations=5,
        original_actual_generations=8)


def prepare(source, plan):
    source = Path(source).resolve(strict=True)
    proof = review(source)
    require(proof['identity'] == plan['identity'] and proof['capacity'] == plan['profiles']['primary'] and
        ledger.digest(proof) == reuse.PRIMARY_PARTIAL,
        'original partial observation is not the reviewed candidate/profile')
    files = {str(p): sha(p) for p in source.rglob('*') if p.is_file()}
    return dict(source=str(source), files=files, proof=proof)


def verify(selected):
    for name, digest in selected['files'].items():
        require(sha(name) == digest, 'original partial evidence changed: ' + name)
    require(ledger.digest(selected['proof']) == reuse.PRIMARY_PARTIAL and
        ledger.canonical(review(selected['source'])) == ledger.canonical(selected['proof']),
        'partial evidence reconstruction changed')


def environment(out, selected, current):
    verify(selected)
    proof = selected['proof']
    save(out / 'combined-environment.json', dict(current=current,
        original=proof['original_environment'], original_result=proof['original_result'],
        reused_actual_generations=5, original_failure_preserved=True, separate_continuous_leases=True))
    return dict(current,
        nvml_queries=current['nvml_queries'] + len(proof['original_environment']['queries']),
        lease_result_sha256=sha(out / 'combined-environment.json'))
