// Replay both the public raw stream and the SDK's accumulated failure.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {loadSdk} from '../serve_protocol/sdk.mjs';
const {OpenAI, version} = loadSdk(process.argv[2]);
const evidence = JSON.parse(readFileSync(new URL('../../docs/evidence/serve-responses-errors-20261126/cpu-captures.json', import.meta.url)));
const saved = JSON.parse(evidence.response_json);
const calls = [];
const client = new OpenAI({
  apiKey: 'unused-replay', baseURL: 'http://unit.invalid/v1', maxRetries: 0,
  fetch: async (input, init) => {
    const request = new Request(input, init);
    const {pathname, searchParams} = new URL(request.url);
    calls.push([request.method, pathname]);
    if (request.method === 'POST' && pathname === '/v1/responses') {
      assert.equal((await request.json()).stream, true);
      return new Response(evidence.sse, {headers:{'content-type':'text/event-stream'}});
    }
    assert.equal(request.method, 'GET');
    assert.equal(pathname, '/v1/responses/' + saved.id);
    if (searchParams.get('stream') === 'true') {
      assert.equal(searchParams.has('starting_after'), false);
      return new Response(evidence.sse, {headers:{'content-type':'text/event-stream'}});
    }
    return new Response(evidence.response_json, {headers:{'content-type':'application/json'}});
  },
});
const params = {model:'qwen3.8-flash-next', input:'replay'};
const raw = [];
for await (const event of await client.responses.create({...params, stream:true})) raw.push(event);
const observed = raw.map(event => event.type);
assert.deepEqual(observed, ['response.created','response.in_progress','response.output_item.added',
  'response.function_call_arguments.delta','response.failed']);
assert.deepEqual(raw.map(event=>event.sequence_number), [0,1,2,3,4]);
const stream = client.responses.stream(params);
const buffered = [];
stream.on('event', event => buffered.push(event.type));
const accumulated = await stream.finalResponse();
assert.deepEqual(buffered, observed);
const fetched = await client.responses.retrieve(saved.id);
for (const response of [raw.at(-1).response, accumulated, fetched]) {
  assert.equal(response.id, saved.id);
  assert.equal(response.status, 'failed');
  assert.deepEqual(response.error, saved.error);
  assert.equal(response.error.code,'server_error');
  assert.deepEqual(response.output.map(({id,type,status,call_id,name,arguments:args}) =>
    ({id,type,status,call_id,name,args})), saved.output.map(({id,type,status,call_id,name,arguments:args}) =>
    ({id,type,status,call_id,name,args})));
  assert.equal(response.output[0].status, 'incomplete');
  assert.equal(response.output[0].arguments,'{');
}
const cursors = [];
for (const cursor of [3, 4]) {
  const resumed = client.responses.stream({response_id:saved.id, starting_after:cursor});
  const delivered = [];
  resumed.on('event', e => delivered.push(e.sequence_number));
  const final = await resumed.finalResponse();
  assert.deepEqual(delivered, cursor === 3 ? [4] : []);
  assert.deepEqual(final.error, saved.error);
  assert.deepEqual(final.output, accumulated.output);
  cursors.push({cursor, delivered, final_status:final.status});
}
assert.deepEqual(calls,[['POST','/v1/responses'],['POST','/v1/responses'],
  ...Array.from({length:3},()=>['GET','/v1/responses/'+saved.id])]);
console.log(JSON.stringify({sdk:version,events:observed, sequence:raw.map(e=>e.sequence_number),
  resource:saved.id, error:saved.error, status:accumulated.status, unfinished:accumulated.output[0].status,
  requests:calls, resumed:cursors, transport:'sealed native CPU capture replay', sse_sha256:createHash('sha256').update(evidence.sse).digest('hex')},null,2));
