// Exercise cumulative Assistants input admission through the official client.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let body = '';
for await (const chunk of process.stdin) {
  body += chunk;
  assert.ok(Buffer.byteLength(body) <= 16384);
}
const plan = JSON.parse(body);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const owner = new OpenAI({apiKey:plan.key, baseURL:base.href, maxRetries:0, timeout:60000});
const {assistants, threads} = owner.beta;
const runs = threads.runs;
const model = 'qwen3.8-flash-next';
const source = readFileSync(plan.source);
assert.equal(createHash('sha256').update(source).digest('hex'), plan.source_sha256);
const publication = JSON.parse(source).publication;
assert.ok(Number.isSafeInteger(publication.publication_revision));
const number = String(publication.publication_revision);
const assistant = await assistants.create({model,temperature:0,reasoning_effort:'none'});
const wire = [], events = [];
async function terminal(stream, phase) {
  let result;
  for await (const event of stream) {
    events.push({phase,event:event.event,data:event.data});
    if (event.event === 'thread.run.incomplete') result = event.data;
  }
  assert.equal(events.at(-1).event, 'thread.run.incomplete');
  assert.equal(result.incomplete_details.reason, 'max_prompt_tokens');
  assert.equal(result.usage.total_tokens, result.usage.prompt_tokens + result.usage.completion_tokens);
  return result;
}
async function checkStored(run) {
  const value = await runs.retrieve(run.id, {thread_id:run.thread_id});
  wire.push(value);
  assert.deepEqual(value, run);
  const steps = await runs.steps.list(run.id, {thread_id:run.thread_id});
  const messages = await threads.messages.list(run.thread_id, {run_id:run.id});
  assert.equal(messages.data.length, 0);
  return steps.data;
}
const oversized = await threads.create({messages:[{role:'user',content:'archived '.repeat(400)}]});
const denied = await terminal(await runs.create(oversized.id, {assistant_id:assistant.id,
  max_prompt_tokens:256, stream:true}), 'initial');
assert.deepEqual(await checkStored(denied), []);
assert.deepEqual(denied.usage, {prompt_tokens:0,completion_tokens:0,total_tokens:0});

const lookup = await threads.create({messages:[
  {role:'user',content:'What is the current Q4T release revision?'}]});
const named = {type:'function', function:{name:'get_release'}};
const queued = await runs.create(lookup.id, {assistant_id:assistant.id, temperature:0,
  instructions:'Call get_release to read the project release. Once you receive the result answer with just its integer.',
  tools:[{type:'function',function:{name:'get_release',
    description:'Read the current Q4T release revision from its publication receipt.',
    parameters:{type:'object',properties:{},additionalProperties:false}, strict:false}}],
  tool_choice:named, max_prompt_tokens:580, max_completion_tokens:256});
const pending = await runs.poll(queued.id, {thread_id:lookup.id}, {pollIntervalMs:250});
assert.equal(pending.status, 'requires_action', JSON.stringify(pending));
const [call] = pending.required_action.submit_tool_outputs.tool_calls;
assert.equal(call.function.name, 'get_release');
const exhausted = await terminal(await runs.submitToolOutputs(pending.id, {thread_id:lookup.id,
  tool_outputs:[{tool_call_id:call.id,output:number}], stream:true}), 'submit');
const [settled] = await checkStored(exhausted);
assert.equal(settled.status, 'completed');
assert.equal(settled.step_details.tool_calls[0].function.output, number);
assert.ok(exhausted.usage.prompt_tokens > 0 && exhausted.usage.prompt_tokens < 580);
assert.ok(exhausted.usage.completion_tokens > 0);

const file = await owner.files.create({purpose:'assistants',file:new File(
  [JSON.stringify(publication,null,2)],'q4t-publication.txt',{type:'text/plain'})});
const search = await threads.create({messages:[{role:'user',
  content:'Search the attached Q4T publication record for publication_revision and report only its number.',
  attachments:[{file_id:file.id,tools:[{type:'file_search'}]}]}]});
const [store] = search.tool_resources.file_search.vector_store_ids;
for (let i=0; i<40; ++i) {
  const indexed = await owner.vectorStores.files.retrieve(file.id,{vector_store_id:store});
  if (indexed.status === 'completed') break;
  assert.equal(indexed.status, 'in_progress');
  assert.ok(i < 39, 'publication record indexing timed out');
  await delay(300);
}
const searchRequest = {assistant_id:assistant.id,temperature:0,
  instructions:'Retrieve the publication revision using file search. Answer with that integer only.',
  tools:[{type:'file_search',file_search:{max_num_results:1}}],
  tool_choice:{type:'file_search'},max_prompt_tokens:640,stream:true};
const searched = await terminal(await runs.create(search.id,searchRequest), 'file_search');
const [hit] = await checkStored(searched);
assert.equal(hit.status, 'completed');
assert.ok(hit.step_details.tool_calls.some(c => c.type==='file_search' &&
  c.file_search.results.some(r => r.file_id===file.id)));
assert.ok(searched.usage.prompt_tokens > 0 && searched.usage.prompt_tokens <= 640);

// On the same thread and fixed model settings the successful query above
// measured prompt plus embedding input. One token less fits its model call
// and refuses the host query before any embedding input is charged.
const queryLimit = searched.usage.prompt_tokens - 1;
assert.ok(queryLimit >= 256);
const stoppedSearch = await terminal(await runs.create(search.id, {
  ...searchRequest, max_prompt_tokens:queryLimit}), 'query_admission');
const [skipped] = await checkStored(stoppedSearch);
assert.equal(skipped.status, 'cancelled');
assert.ok(skipped.step_details.tool_calls.every(c =>
  c.type === 'file_search' && !(c.file_search.results?.length)));
assert.ok(stoppedSearch.usage.prompt_tokens > 0 &&
  stoppedSearch.usage.prompt_tokens <= queryLimit);
assert.equal(stoppedSearch.usage.completion_tokens, searched.usage.completion_tokens);

const independent = await owner.chat.completions.create({model,temperature:0,max_tokens:16,
  reasoning_effort:'none',messages:[{role:'user',content:'What is 6 times 7? Reply only with the number.'}]});
const answer = independent.choices[0].message.content.trim();
assert.equal(answer,'42');
process.stdout.write(JSON.stringify({sdk:version,wire,events,release_revision:Number(number),
  independent_chat:answer,initial:denied.id,submit:exhausted.id,file_search:searched.id,query_admission:stoppedSearch.id,
  usage:[denied,exhausted,searched,stoppedSearch].map(r=>r.usage),source_sha256:plan.source_sha256})+'\n');
