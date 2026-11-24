// Leave the generated file and its Response for a second service generation.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 32768);
}
const plan = JSON.parse(input);
const origin = new URL(plan.base);
assert.equal(origin.protocol, 'https:');
assert.equal(origin.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = apiKey => new OpenAI({apiKey, baseURL: origin.href, maxRetries: 0, timeout: 75000});
const owner = client(plan.key);
const other = client(plan.other);
const bytes = readFileSync(plan.source);
assert.equal(createHash('sha256').update(bytes).digest('hex'), plan.source_sha256);
const revision = JSON.parse(bytes).publication.publication_revision;
const create = plan.phase === 'create';
assert.ok(create || plan.phase === 'resume');
const include = ['code_interpreter_call.outputs'];
const expected = String(create ? revision : revision + 1);
const hidden = error => error instanceof OpenAI.APIError && error.status === 404;
let container, code, old;
if (create) {
  const source = await owner.files.create({purpose:'assistants',file:new File([bytes],'release.json')});
  container = {type:'auto',file_ids:[source.id]};
  code = "import json\nv = json.load(open('/mnt/data/release.json'))['publication']['publication_revision']\nopen('/mnt/data/version.txt','w').write(str(v))\nprint(v)";
} else {
  old = plan.prior;
  assert.equal(old.revision, revision);
  assert.equal(old.source_sha256, plan.source_sha256);
  const prior = await owner.responses.retrieve(old.response,{include});
  const item = prior.output.find(i => i.type === 'code_interpreter_call');
  assert.equal(item.container_id, old.container);
  assert.equal(item.outputs[0].logs.trim(), String(revision));
  await assert.rejects(other.responses.retrieve(old.response),hidden);
  const file = await owner.containers.files.content.retrieve(old.file,{container_id:old.container});
  assert.equal(await file.text(),String(revision));
  container = {type:'auto'};
  code = "v = int(open('/mnt/data/version.txt').read()) + 1\nopen('/mnt/data/next.txt','w').write(str(v))\nprint(v)";
}
const name = create ? 'version.txt' : 'next.txt';
const marker = 'sandbox:/mnt/data/'+name;
const stream = await owner.responses.create({
  model:'qwen3.8-flash-next',temperature:0,reasoning:{effort:'none'},max_output_tokens:256,
  ...(create ? {} : {previous_response_id:old.response}),
  tools:[{type:'code_interpreter',container}],tool_choice:{type:'code_interpreter'},include,stream:true,
  input:'Execute this Python code exactly once. Answer with the number printed and this link: [file]('+marker+').\n'+code});
const events = [];
for await (const event of stream) {
  events.push(event);
  assert.ok(events.length <= 1024);
}
const result = events.at(-1).response;
assert.equal(result.status,'completed',JSON.stringify(result.error));
const calls = result.output.filter(i=>i.type==='code_interpreter_call');
assert.equal(calls.length,1);
const [call] = calls;
assert.equal(call.status,'completed');
assert.equal(call.outputs[0].logs.trim(),expected);
if (!create) assert.equal(call.container_id,old.container);
const part = result.output.filter(i=>i.type==='message').flatMap(i=>i.content)
    .find(p=>p.type==='output_text' && p.text.includes(marker));
assert.ok(part.text.includes(expected));
const ref = part.annotations.find(a=>a.type==='container_file_citation' && a.filename===name);
assert.equal(ref.container_id,call.container_id);
assert.equal(Array.from(part.text).slice(ref.start_index,ref.end_index).join(''),marker);
assert.ok(events.some(e=>e.annotation?.file_id===ref.file_id));
const path = {container_id:call.container_id};
const downloaded = await owner.containers.files.content.retrieve(ref.file_id,path);
assert.equal(await downloaded.text(),expected);
await assert.rejects(other.containers.files.content.retrieve(ref.file_id,path),hidden);
if (create) await owner.files.delete(container.file_ids[0]);
process.stdout.write(JSON.stringify({phase:plan.phase,sdk:version,source_sha256:plan.source_sha256,revision,
  response:result.id,container:call.container_id,file:ref.file_id,filename:name,stdout:expected,
  prior_response:create?null:old.response,prior_file:create?null:old.file,foreign_status:404,
  source_removed:create,events:events.map(e=>e.type)})+'\n');
