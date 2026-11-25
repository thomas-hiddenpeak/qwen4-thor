// Exercise the public stored-message projection with the owner's actual model Chat.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const connect = apiKey => new OpenAI({baseURL: plan.base, apiKey, maxRetries: 0, timeout: 45000});
const owner = connect(plan.key);
const peer = connect(plan.other);
const samples = [
  {part: '未授权修改项目资料。'},
  {part: '原始拒答片段。', field: '单独记录的拒答说明。'},
];
const cases = [];
for (const [i, sample] of samples.entries()) {
  const parts = [{type: 'refusal', refusal: sample.part}];
  const refusal = {role: 'assistant', content: parts};
  if (sample.field) refusal.refusal = sample.field;
  let id = plan.previous?.cases[i].id;
  if (!id) {
    const result = await owner.chat.completions.create({
      model: plan.model, store: true, temperature: 0, reasoning_effort: 'none',
      max_completion_tokens: 32, messages: [
        {role: 'user', content: 'May I change the project record?'},
        refusal,
        {role: 'user', content: 'Independent arithmetic question: compute 17 + 25. Reply only with the number.'},
      ],
    });
    assert.equal(result.choices[0].finish_reason, 'stop');
    assert.equal(result.choices[0].message.content.trim(), '42');
    id = result.id;
  }
  const list = params => owner.chat.completions.messages.list(id, params);
  const page = await list({order: 'asc'});
  assert.deepEqual(page.data.map(row => row.role), ['user', 'assistant', 'user']);
  const row = page.data[1];
  assert.equal(row.content, null);
  assert.equal(row.content_parts, null);
  assert.equal(row.refusal, sample.field ?? sample.part);
  assert.deepEqual(row.original_content_parts, parts);
  const reverse = await list({order: 'desc', limit: 1, after: page.last_id});
  assert.ok(reverse.has_more);
  assert.deepEqual(reverse.data, [row]);
  await assert.rejects(peer.chat.completions.messages.list(id), err => err.status === 404);
  cases.push({id, message_id: row.id, refusal: row.refusal, original_content_parts: parts,
              last_id: page.last_id, peer: 404, recovered: Boolean(plan.previous)});
}
process.stdout.write(JSON.stringify({sdk: version, cases}));
