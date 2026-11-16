// Actual native CPU wire, consumed by the pinned official and project clients.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';
import { parseJsonWire, parseSseWire } from '../examples/js/chat_client.mjs';

const root = resolve(process.env.Q4T_WEB_API_OUTPUT);
const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const bytes = name => readFileSync(resolve(root, name));
const json = name => JSON.parse(bytes(name));
function client(name, stream, api) {
  return new OpenAI({ apiKey: 'cpu', baseURL: 'http://cpu.invalid/v1', maxRetries: 0,
    fetch: async (url, init) => {
      assert.equal(new URL(url).pathname, '/v1/' + api);
      const body = JSON.parse(init.body);
      assert.equal(body.stream ?? false, stream);
      if (api === 'responses') assert.deepEqual(body.tools, [{type:'web_search',external_web_access:false}]);
      else assert.deepEqual(body.web_search_options, {});
      return new Response(bytes(name + (stream ? '.wire' : '.json')), {status:200,
        headers:{'content-type':stream ? 'text/event-stream':'application/json'}});
    }});
}

test('official Responses JSON/SSE retains calls, citations, sources and sequence', async () => {
  for (const name of ['response', 'response-included']) {
    const request = {model:'cpu',input:'Find alpha',tools:[{type:'web_search',external_web_access:false}]};
    const expected = json(name + '.json');
    const finite = await client(name, false, 'responses').responses.create(request);
    assert.deepEqual(finite.output, expected.output);
    const stream = client(name, true, 'responses').responses.stream(request);
    const events = [];
    for await (const event of stream) events.push(event);
    const result = await stream.finalResponse();
    assert.deepEqual(result.output[0], expected.output[0]);
    assert.deepEqual(result.output[1].content[0].annotations, expected.output[1].content[0].annotations);
    assert.equal(result.output_text, expected.output[1].content[0].text);
    events.forEach((event, i) => assert.equal(event.sequence_number, i));
  }
});

test('official and project Chat JSON/SSE retain source citations and reject bad offsets', async () => {
  const request = {model:'cpu',messages:[{role:'user',content:'Find alpha'}],web_search_options:{}};
  const expected = json('chat.json');
  const finite = await client('chat', false, 'chat/completions').chat.completions.create(request);
  assert.deepEqual(finite.choices[0].message, expected.choices[0].message);
  const result = await client('chat', true, 'chat/completions').chat.completions.stream(request).finalChatCompletion();
  assert.deepEqual(result.choices[0].message.annotations, expected.choices[0].message.annotations);
  const a = parseJsonWire(bytes('chat.json')).result, b = parseSseWire(bytes('chat.wire')).result;
  assert.deepEqual(a.message, b.message);
  expected.choices[0].message.annotations[0].url_citation.end_index = 100000;
  assert.throws(() => parseJsonWire(Buffer.from(JSON.stringify(expected))), /offsets/);
  expected.choices[0].message.content = '中文🙂 source';
  Object.assign(expected.choices[0].message.annotations[0].url_citation, {start_index:4,end_index:10});
  assert.equal(parseJsonWire(Buffer.from(JSON.stringify(expected))).result.message.content, '中文🙂 source');
  expected.choices[0].message.annotations[0].url_citation.end_index = 11;
  assert.throws(() => parseJsonWire(Buffer.from(JSON.stringify(expected))), /offsets/);
});
