// Official SDK, native loopback TLS and an independently verified release file.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync, writeFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const url = new URL(plan.base);
assert.equal(url.protocol, 'https:');
assert.equal(url.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version,'7.47.0');
const client = key => new OpenAI({apiKey:key, baseURL:url.href, maxRetries:0, timeout:75000});
const owner = client(plan.key), other = client(plan.other);
const bytes = readFileSync(plan.source);
assert.equal(createHash('sha256').update(bytes).digest('hex'), plan.source_sha256);
const revision = String(JSON.parse(bytes).publication.publication_revision);
const file = await owner.files.create({purpose:'assistants', file:new File([bytes],'release.json')});
const workspace = await owner.containers.create({name:'release-report', file_ids:[file.id]});
await owner.files.delete(file.id);
const code = "import json\nv = json.load(open('/mnt/data/release.json'))['publication']['publication_revision']\nopen('/mnt/data/selected.txt', 'w').write(str(v))\nprint(v)";
const model = 'qwen3.8-flash-next';
const request = {model, temperature:0, reasoning:{effort:'none'}, max_output_tokens:256,
  include:['code_interpreter_call.outputs'], tools:[{type:'code_interpreter',container:workspace.id}],
  tool_choice:{type:'code_interpreter'}, input:'Run this Python code exactly. Reply with the number it prints followed by this download link: [selected](sandbox:/mnt/data/selected.txt).\n'+code};
const wire = [];
const stream = await owner.responses.create({...request,stream:true});
for await (const event of stream) {
  wire.push(event);
  assert.ok(wire.length <= 1024);
  writeFileSync(plan.wire, JSON.stringify(wire), {mode:0o600});
}
const response = wire.at(-1)?.response;
assert.equal(response?.status, 'completed');
const saved = await owner.responses.retrieve(response.id,{include:['code_interpreter_call.outputs']});
writeFileSync(plan.wire+'.get.json', JSON.stringify(saved), {mode:0o600});
assert.deepEqual(saved.status,response.status);
assert.deepEqual(saved.usage,response.usage);
assert.deepEqual(saved.output,response.output);
const replay = await owner.responses.retrieve(response.id,
  {stream:true,include:['code_interpreter_call.outputs']});
const replayEvents = [];
for await (const event of replay) {
  replayEvents.push(event);
  assert.ok(replayEvents.length <= 1024);
}
assert.deepEqual(replayEvents.map(e=>[e.type,e.sequence_number]),
                 wire.map(e=>[e.type,e.sequence_number]));
assert.deepEqual(replayEvents.at(-1).response.output,saved.output);
writeFileSync(plan.wire+'.replay.json', JSON.stringify(replayEvents), {mode:0o600});
const sequences = wire.map(e=>e.sequence_number);
assert.deepEqual(sequences,sequences.map((_,i)=>i + sequences[0]));
const item = response.output.find(i=>i.type==='code_interpreter_call');
assert.equal(item?.status, 'completed');
assert.equal(item.container_id, workspace.id);
assert.equal(item.outputs.find(o=>o.type==='logs')?.logs.trim(),revision);
const answerText = response.output.filter(i=>i.type==='message').flatMap(i=>i.content)
    .filter(p=>p.type==='output_text').map(p=>p.text).join('').trim();
const answer = answerText.match(/\b\d+\b/)?.[0];
assert.equal(answer, revision);
const events = wire.filter(e=>e.item_id===item.id);
const types = events.map(e=>e.type);
for (const kind of ['response.code_interpreter_call.in_progress','response.code_interpreter_call_code.delta',
  'response.code_interpreter_call_code.done','response.code_interpreter_call.interpreting',
  'response.code_interpreter_call.completed']) assert.ok(types.includes(kind),kind);
const files = await owner.containers.files.list(workspace.id);
const created = files.data.find(f=>f.path==='/mnt/data/selected.txt');
assert.equal(created?.source,'assistant');
const marker = 'sandbox:/mnt/data/selected.txt';
const verifyRange = (text, ref) =>
  assert.equal(Array.from(text).slice(ref.start_index,ref.end_index).join(''),marker);
