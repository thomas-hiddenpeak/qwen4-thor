"""Verify a previously interrupted Responses job through the official strict TLS client."""
import hashlib
import json
from pathlib import Path
import secrets
import ssl
import sys

import openai
try:
    import httpx2 as httpx
except ImportError:
    import httpx


def cancel_during_generation(owner, context, plan, destination):
    command = dict(request_id='sdk-failure-' + secrets.token_hex(8),
                   cancel_token=secrets.token_hex(32))
    requested = False
    terminal = None
    sequence = -1
    with owner.responses.create(
            model='qwen3.8-flash-next', store=True, stream=True,
            temperature=0, reasoning={'effort': 'none'},
            max_output_tokens=plan.get('max_output_tokens', 384),
            input='Write the integers from 1 through 300, one integer per line.',
            extra_body=command) as stream:
        for event in stream:
            sequence += 1
            with (destination/'live-sequence.jsonl').open('a') as trace:
                trace.write(json.dumps(dict(position=sequence, observed=event.sequence_number,
                                            event=event.type))+'\n')
            assert event.sequence_number == sequence and sequence < 1200 and terminal is None, (
                sequence, event.sequence_number, event.type)
            if event.type == 'response.output_text.delta' and not requested:
                with httpx.Client(verify=context, trust_env=False, timeout=10) as control:
                    receipt = control.post(plan['base'] + '/requests/cancel', json=command,
                        headers={'Authorization': 'Bearer ' + plan['key']})
                assert receipt.status_code == 202
                requested = True
            if event.type == 'response.failed':
                terminal = event.response
            assert event.type not in ('response.completed', 'response.incomplete', 'error')
    assert requested and terminal is not None and terminal.status == 'failed'
    assert terminal.error.code == 'server_error' and terminal.error.type == 'explicit_cancel'
    saved = owner.responses.with_raw_response.retrieve(terminal.id)
    assert saved.status_code == 200 and saved.parse() == terminal
    (destination/'live-get.json').write_bytes(saved.http_response.content)
    with owner.responses.retrieve(terminal.id, stream=True, starting_after=sequence-1) as replay:
        last, = replay
    assert last.type == 'response.failed' and last.sequence_number == sequence and last.response == terminal
    (destination/'live-decoded-terminal.json').write_text(last.model_dump_json(exclude_unset=True))
    return dict(id=terminal.id, control=202, sequence=sequence, get=saved.status_code,
                code=terminal.error.code, cause=terminal.error.type)


def restored_generation(owner, peer, previous, destination):
    rid = previous['id']
    raw = owner.responses.with_raw_response.retrieve(rid)
    resource = raw.parse()
    path = destination / 'restored-get.json'
    path.write_bytes(raw.http_response.content)
    assert raw.status_code == 200 and resource.id == rid and resource.status == 'failed'
    assert resource.error.code == 'server_error' and resource.error.type == 'explicit_cancel'
    with owner.responses.retrieve(rid, stream=True, starting_after=previous['sequence'] - 1) as stream:
        last, = stream
    assert last.type == 'response.failed' and last.response == resource
    assert last.sequence_number == previous['sequence']
    terminal = destination / 'restored-terminal.json'
    terminal.write_text(last.model_dump_json(exclude_unset=True))
    try:
        peer.responses.retrieve(rid)
    except openai.NotFoundError as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError('Restored response was visible across clients')
    return dict(id=rid, sequence=last.sequence_number, get=raw.status_code, peer=404,
                resource_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                terminal_sha256=hashlib.sha256(terminal.read_bytes()).hexdigest())


def main():
    plan = json.load(sys.stdin)
    context = ssl.create_default_context(cafile=plan.get('certificate'))
    destination = Path(plan['capture'])
    destination.mkdir(mode=0o700)
    options = dict(base_url=plan['base'], max_retries=0, timeout=20,
                   _strict_response_validation=True)
    betas = ['responses_multi_agent=v1']
    rid = plan['id']
    cursor = plan['last_sequence'] - 1
    with (openai.OpenAI(api_key=plan['key'], http_client=httpx.Client(verify=context, trust_env=False),
                        **options) as owner,
          openai.OpenAI(api_key=plan['other'], http_client=httpx.Client(verify=context, trust_env=False),
                        **options) as peer):
        api = owner.beta.responses
        raw = api.with_raw_response.retrieve(rid, betas=betas)
        response = raw.parse()
        assert raw.status_code == 200 and response.status == 'failed' and response.id == rid
        error = response.error
        assert (error.code, error.type, error.message) == ('server_error', plan['cause'], plan['message'])
        (destination/'get.json').write_bytes(raw.http_response.content)
        resumed = api.with_raw_response.retrieve(rid, stream=True, starting_after=cursor, betas=betas)
        assert resumed.status_code == 200
        with resumed.parse() as stream:
            terminal, = stream
        assert terminal.type == 'response.failed' and terminal.sequence_number == cursor + 1
        assert terminal.response.model_dump(exclude_unset=True) == response.model_dump(exclude_unset=True)
        # This file is the SDK's decoded event, not a raw network capture.
        (destination/'decoded-event.json').write_text(terminal.model_dump_json(exclude_unset=True))
        cancel = api.with_raw_response.cancel(rid, betas=betas)
        assert cancel.status_code == 200 and cancel.parse() == response
        (destination/'cancel.json').write_bytes(cancel.http_response.content)
        try:
            peer.beta.responses.retrieve(rid, betas=betas)
        except openai.NotFoundError as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError('Foreign response was visible')
        previous = plan.get('previous')
        restored = restored_generation(owner, peer, previous, destination) if previous else None
        live = cancel_during_generation(owner, context, plan, destination)
    print(json.dumps(dict(sdk=openai.__version__, id=rid, strict=True, get=raw.status_code,
                          resumed=resumed.status_code, cancel=cancel.status_code, peer=404,
                          public_code=error.code, cause=error.type, sequence=cursor+1, live=live,
                          restored=restored,
                          captures={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(destination.iterdir())})))


if __name__ == '__main__':
    main()
