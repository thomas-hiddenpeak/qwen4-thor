// Create and later complete one persisted tool call through two service generations.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 32768);
}
const p = JSON.parse(input);
const origin = new URL(p.base);
assert.equal(origin.protocol,'https:');
assert.equal(origin.hostname,'127.0.0.1');
const {OpenAI,version} = loadSdk(p.sdk);
assert.equal(version,'7.47.0');
const bytes = readFileSync(p.source);
assert.equal(createHash('sha256').update(bytes).digest('hex'),p.source_sha256);
const revision = JSON.parse(bytes).publication.publication_revision;
const connect = apiKey => new OpenAI({apiKey,baseURL:p.base,timeout:60000,maxRetries:0});
const owner = connect(p.key), foreign = connect(p.other);
const runs = owner.beta.threads.runs;
const hidden = e => e instanceof OpenAI.APIError && e.status === 404;
const budget = p.max_prompt_tokens;
if (budget !== undefined) assert.ok(Number.isSafeInteger(budget) && budget >= 256);
const budgetFields = budget === undefined ? {} : {max_prompt_tokens:budget};
let proof;
if (p.phase === 'create') {
  const assistant = await owner.beta.assistants.create({
    model:'qwen3.8-flash-next', temperature:0, reasoning_effort:'none',
    instructions:'Look up the release using get_release. When the tool returns, answer only with the integer returned.',
    tools:[{type:'function',function:{name:'get_release',description:'Read the Q4T publication revision.',
      strict:false,parameters:{type:'object',properties:{},additionalProperties:false}}}]});
  const thread = await owner.beta.threads.create({metadata:{purpose:'release handoff'},
    messages:[{role:'user',content:'What is the Q4T publication revision?'}]});
  const events = [];
  const stream = runs.stream(thread.id,{assistant_id:assistant.id,max_completion_tokens:256,...budgetFields,
    tool_choice:{type:'function',function:{name:'get_release'}}});
  stream.on('event', e => events.push(e.event));
  const run = await stream.finalRun();
  assert.equal(run.status,'requires_action',JSON.stringify(run));
  if (budget !== undefined) assert.equal(run.max_prompt_tokens,budget);
  const calls = run.required_action.submit_tool_outputs.tool_calls;
  assert.equal(calls.length,1);
  assert.equal(calls[0].function.name,'get_release');
  assert.deepEqual(JSON.parse(calls[0].function.arguments),{});
  const path = {thread_id:thread.id};
  const steps = (await runs.steps.list(run.id,path)).data;
  assert.equal(steps.length,1);
  assert.equal(steps[0].step_details.tool_calls[0].id,calls[0].id);
  assert.equal(steps[0].status,'in_progress');
  await assert.rejects(foreign.beta.threads.runs.retrieve(run.id,path),hidden);
  assert.ok(events.includes('thread.run.step.delta') && events.at(-1)==='thread.run.requires_action');
  proof = {assistant:assistant.id,thread:thread.id,run:run.id,step:steps[0].id,
    call:calls[0].id,arguments:calls[0].function.arguments,events,status:run.status};
} else {
  assert.equal(p.phase,'resume');
  const old = p.prior;
  assert.equal(old.status,'requires_action');
  assert.equal(old.source_sha256,p.source_sha256);
  const path = {thread_id:old.thread};
  const run = await runs.retrieve(old.run,path);
  const assistant = await owner.beta.assistants.retrieve(old.assistant);
  assert.equal(run.assistant_id,assistant.id);
  assert.equal(run.thread_id,old.thread);
  assert.equal(run.status,'requires_action');
  if (budget !== undefined) assert.equal(run.max_prompt_tokens,budget);
  const [call] = run.required_action.submit_tool_outputs.tool_calls;
  assert.equal(call.id,old.call);
  assert.equal(call.function.arguments,old.arguments);
  const pending = await runs.steps.retrieve(old.step,{...path,run_id:old.run});
  assert.equal(pending.status,'in_progress');
  await assert.rejects(foreign.beta.threads.runs.retrieve(old.run,path),hidden);
  const stream = runs.submitToolOutputsStream(old.run,{...path,
    tool_outputs:[{tool_call_id:call.id,output:String(revision)}]});
  const events = [], text = [];
  stream.on('event', e => events.push(e.event));
  stream.on('textDelta', d => text.push(d.value));
  const done = await stream.finalRun();
  assert.equal(done.id,old.run);
  assert.equal(done.status,'completed',JSON.stringify(done));
  if (budget !== undefined) {
    assert.equal(done.max_prompt_tokens,budget);
    assert.ok(done.usage.prompt_tokens > 0 && done.usage.prompt_tokens <= budget);
  }
  assert.equal(text.join('').trim(),String(revision));
  const steps = (await runs.steps.list(done.id,{...path,order:'asc'})).data;
  assert.equal(steps.length,2);
  assert.equal(steps[0].id,old.step);
  assert.equal(steps[0].step_details.tool_calls[0].function.output,String(revision));
  assert.equal(steps[1].type,'message_creation');
  assert.ok(steps.every(s=>s.status==='completed'));
  const [message] = (await owner.beta.threads.messages.list(old.thread,{run_id:done.id})).data;
  assert.equal(message.id,steps[1].step_details.message_creation.message_id);
  assert.equal(message.content[0].text.value,text.join(''));
  assert.equal(events.filter(e=>e==='thread.run.step.completed').length,2);
  assert.equal(events.at(-1),'thread.run.completed');
  proof = {assistant:assistant.id,thread:run.thread_id,run:done.id,step:steps[0].id,call:call.id,
    arguments:call.function.arguments,events,status:done.status,answer:text.join('').trim(),
    total_tokens:done.usage.total_tokens,message:message.id,
    ...(budget === undefined ? {} : {prompt_tokens:done.usage.prompt_tokens})};
}
process.stdout.write(JSON.stringify({...proof,...budgetFields,sdk:version,source_sha256:p.source_sha256,
  revision,foreign_status:404})+'\n');