const message = saved.output.find(i=>i.type==='message');
const part = message.content.find(p=>p.type==='output_text' && p.text.includes(marker));
const citation = part?.annotations?.find(a=>a.type==='container_file_citation');
assert.equal(citation?.file_id,created.id);
assert.equal(citation.container_id,workspace.id);
verifyRange(part.text,citation);
assert.ok(wire.some(e=>e.type==='response.output_text.annotation.added' &&
  e.annotation?.file_id===created.id));
const opts = {container_id:workspace.id};
const download = await owner.containers.files.content.retrieve(created.id,opts);
assert.equal(await download.text(),revision);
await assert.rejects(other.containers.files.content.retrieve(created.id,opts),e=>e.status===404);
const attached = await owner.files.create({purpose:'assistants',file:new File([bytes],'release.json')});
const assistant = await owner.beta.assistants.create({model,temperature:0, reasoning_effort:'none',
  tools:[{type:'code_interpreter'}],tool_resources:{code_interpreter:{file_ids:[attached.id]}}});
const thread = await owner.beta.threads.create({
  messages:[{role:'user',content:request.input}]});
const runs = owner.beta.threads.runs;
const runStream = runs.stream(thread.id,{assistant_id:assistant.id,
  max_prompt_tokens:4096,max_completion_tokens:256,tool_choice:{type:'code_interpreter'}});
const assistantEvents = [];
runStream.on('event', event => {
  // AssistantStream accumulates later deltas into earlier nested objects.
  assistantEvents.push(structuredClone(event));
  assert.ok(assistantEvents.length <= 1024);
  writeFileSync(plan.wire+'.assistants.json',JSON.stringify(assistantEvents),{mode:0o600});
});
const run = await runStream.finalRun();
assert.equal(run.status,'completed',JSON.stringify(run.last_error));
const path = {thread_id:thread.id};
const steps = (await runs.steps.list(run.id,{...path,order:'asc'})).data;
assert.equal(steps.length,2);
assert.ok(steps.every(s=>s.status==='completed'));
const call = steps[0].step_details.tool_calls[0];
assert.equal(call.type,'code_interpreter');
assert.ok(call.code_interpreter.input.includes('release.json'));
assert.equal(call.code_interpreter.outputs[0].logs.trim(),revision);
const [reply] = (await owner.beta.threads.messages.list(thread.id,{run_id:run.id})).data;
assert.equal(steps[1].step_details.message_creation.message_id,reply.id);
const cited = reply.content.find(p=>p.type==='text' && p.text.value.includes(marker));
assert.ok(cited.text.value.includes(revision));
const ref = cited.text.annotations.find(a=>a.type==='file_path');
verifyRange(cited.text.value,ref);
assert.equal(ref.text,marker);
const output = await owner.files.retrieve(ref.file_path.file_id);
assert.equal(output.purpose,'assistants_output');
assert.equal(await (await owner.files.content(output.id)).text(),revision);
await assert.rejects(other.files.content(output.id),e=>e.status===404);
const deltas = assistantEvents.filter(e=>e.event==='thread.run.step.delta')
    .flatMap(e=>e.data.delta.step_details?.tool_calls ?? []);
assert.equal(deltas.map(d=>d.code_interpreter?.input ?? '').join(''),call.code_interpreter.input);
assert.equal(deltas.flatMap(d=>d.code_interpreter?.outputs ?? []).map(o=>o.logs).join('').trim(),revision);
assert.ok(assistantEvents.some(e=>e.event==='thread.message.delta' &&
  e.data.delta.content?.some(p=>p.text?.annotations?.some(a=>a.file_path?.file_id===output.id))));
assert.equal(assistantEvents.at(-1).event,'thread.run.completed');
writeFileSync(plan.wire+'.artifacts.json', JSON.stringify({run,steps,reply,output}), {mode:0o600});
const chat = await owner.chat.completions.create({model,temperature:0,enable_thinking:false,
  max_completion_tokens:24,messages:[{role:'user',content:'Compute 17 plus 25. Respond only with the number.'}]});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,revision,answer,container:workspace.id,
  file:created.id, citation, answer_text:answerText, assistant_run:run.id, assistant_file:output.id, events:types, returned_logs:item.outputs,foreign_status:404,
  independent_chat:independent,wire})+'\n');
