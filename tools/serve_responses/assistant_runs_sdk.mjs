// Drive a real Assistants tool handoff using the installed SDK and a release record.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
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
const evidence = readFileSync(plan.source);
assert.equal(createHash('sha256').update(evidence).digest('hex'), plan.source_sha256);
const revision = JSON.parse(evidence).publication.publication_revision;
assert.ok(Number.isSafeInteger(revision) && revision > 0);
const wire = [];
const client = apiKey => new OpenAI({
  apiKey, baseURL:base.href, maxRetries:0, timeout:60000,
  fetch:async (request, init) => {
    const res = await fetch(request, init);
    const path = new URL(typeof request === 'string' || request instanceof URL
      ? request : request.url).pathname;
    const method = init?.method ?? request.method ?? 'GET';
    if (res.ok && path.includes('/runs') && res.headers.get('content-type')?.includes('json'))
      wire.push({path, method, value:await res.clone().json()});
    return res;
  }
});
const owner = client(plan.key), other = client(plan.other);
const threads = owner.beta.threads, runs = threads.runs;
const matches = status => e => e instanceof OpenAI.APIError && e.status === status;
const model = 'qwen3.8-flash-next';
const fn = {name:'get_release', description:'Read the current Q4T release revision from its publication receipt.',
  parameters:{type:'object', properties:{}, additionalProperties:false}, strict:false};
const named = {type:'function', function:{name:fn.name}};
const assistant = await owner.beta.assistants.create({model, temperature:0, reasoning_effort:'none',
  instructions:'Call get_release to read the project release. Once you receive the result answer with just its integer.',
  tools:[{type:'function',function:fn}]});
const thread = await threads.create({messages:[{role:'user',content:'What is the current Q4T release revision?'}]});
const path = {thread_id:thread.id};
assert.deepEqual((await runs.list(thread.id)).data, []);
const settings = {assistant_id:assistant.id,tool_choice:named,max_completion_tokens:256};
const events = [];
async function collect(stream, phase) {
  stream.on('event', e => events.push({phase, event:e.event, data:structuredClone(e.data)}));
  const run = await stream.finalRun();
  assert.equal(events.at(-1).event, 'thread.run.' + run.status);
  return run;
}
const first = await collect(runs.stream(thread.id, settings), 'create');
assert.equal(first.status, 'requires_action', JSON.stringify(first));
assert.equal(first.assistant_id, assistant.id);
const calls = first.required_action.submit_tool_outputs.tool_calls;
assert.equal(calls.length, 1);
const call = calls[0];
assert.equal(call.function.name, fn.name);
assert.deepEqual(JSON.parse(call.function.arguments), {});
const deltas = events.flatMap(e => e.data?.delta?.step_details?.tool_calls ?? []);
assert.ok(deltas.some(d => d.id === call.id && d.function.name === fn.name));
assert.equal(deltas.map(d => d.function.arguments ?? '').join(''), call.function.arguments);
assert.equal((await runs.retrieve(first.id,path)).status, 'requires_action');
assert.equal((await runs.update(first.id,{...path,metadata:{case:'release'}})).metadata.case, 'release');
await assert.rejects(other.beta.threads.runs.retrieve(first.id,path), matches(404));
await assert.rejects(threads.messages.create(thread.id,{role:'user',content:'Change active history'}), matches(409));
for (const outputs of [[], [{tool_call_id:'call_wrong',output:'x'}],
    [{tool_call_id:call.id,output:'x'},{tool_call_id:call.id,output:'y'}]])
  await assert.rejects(runs.submitToolOutputs(first.id,{...path,tool_outputs:outputs}), matches(400));
assert.equal((await runs.list(thread.id,{limit:1})).data[0].id, first.id);
const pendingSteps = (await runs.steps.list(first.id,path)).data;
assert.equal(pendingSteps.length, 1);
const stepPath = {...path,run_id:first.id};
const step = await runs.steps.retrieve(pendingSteps[0].id,stepPath);
assert.equal(step.step_details.tool_calls[0].id, call.id);
assert.equal(step.status, 'in_progress');
await assert.rejects(other.beta.threads.runs.steps.retrieve(step.id,stepPath),matches(404));
const result = String(revision);
const completed = await collect(runs.submitToolOutputsStream(first.id,{...path,
  tool_outputs:[{tool_call_id:call.id,output:result}]}), 'submit');
assert.equal(completed.status,'completed',JSON.stringify(completed));
assert.deepEqual(completed.tool_choice,named);
assert.equal(completed.metadata.case,'release');
assert.ok(completed.usage.prompt_tokens > 0 && completed.usage.completion_tokens > 0);
const messages = (await threads.messages.list(thread.id,{run_id:first.id})).data;
assert.equal(messages.length,1);
assert.equal(messages[0].run_id,first.id);
const answer = messages[0].content.map(p=>p.text.value).join('');
assert.equal(answer.trim(),result);
assert.equal(events.filter(e=>e.phase==='submit' && e.event==='thread.message.delta')
  .flatMap(e=>e.data.delta.content).map(p=>p.text.value).join(''),answer);
