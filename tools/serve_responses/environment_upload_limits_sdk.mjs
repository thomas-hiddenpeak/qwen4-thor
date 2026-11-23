// Check decoded byte limits through all public hosted file admission paths.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const owner = new OpenAI({apiKey:plan.key, baseURL:base.href, maxRetries:0, timeout:60000});
const {environments, sessions} = owner.beta.agents;
const limit = 5 * 1024 * 1024;
const at = Buffer.alloc(limit, 0x71).toString('base64');
const over = Buffer.alloc(limit+1, 0x71).toString('base64');
assert.equal(at.length, over.length);
const file = (name, data) => ({type:'inline',path:'/workspace/'+name,data});
const valid = [file('one',at),file('two',at)];
const tooManyBytes = [...valid,file('three','YQ==')];
const bad = param => err => err instanceof OpenAI.APIError && err.status === 400 &&
                            err.error.param === param;
const {templates} = environments;
await assert.rejects(templates.create({files:[file('one',over)]}),bad('files[0].data'));
const template = await templates.create({files:valid});
assert.deepEqual(template.files.map(x=>x.size_bytes),[limit,limit]);
await assert.rejects(templates.update(template.id,{files:tooManyBytes}),bad('files[2].data'));
assert.deepEqual((await templates.retrieve(template.id)).files,template.files);
const environment = {type:'openai_hosted',files:tooManyBytes};
await assert.rejects(environments.create({environment}),bad('environment.files[2].data'));
const agent = {model:'qwen3.8-flash-next'};
await assert.rejects(sessions.create({agent,environment}),bad('environment.files[2].data'));
const session = await sessions.create({agent,environment:{type:'openai_hosted'}});
const id = session.environment.id;
await assert.rejects(environments.files.create(id,file('one',over)),bad('data'));
assert.equal((await environments.files.list(id)).data.length,0);
assert.equal((await sessions.delete(session.id)).deleted,true);
assert.equal((await templates.delete(template.id)).deleted,true);
process.stdout.write(JSON.stringify({sdk:version,file_limit:limit,total_limit:2*limit,
  identical_encoded_lengths:true,create:400,update:400,warm:400,session:400,install:400,
  existing_files_unchanged:true})+'\n');
