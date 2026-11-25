// Authenticated direct and two-phase image uploads followed by source-free retry.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {appendFileSync, readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({baseURL: plan.base, apiKey: key, maxRetries: 0});
const project = client(plan.key);
const foreign = client(plan.other);
const agent = await project.beta.agents.create({
  model: plan.model,
  instructions: 'Answer visual questions using the attached picture. Return exactly one color word.',
  reasoning: {effort: 'none'},
});
const createSession = user => project.beta.chatkit.sessions.create({
  user, workflow: {id: agent.id}, rate_limits: {max_requests_per_1_minute: 50},
  chatkit_configuration: {file_upload: {enabled: true, max_files: 2, max_file_size: 1}},
});
const owner = await createSession('visual-reviewer');
const sibling = await createSession('another-reviewer');

async function rpc(session, type, params, status = 200, stream = false) {
  const response = await fetch(plan.base + '/chatkit', {
    method: 'POST', headers: {Authorization: 'Bearer ' + session.client_secret, 'Content-Type': 'application/json'},
    body: JSON.stringify({type, params}), signal: AbortSignal.timeout(75000),
  });
  if (response.status !== status) throw new Error(type + ' ' + response.status + ' ' + (await response.text()).slice(0, 360));
  if (!stream) return response.json();
  const events = (await response.text()).split(/\r?\n/).filter(x => x.startsWith('data: ')).map(x => JSON.parse(x.slice(6)));
  const failed = events.find(x => x.type === 'error');
  assert.ok(!failed, JSON.stringify(failed));
  return events;
}

async function upload(fixture, bytes, mode) {
  const name = 'sample.' + fixture.extension;
  if (mode === 'two_phase') {
    const pending = await rpc(owner, 'attachments.create', {name, size: bytes.length, mime_type: fixture.mime});
    const descriptor = pending.upload_descriptor;
    assert.equal(new URL(descriptor.url).origin, new URL(plan.base).origin);
    const sent = await fetch(descriptor.url, {
      method: descriptor.method, headers: descriptor.headers, body: bytes,
      redirect: 'error', signal: AbortSignal.timeout(15000),
    });
    assert.equal(sent.status, 200);
    const result = await sent.json();
    assert.equal(result.id, pending.id);
    return result;
  }
  const form = new FormData();
  form.append('file', new File([bytes], name, {type: fixture.mime}));
  const sent = await fetch(plan.base + '/chatkit/attachments', {
    method: 'POST', headers: {Authorization: 'Bearer ' + owner.client_secret},
    body: form, signal: AbortSignal.timeout(15000),
  });
  assert.equal(sent.status, 200);
  return sent.json();
}

