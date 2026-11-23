// Exercise the caller-owned template and Files APIs through the native TLS entry.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
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
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({apiKey:key, baseURL:base.href, maxRetries:0, timeout:30000});
const owner = client(plan.key), other = client(plan.other);
const templates = owner.beta.agents.environments.templates;
const peer = other.beta.agents.environments.templates;
const fails = status => error => error instanceof OpenAI.APIError && error.status === status;
const text = 'Project test fixture for the native environment template.';
const source = await owner.files.create({
  file:new File([text], 'project-record.txt', {type:'text/plain'}), purpose:'user_data',
});
const secret = randomBytes(24).toString('hex');
const inline = Buffer.from(text).toString('base64');
const file = {type:'file_id',path:'/workspace/project-record.txt',file_id:source.id};
const result = await templates.create({
  name:'Project documents',env:{PROJECT_TOKEN:secret},
  setup_commands:[{command:'echo '+secret}],
  files:[file,{type:'inline',path:'/workspace/inline.txt',data:inline}],
  network:{access:'restricted',allowed_domains:['example.org']},
});
assert.deepEqual(result.files, [file,{type:'inline',path:'/workspace/inline.txt',
                                     size_bytes:Buffer.byteLength(text)}]);
const safe = row => {
  const raw = JSON.stringify(row);
  for (const hidden of [secret,inline,'setup_commands','PROJECT_TOKEN']) assert.ok(!raw.includes(hidden));
};
safe(result);
const defaults = await templates.create();
assert.equal(defaults.network.access,'disabled');
const ids = [];
for await (const row of templates.list({limit:1, order:'asc'})) { safe(row); ids.push(row.id); }
assert.deepEqual(ids.sort(),[defaults.id,result.id].sort());
assert.deepEqual((await peer.list()).data,[]);
await assert.rejects(peer.retrieve(result.id),fails(404));
await assert.rejects(peer.create({files:[file]}),fails(404));
const patch = await templates.update(result.id,{desktop:{enabled:true},name:'Patched project'});
assert.equal(patch.desktop.enabled,true);
assert.deepEqual(patch.files,result.files);
safe(await templates.retrieve(result.id));
await assert.rejects(templates.update(result.id,{files:[{type:'inline',path:'/outside',data:inline}]}),fails(400));
assert.deepEqual(await templates.retrieve(result.id),patch);
const cleared = await templates.update(result.id,{files:null,env:null,setup_commands:null,network:null});
assert.equal(cleared.network.access,'disabled');
assert.deepEqual(cleared.files,[]);
await assert.rejects(peer.delete(result.id),fails(404));
for (const id of ids) assert.equal((await templates.delete(id)).deleted,true);
await assert.rejects(templates.retrieve(result.id),fails(404));
assert.equal((await owner.files.delete(source.id)).deleted,true);
const chat = await owner.chat.completions.create({model:'qwen3.8-flash-next',
  messages:[{role:'user',content:'What is six times seven? Respond with only the number.'}],
  temperature:0,max_tokens:24,chat_template_kwargs:{enable_thinking:false}});
assert.equal(chat.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,operations:5,templates:2,pages:2,
  inline_bytes:Buffer.byteLength(text),owned_file:true,foreign_file:404,private_fields_omitted:true,
  foreign_read:404,foreign_delete:404,deleted_get:404,invalid_update:400,chat:'42'})+'\n');
