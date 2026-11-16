"""Chat moderation schemas, optional real CPU planner and both wire clients."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import unittest

from tools.examples import chat_client
from tools.serve_protocol.build_contract import chat_schema, vision_schema
from tools.serve_protocol.check_contract import Validator

ROOT = Path(__file__).resolve().parents[2]
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


class ChatModeration(unittest.TestCase):
    def check_request(self, moderation, *, shape=True, param=None, choices=1):
        row = dict(messages=[dict(role='user', content='Hello.')],
                   enable_thinking=False, n=choices, moderation=moderation)
        original = copy.deepcopy(row)
        for schema in (chat_schema(), vision_schema()):
            self.assertEqual(Validator(schema).is_valid(row), shape)
        if param is None:
            py = chat_client.make_request(row['messages'], [], moderation=moderation)
            py = chat_client.with_json_schema(py, dict(type='object'))
            self.assertEqual(py.get('moderation'), moderation)
            script = """
import {makeRequest, withJsonSchema} from './tools/examples/js/chat_client.mjs';
import fs from 'node:fs';
const row = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(withJsonSchema(makeRequest(row.messages,
  {moderation:row.moderation,n:row.n}), {type:'object'})));
"""
            result = subprocess.run(['node', '--input-type=module', '-e', script],
                cwd=ROOT, input=json.dumps(row), text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['moderation'], moderation)
        self.assertEqual(row, original)
        probe = os.environ.get('Q4T_MODERATION_PROBE')
        if not probe:
            return
        envelope = dict(body=dict(request=row, include_prompt=True, include_token_ids=True))
        raw = json.dumps(envelope)
        result = subprocess.run([str(Path(probe).resolve()), str(MODEL/'tokenizer.json')],
            input=raw+'\n', text=True, capture_output=True, timeout=30)
        out = Path(os.environ['Q4T_MODERATION_OUTPUT']) / self._testMethodName
        out.mkdir(parents=True, exist_ok=False)
        (out/'request.jsonl').write_text(raw+'\n')
        (out/'native.jsonl').write_text(result.stdout)
        (out/'native.stderr').write_text(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        if param:
            self.assertEqual(value['error']['param'], param)
            return
        suffix = '<|im_start|>assistant\n<think>\n\n</think>\n\n'
        refusal = ("<|im_start|>system\n# Refusal output\n\n"
            "For a final answer that refuses the user's request, output <q4t_refusal> "
            "followed by a nonempty refusal message and </q4t_refusal>, then stop. "
            "Start this frame at the beginning of the final answer, after </think> "
            "if reasoning is enabled. Do not add normal text, JSON or tool calls "
            "to a refusal. This is an output control frame, not a tool. A refusal "
            "takes precedence over the response schema or required tool choice. "
            "For all other answers, use the normal response format and tool "
            "protocol. Do not use this frame merely to report a tool error, "
            "an empty result or uncertainty.<|im_end|>\n")
        self.assertEqual(value['prompt'], refusal + '<|im_start|>user\nHello.<|im_end|>\n'+suffix)
        from tokenizers import Tokenizer
        expected = Tokenizer.from_file(str(MODEL/'tokenizer.json')).encode(
            value['prompt'], add_special_tokens=False).ids
        self.assertEqual(value['token_ids'], expected)
        if moderation is None:
            self.assertNotIn('moderation', value)
        else:
            self.assertEqual(value['moderation']['auxiliary_output_tokens'], 1024)
            self.assertFalse(value['moderation']['scores_executed'])

    def test_null_keeps_primary_prompt_and_no_review_budget(self):
        self.check_request(None)

    def test_default_score_uses_actual_model(self):
        self.check_request(dict(model='qwen3.8-flash-next'))

    def test_block_output_preserves_multiple_choices(self):
        self.check_request(dict(model='qwen3.8-flash-next', policy=dict(
            input=dict(mode='score'), output=dict(mode='block'))), choices=2)

    def test_nullable_policy_keeps_model_binding(self):
        self.check_request(dict(model='qwen3.8-flash-next', policy=None))

    def test_missing_classifier_model_is_not_ignored(self):
        self.check_request({}, shape=False, param='moderation.model')

    def test_unknown_policy_mode_is_rejected(self):
        self.check_request(dict(model='qwen3.8-flash-next', policy=dict(
            input=dict(mode='skip'))), shape=False, param='moderation.policy.input.mode')

    def test_unknown_nested_override_is_rejected(self):
        self.check_request(dict(model='qwen3.8-flash-next', policy=dict(
            output=dict(mode='block', bypass=True))), shape=False,
            param='moderation.policy.output.bypass')

    def test_json_sse_clients_keep_reports_and_reject_duplicate_terminal(self):
        fixtures = os.environ.get('Q4T_MODERATION_FIXTURES')
        if fixtures:
            value = json.loads((Path(fixtures)/'chat-moderation-output-block.json').read_text())
        else:
            # Public CI has no tokenizer/model; validate the same wire contract.
            error = dict(type='error', code='not_executed', message='No score')
            value = dict(id='chatcmpl_review', object='chat.completion', created=1,
                model='local-model', choices=[dict(index=0, finish_reason='content_filter',
                    logprobs=None, message=dict(role='assistant', content=None))],
                usage=dict(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                moderation=dict(input=error, output=error),
                q4t_moderation_usage=dict(input_tokens=3, output_tokens=1, model_steps=1))
        envelope = {k:value[k] for k in ('id','model','created')}
        envelope['object'] = 'chat.completion.chunk'
        events = []
        for choice in value['choices']:
            events.append(dict(envelope, choices=[dict(index=choice['index'],
                delta=dict(role='assistant'), finish_reason=None)]))
            events.append(dict(envelope, choices=[dict(index=choice['index'],
                delta={}, finish_reason=choice['finish_reason'])]))
        metadata = dict(envelope, choices=[], moderation=value['moderation'],
                        q4t_moderation_usage=value['q4t_moderation_usage'])
        events.extend([metadata, dict(envelope, choices=[], usage=value['usage'])])
        encode = lambda rows: (''.join('data: '+json.dumps(x)+'\r\n\r\n' for x in rows)+
                               'data: [DONE]\r\n\r\n').encode()
        raw, wire = json.dumps(value).encode(), encode(events)
        for result in [chat_client.read_json(io.BytesIO(raw)), chat_client.read_sse(io.BytesIO(wire))]:
            self.assertEqual(result.moderation, value['moderation'])
            self.assertEqual(result.q4t_moderation_usage, value['q4t_moderation_usage'])
            selected = result.select(0) if hasattr(result, 'select') else result
            self.assertEqual(selected.finish_reason, 'content_filter')
            with self.assertRaises(chat_client.ProtocolError):
                chat_client.prepare_tool_calls(selected, {})
        duplicate = encode(events[:-1]+[metadata,events[-1]])
        with self.assertRaises(chat_client.ProtocolError):
            chat_client.read_sse(io.BytesIO(duplicate))
        script = """
import {parseJsonWire,parseSseWire} from './tools/examples/js/chat_client.mjs';
import fs from 'node:fs';
const v=JSON.parse(fs.readFileSync(0,'utf8'));
const results=[parseJsonWire(Buffer.from(v.json)).result,parseSseWire(Buffer.from(v.sse)).result];
let rejected=false;try{parseSseWire(Buffer.from(v.duplicate));}catch{rejected=true;}
process.stdout.write(JSON.stringify({results,rejected}));
"""
        js = subprocess.run(['node','--input-type=module','-e',script],cwd=ROOT,
            input=json.dumps(dict(json=raw.decode(),sse=wire.decode(),duplicate=duplicate.decode())),
            text=True,capture_output=True,timeout=15)
        self.assertEqual(js.returncode,0,js.stderr)
        result=json.loads(js.stdout)
        self.assertTrue(result['rejected'])
        for item in result['results']:
            self.assertEqual(item['moderation'],value['moderation'])
            self.assertEqual(item['q4t_moderation_usage'],value['q4t_moderation_usage'])


if __name__ == '__main__':
    unittest.main()
