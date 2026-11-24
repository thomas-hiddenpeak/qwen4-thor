// Exercise project retrieval with the actual reviewed record and model request.
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const piece of process.stdin) input += piece;
const options = JSON.parse(input);
const {OpenAI, version} = loadSdk(options.sdk);
const connect = apiKey => new OpenAI({baseURL: options.base, apiKey, maxRetries: 0, timeout: 45000});
const project = connect(options.key), peer = connect(options.other);
const query = await project.responses.create({model: options.model, input: 'Return only 8 times 9.',
  store: true, reasoning: {effort: 'none'}, temperature: 0, max_output_tokens: 48}).withResponse();
assert.equal(query.data.output_text.trim(), '72');
const requestId = query.response.headers.get('x-request-id');
assert.ok(requestId);
const common = {created_at: Math.floor(Date.now()/1000)};
const alert = {...common, object: 'safety.alert', id: 'alert_' + randomUUID().replaceAll('-', ''),
  request_id: requestId, response_id: query.data.id, model: query.data.model, request_paused: false,
  error_type: 'other', reason: 'Operator-reviewed service acceptance exercise.'};
const review = {...common, object: 'safety.case', id: 'C-' + randomUUID(), reason: null,
  entity_identifier: 'q4t-business-acceptance', notice: {type: 'warning'}};
function publish(resource) {
  // Executable, object directory and project are inputs to the local operator harness.
  const result = spawnSync(options.engine,
    ['safety', 'publish', '--object-store-dir', options.objects, '--client-id', 'alpha'],
    {input: JSON.stringify({resource}), timeout: 10000, encoding: 'utf8'});
  assert.equal(result.status, 0, result.stdout + result.stderr);
  return JSON.parse(result.stdout).value;
}
const wire = [];
for (const [path, record, api, foreign] of [
  ['/safety/alerts/{id}', alert, project.safety.alerts, peer.safety.alerts],
  ['/safety/cases/{id}', review, project.safety.cases, peer.safety.cases]]) {
  assert.deepEqual(publish(record), publish(record));
  const reply = await api.retrieve(record.id).withResponse();
  assert.equal(reply.response.status, 200);
  assert.deepEqual(reply.data, record);
  await assert.rejects(foreign.retrieve(record.id), e => e.status === 404);
  wire.push({path, method: 'get', status: 200, value: reply.data});
}
await assert.rejects(project.safety.alerts.retrieve('x'.repeat(39)), e => e.status === 400);
await assert.rejects(project.safety.cases.retrieve('x'.repeat(129)), e => e.status === 400);
const independent = await peer.chat.completions.create({model: options.model,
  reasoning_effort: 'none', temperature: 0, max_tokens: 32,
  messages: [{role: 'user', content: 'Return only 6 times 7.'}]});
assert.equal(independent.choices[0].message.content.trim(), '42');
process.stdout.write(JSON.stringify({sdk: version, independent_chat: '42', answer: '72',
  approved_alert: alert.id, reviewed_case: review.id, approved_request: requestId,
  source_response: query.data.id, foreign_status: 404, excessive_id_status: 400, wire}));
