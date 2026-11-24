// Real official SDK v2 header, ownership, durable tool settings and vector binding.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({ baseURL:base.href, apiKey:key, maxRetries:0, timeout:60000 });
const owner = client(plan.key), other = client(plan.other);
const api = owner.beta.assistants;
const error = status => e => e instanceof OpenAI.APIError && e.status === status;
const model = 'qwen3.8-flash-next';
await assert.rejects(owner.get('/assistants'), error(400));
const evidence = readFileSync(plan.source);
assert.equal(createHash('sha256').update(evidence).digest('hex'), plan.source_sha256);
const publication = JSON.parse(evidence).publication;
const excerpt = /^  "publication": \{[\s\S]+?^  \},/m.exec(evidence.toString())[0];
const file = await owner.files.create({file:new File([excerpt], 'project.txt', {type:'text/plain'}),
  purpose:'assistants'});
const cfg = { model, name:'q4t-project-records', instructions:'Look up the project release.',
  metadata:{project:'q4t'}, reasoning_effort:'low', temperature:0,
  tools:[{type:'function',function:{name:'read_release',parameters:{type:'object',properties:{}}}},
         {type:'file_search'}, {type:'code_interpreter'}],
  tool_resources:{file_search:{vector_stores:[{file_ids:[file.id],metadata:{role:'records'}}]},
                  code_interpreter:{file_ids:[file.id]}} };
const created = await api.create(cfg);
assert.equal(created.object,'assistant');
assert.ok(created.id.startsWith('asst_'));
assert.deepEqual(created.tool_resources.code_interpreter.file_ids,[file.id]);
const [vs] = created.tool_resources.file_search.vector_store_ids;
assert.ok(vs.startsWith('vs_'));
assert.equal((await owner.vectorStores.retrieve(vs)).metadata.role,'records');
let state;
for (let i=0; i<25; ++i) {
  state = await owner.vectorStores.files.retrieve(file.id, {vector_store_id:vs});
  if (state.status !== 'in_progress') break;
  await delay(800);
}
assert.equal(state.status, 'completed', JSON.stringify(state.last_error));
const search = await owner.vectorStores.search(vs, {query:'publication_revision',max_num_results:1});
assert.equal(search.data[0].file_id, file.id);
assert.ok(search.data[0].content.some(c => c.text.includes('"publication_revision": '+publication.publication_revision)));
assert.deepEqual((await api.retrieve(created.id)).tools,cfg.tools);
await assert.rejects(other.beta.assistants.retrieve(created.id),error(404));
await assert.rejects(other.beta.assistants.create({model,tool_resources:{
  file_search:{vector_store_ids:[vs]}}}),error(404));
await assert.rejects(other.beta.assistants.create({model,tool_resources:{
  code_interpreter:{file_ids:[file.id]}}}),error(404));
assert.equal((await other.beta.assistants.list()).data.length,0);
await assert.rejects(api.create({model:'missing-model'}),error(404));
await assert.rejects(api.update(created.id,{temperature:2.01}),error(400));
await assert.rejects(api.update(created.id,{name:'业'.repeat(257)}),error(400));
const updated = await api.update(created.id,{name:'项目资料',instructions:null,
  response_format:{type:'json_object'},top_p:0.6,tool_resources:{file_search:{vector_store_ids:[vs]}}});
assert.equal(updated.created_at,created.created_at);
assert.equal(updated.name,'项目资料');
assert.equal(updated.instructions,null);
assert.deepEqual(updated.metadata,created.metadata);
const second = await api.create({model,name:'second'});
const firstPage = await api.list({limit:1,order:'asc'});
assert.equal(firstPage.has_more,true);
const next = await firstPage.getNextPage();
assert.deepEqual(new Set([firstPage.data[0].id,next.data[0].id]),new Set([created.id,second.id]));
const before = await api.list({before:next.data[0].id,order:'asc',limit:1});
assert.equal(before.data[0].id,firstPage.data[0].id);
for (const id of [created.id,second.id]) assert.equal((await api.delete(id)).deleted,true);
await assert.rejects(api.retrieve(created.id),error(404));
assert.equal((await owner.vectorStores.retrieve(vs)).id,vs);
await owner.vectorStores.delete(vs);
await owner.files.delete(file.id);
const chat = await owner.chat.completions.create({model,max_tokens:20,temperature:0,reasoning_effort:'none',
  messages:[{role:'user',content:'What is 6 times 7? Reply only with the number.'}]});
const answer = chat.choices[0].message.content.trim();
assert.equal(answer,'42');
process.stdout.write(JSON.stringify({sdk:version,v2_header_required:400,created:2,deleted:2,
  persisted_tools:3,indexed_file:state.status,retrieved_revision:publication.publication_revision,source_sha256:plan.source_sha256,inline_store_visible:true,file_bound:true,foreign_statuses:[404,404,404],
  bidirectional_pagination:true,invalid_model:404,invalid_updates:[400,400],independent_chat:answer})+'\n');
