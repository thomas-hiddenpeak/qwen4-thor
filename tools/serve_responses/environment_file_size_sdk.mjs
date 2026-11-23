// Real TLS acceptance for a maximum-size inline file in a hosted template.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {loadSdk} from '../serve_protocol/sdk.mjs';

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
const client = key => new OpenAI({apiKey:key, baseURL:base.href, maxRetries:0, timeout:60000});
const owner = client(plan.key), peer = client(plan.other);
const templates = owner.beta.agents.environments.templates;
const bytes = Buffer.alloc(5 * 1024 * 1024);
for (let i = 0; i < bytes.length; ++i) bytes[i] = (i * 31 + (i >>> 8)) & 255;
const path = '/workspace/records.bin';
const file = {type:'inline',path,data:bytes.toString('base64')};
const result = await templates.create({files:[file]}).withResponse();
assert.equal(result.response.status,201);
const {id,files} = result.data;
assert.deepEqual(files,[{type:'inline',path,size_bytes:bytes.length}]);
assert.equal(JSON.stringify(result.data).includes(file.data.slice(0,100)),false);
await assert.rejects(peer.beta.agents.environments.templates.retrieve(id),
  e => e instanceof OpenAI.APIError && e.status === 404);
assert.deepEqual((await templates.retrieve(id)).files,files);
const patched = await templates.update(id,{name:'retained binary'}).withResponse();
assert.equal(patched.response.status,200);
assert.deepEqual(patched.data.files,files);
const invalid = {...file,data:file.data+'AAAA'};
await assert.rejects(templates.update(id,{files:[invalid]}),
  e => e instanceof OpenAI.APIError && e.status === 400);
assert.deepEqual(await templates.retrieve(id),patched.data);
assert.deepEqual((await templates.update(id,{files:null})).files,[]);
assert.equal((await templates.delete(id)).deleted,true);
const chat = await owner.chat.completions.create({model:'qwen3.8-flash-next',
  messages:[{role:'user',content:'What is six times seven? Respond with only the number.'}],
  temperature:0,max_tokens:24,chat_template_kwargs:{enable_thinking:false}});
assert.equal(chat.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,size_bytes:bytes.length,encoded_bytes:file.data.length,
  source_sha256:createHash('sha256').update(bytes).digest('hex'),created:201,patched:200,
  foreign:404,over_limit:400,old_files_retained:true,cleared:true,deleted:true,chat:'42'})+'\n');