const steps = (await runs.steps.list(first.id,{...path,order:'asc'})).data;
assert.equal(steps.length,2);
assert.ok(steps.every(s => s.status==='completed'));
assert.equal(steps[0].step_details.tool_calls[0].function.output,result);
assert.equal(steps[1].step_details.message_creation.message_id,messages[0].id);
assert.ok(events.some(e => e.phase === 'submit' && e.event === 'thread.run.step.completed' &&
  e.data.id === steps[0].id && e.data.step_details.tool_calls[0].function.output === result));
await assert.rejects(runs.submitToolOutputs(first.id,{...path,tool_outputs:[]}),matches(409));
const second = await threads.createAndRun({...settings,thread:{messages:[
  {role:'user',content:'Look up the current Q4T release.'}]}});
const secondPath = {thread_id:second.thread_id};
assert.equal((await runs.poll(second.id,secondPath,{pollIntervalMs:300})).status,'requires_action');
const cancelled = await runs.cancel(second.id,secondPath);
assert.equal(cancelled.status,'cancelled',JSON.stringify(cancelled));
assert.ok(cancelled.cancelled_at > 0);
assert.equal((await runs.steps.list(second.id,secondPath)).data[0].status,'cancelled');
await assert.rejects(runs.submitToolOutputs(second.id,{...secondPath,tool_outputs:[]}),matches(409));
await assert.rejects(runs.retrieve(first.id,secondPath),matches(404));
const excerpt = /^  "publication": \{[\s\S]+?^  \},/m.exec(evidence.toString())[0];
const file = await owner.files.create({purpose:'assistants',
  file:new File([excerpt], 'q4t-publication.txt', {type:'text/plain'})});
const searchThread = await threads.create({messages:[{role:'user',
  content:'Search the attached Q4T publication record for publication_revision and report only its number.',
  attachments:[{file_id:file.id,tools:[{type:'file_search'}]}]}]});
const [store] = searchThread.tool_resources.file_search.vector_store_ids;
for (let i=0; i<30; ++i) {
  const indexed = await owner.vectorStores.files.retrieve(file.id,{vector_store_id:store});
  if (indexed.status === 'completed') break;
  assert.equal(indexed.status,'in_progress',JSON.stringify(indexed.last_error));
  assert.ok(i<29,'timed out indexing publication record');
  await delay(500);
}
const searchPath = {thread_id:searchThread.id};
const searchRun = await runs.create(searchThread.id,{assistant_id:assistant.id,temperature:0,
  instructions:'Retrieve the publication revision using file search. Answer with that integer only.',
  tools:[{type:'file_search',file_search:{max_num_results:1,
    ranking_options:{ranker:'default_2024_08_21',score_threshold:0}}}],
  tool_choice:{type:'file_search'}});
const searched = await runs.poll(searchRun.id,searchPath,{pollIntervalMs:500});
assert.equal(searched.status,'completed',JSON.stringify(searched));
const searchSteps = (await runs.steps.list(searched.id,searchPath)).data;
const matchesFile = s => s.step_details.tool_calls?.some(c => c.type==='file_search' &&
  c.file_search.results?.some(r=>r.file_id===file.id));
const searchStep = searchSteps.find(matchesFile);
assert.ok(searchStep,JSON.stringify(searchSteps));
const seen = searchStep.step_details.tool_calls[0].file_search.results[0];
assert.equal(seen.file_name,'q4t-publication.txt');
assert.ok(!('content' in seen));
const included = await runs.steps.retrieve(searchStep.id,{...searchPath,run_id:searched.id,
  include:['step_details.tool_calls[*].file_search.results[*].content']});
assert.ok(included.step_details.tool_calls[0].file_search.results.some(r =>
  r.file_id===file.id && r.content.some(p=>p.text.includes('"publication_revision": '+revision))));
const searchedMessage = (await threads.messages.list(searchThread.id,{run_id:searched.id})).data[0];
const searchAnswer = searchedMessage.content.map(p=>p.text.value).join('').trim();
assert.equal(searchAnswer,String(revision));
await threads.delete(searchThread.id);
await owner.vectorStores.delete(store);
await owner.files.delete(file.id);
for (const id of [second.thread_id,thread.id]) await threads.delete(id);
await assert.rejects(runs.retrieve(first.id,path),matches(404));
await owner.beta.assistants.delete(assistant.id);
const independent = (await owner.chat.completions.create({model, temperature:0, max_tokens:16,
  reasoning_effort:'none', messages:[{role:'user',content:'What is 6 times 7? Reply only with the number.'}]
})).choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version, wire, events, release_revision:revision,
  source_sha256:plan.source_sha256, answer, search_answer:searchAnswer, search_filename:seen.file_name, independent_chat:independent, terminal:completed.status,
  cancelled:cancelled.status, steps:steps.map(s=>s.type), usage:completed.usage,
  rejected_submissions:3, foreign_statuses:[404,404], active_thread_write:409,
  event_counts:Object.fromEntries(['create','submit'].map(p=>[p,events.filter(e=>e.phase===p).length]))})+'\n');
