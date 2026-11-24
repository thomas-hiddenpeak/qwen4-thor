// Exercise persisted messages and real file_search indexing through native TLS.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const wire = [];
const client = apiKey => new OpenAI({baseURL:base.href, apiKey, maxRetries:0, timeout:60000,
  fetch:async (input, init) => {
    const res = await fetch(input, init);
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (res.ok && url.pathname.startsWith('/v1/threads')) {
      wire.push({path:url.pathname,method:init?.method ?? input.method ?? 'GET',
        value:await res.clone().json()});
    }
    return res;
  }});
const owner = client(plan.key), other = client(plan.other);
const api = owner.beta.threads, messages = api.messages;
const capabilities = await owner.get('/capabilities');
assert.equal(capabilities.assistants.threads,'/v1/threads');
assert.equal(capabilities.assistants.messages,'/v1/threads/{thread_id}/messages');
const error = status => e => e instanceof OpenAI.APIError && e.status === status;
const evidence = readFileSync(plan.source);
assert.equal(createHash('sha256').update(evidence).digest('hex'), plan.source_sha256);
const revision = JSON.parse(evidence).publication.publication_revision;
const excerpt = /^  "publication": \{[\s\S]+?^  \},/m.exec(evidence.toString())[0];
const upload = async (text, name) => owner.files.create({
  purpose:'assistants', file:new File([text], name, {type:'text/plain'})});
const release = await upload(excerpt, 'release.txt');
const followup = await upload('Operating intent of the Q4T published service is STOPPED.', 'intent.txt');
const evidenceTools = [{type:'file_search'},{type:'code_interpreter'}];
await assert.rejects(owner.post('/threads', {body:{}}), error(400));
const thread = await api.create({metadata:{project:'q4t'}, messages:[{
  role:'user',content:[{type:'text',text:'Find the project release revision.'}],
  attachments:[{file_id:release.id, tools:evidenceTools}]}]});
assert.equal(thread.object,'thread');
assert.deepEqual(thread.tool_resources.code_interpreter.file_ids,[release.id]);
const [vs] = thread.tool_resources.file_search.vector_store_ids;
assert.equal((await owner.vectorStores.retrieve(vs)).expires_after.days,7);
const first = (await messages.list(thread.id)).data[0];
assert.deepEqual(first.content[0],{type:'text',text:{value:'Find the project release revision.',annotations:[]}});
assert.equal(first.status,'completed');
assert.equal(first.thread_id,thread.id);
assert.equal(first.run_id,null);
const second = await messages.create(thread.id,{role:'user',content:'Find the runtime intent.',
  attachments:[{file_id:followup.id,tools:evidenceTools}]});
const third = await messages.create(thread.id,{role:'assistant',content:'Resources attached.'});
const path = {thread_id:thread.id};
assert.equal((await messages.update(third.id,{...path,metadata:{proof:'revision'}})).metadata.proof,'revision');
assert.equal((await messages.retrieve(third.id,path)).content[0].text.value,'Resources attached.');
const start = await messages.list(thread.id,{order:'asc',limit:1});
const next = await start.getNextPage();
assert.equal(start.data[0].id,first.id);
assert.equal(next.data[0].id,second.id);
const previous = await messages.list(thread.id,{before:third.id,order:'asc',limit:1});
assert.equal(previous.data[0].id,second.id);
assert.equal(wire.at(-1).value.first_id,second.id);
assert.equal((await messages.list(thread.id,{run_id:'run_unknown'})).data.length,0);
const updated = await api.update(thread.id,{metadata:{project:'q4t',stage:'verified'}});
assert.deepEqual(updated.tool_resources.code_interpreter.file_ids,[release.id,followup.id]);
assert.equal((await api.retrieve(thread.id)).metadata.stage,'verified');
const empty = await api.create();
assert.equal((await messages.list(empty.id)).data.length,0);
await assert.rejects(messages.retrieve(first.id,{thread_id:empty.id}),error(404));
await assert.rejects(other.beta.threads.retrieve(thread.id),error(404));
await assert.rejects(other.beta.threads.messages.retrieve(first.id,path),error(404));
await assert.rejects(other.beta.threads.create({messages:[{role:'user',content:'x',
  attachments:[{file_id:release.id,tools:evidenceTools}]}]}),error(404));
for (const f of [release,followup]) {
  let state;
  for (let attempt=0; attempt<30; ++attempt) {
    state = await owner.vectorStores.files.retrieve(f.id,{vector_store_id:vs});
    if (state.status !== 'in_progress') break;
    await delay(600);
  }
  assert.equal(state.status,'completed',JSON.stringify(state.last_error));
}
const hits = await owner.vectorStores.search(vs,{query:'publication_revision',max_num_results:2});
assert.ok(hits.data.some(r => r.file_id===release.id && r.content.some(c=>c.text.includes(String(revision)))));
const notes = await owner.vectorStores.search(vs,{query:'Q4T operating intent',max_num_results:2});
assert.ok(notes.data.some(r => r.file_id===followup.id && r.content.some(c=>c.text.includes('STOPPED'))));
assert.equal((await messages.delete(third.id,path)).object,'thread.message.deleted');
await assert.rejects(messages.retrieve(third.id,path),error(404));
for (const id of [thread.id,empty.id]) assert.equal((await api.delete(id)).object,'thread.deleted');
await assert.rejects(messages.retrieve(first.id,path),error(404));
await owner.vectorStores.delete(vs);
await owner.files.delete(release.id);
await owner.files.delete(followup.id);
const chat = await owner.chat.completions.create({model:'qwen3.8-flash-next',temperature:0,
  max_tokens:16,reasoning_effort:'none',
  messages:[{role:'user',content:'What is 6 times 7? Reply only with the number.'}]});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,thread_methods:4,message_methods:5,wire,
  indexed_files:2,retrieved_revision:revision,retrieved_intent:'STOPPED',
  source_sha256:plan.source_sha256,bidirectional_pagination:true,
  implicit_store_expiry_days:7,foreign_statuses:[404,404,404],wrong_parent:404,
  deleted_descendant:404,independent_chat:independent})+'\n');