const answer = events => {
  const messages = events.filter(x => x.type === 'thread.item.done' && x.item.type === 'assistant_message');
  assert.equal(messages.length, 1);
  return messages[0].item.content.map(x => x.text).join('').trim();
};
function observation(row) {
  if (plan.observations) appendFileSync(plan.observations, JSON.stringify(row) + '\n', {mode: 0o600});
}
async function mathControl(text, phase) {
  const res = await project.chat.completions.create({model: plan.model, temperature: 0, max_tokens: 16,
    messages: [{role: 'user', content: text}], chat_template_kwargs: {enable_thinking: false}});
  const value = res.choices[0].message.content?.trim() ?? '';
  observation({phase, text, value, refusal: res.choices[0].message.refusal ?? null});
  return value;
}
const originalMath = 'Reply with only the integer 6 times 7.';
const previousMath = 'Return only the integer 6 times 7.';
const controls = {};
if (plan.paired_controls) {
  controls.before = [await mathControl(originalMath, 'original_before'),
    await mathControl(previousMath, 'previous_before')];
}
const rows = [];
for (const fixture of plan.fixtures) {
  const bytes = readFileSync(fixture.path);
  const file = await upload(fixture, bytes, fixture.mode);
  const digest = value => createHash('sha256').update(value).digest('hex');
  assert.equal(digest(Buffer.from(await (await project.files.content(file.id)).arrayBuffer())), digest(bytes));
  await assert.rejects(foreign.files.retrieve(file.id), err => err.status === 404);
  if (plan.compare && rows.length === 0) {
    const instructions = 'Answer visual questions using the attached picture. Return exactly one color word.';
    const question = 'What color fills the image? Reply with just the color in English.';
    const message = {role: 'user', content: [{type: 'input_image', file_id: file.id, detail: 'auto'},
      {type: 'input_text', text: question}]};
    const direct = await project.responses.create({model: plan.model, input: [message], instructions,
      temperature: 0, reasoning: {effort: 'none'}, max_output_tokens: 64, store: true});
    const content = direct.output.filter(o => o.type === 'message').flatMap(o => o.content)
      .map(part => ({type: part.type, answer: part.text ?? part.refusal}));
    observation({phase: 'responses_control', response: direct.id, content, status: direct.status});
    const control = await project.chat.completions.create({model: plan.model, temperature: 0, max_tokens: 64,
      chat_template_kwargs: {enable_thinking: false}, messages: [{role: 'system', content: instructions},
        {role: 'user', content: [{type: 'image_url', image_url: {url: 'data:' + fixture.mime + ';base64,' + bytes.toString('base64')}},
          {type: 'text', text: question}]}]});
    const result = control.choices[0].message;
    observation({phase: 'chat_control', answer: result.content, refusal: result.refusal ?? null});
  }
  const user = {attachments: [file.id], inference_options: {},
    content: [{type: 'input_text', text: 'What color fills the image? Reply with just the color in English.'}]};
  await rpc(sibling, 'threads.create', {input: user}, 404);
  const created = await rpc(owner, 'threads.create', {input: user}, 200, true);
  const thread = created.find(x => x.type === 'thread.created').thread.id;
  const first = answer(created);
  observation({phase: 'first', thread, file: file.id, answer: first, expected: fixture.expected});

  const pageResponse = await project.beta.chatkit.threads.listItems(thread, {order: 'asc'}).asResponse();
  assert.equal(pageResponse.status, 200);
  const page = await pageResponse.json();
  const browserPage = await rpc(owner, 'items.list', {thread_id: thread, order: 'asc'});
  const browserUser = browserPage.data.find(x => x.type === 'user_message');
  assert.equal(browserUser.attachments[0].id, file.id);
  assert.equal(browserUser.attachments[0].mime_type, fixture.mime);
  const original = page.data.find(x => x.type === 'chatkit.user_message');
  assert.equal(original.attachments[0].id, file.id);
  await rpc(owner, 'attachments.delete', {attachment_id: file.id});
  await assert.rejects(project.files.retrieve(file.id), err => err.status === 404);
  const renewed = await createSession('visual-reviewer');
  const replay = await rpc(renewed, 'threads.retry_after_item', {thread_id: thread, item_id: original.id}, 200, true);
  const again = answer(replay);
  observation({phase: 'retry', thread, file: file.id, answer: again, expected: fixture.expected});

  const restored = await project.beta.chatkit.threads.listItems(thread, {order: 'asc'});
  assert.equal(restored.data.find(x => x.type === 'chatkit.user_message').id, original.id);
  rows.push({wire: {upload: file, browser: browserPage, management: page},
    mode: fixture.mode, mime: fixture.mime, sha256: digest(bytes),
    thread, file: file.id, item: original.id, first, retry: again, events: [created.length, replay.length],
    source_deleted: 404, other_key: 404, other_user: 404});
}
const independent = await mathControl(originalMath, 'original_after');
if (plan.paired_controls) {
  controls.after = [independent, await mathControl(previousMath, 'previous_after')];
}
console.log(JSON.stringify({sdk: version, rows, controls, independent_chat: independent}));
