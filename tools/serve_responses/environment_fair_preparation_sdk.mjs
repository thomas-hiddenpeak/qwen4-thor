// Queue two independent clients, remove both originals, then retrieve the
// installed workspaces and exercise healthy inference through the same ingress.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) input += part;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const clients = [p.key,p.other].map(apiKey=>new OpenAI({apiKey,baseURL:p.base,
  timeout:30000,maxRetries:0}));
const documents = [readFileSync(p.projectGoals),readFileSync(
  new URL('../../docs/evidence/serve-webhooks-20261124/environment-delivery.json',import.meta.url))];
const sources = await Promise.all(clients.map((c,i)=>c.files.create({
  file:new File([documents[i]],'release.json',{type:'application/json'}),purpose:'user_data'})));
const bodies = sources.map(file=>({environment:{type:'openai_hosted',files:['a','b'].map(name=>({
  type:'file_id',file_id:file.id,path:'/workspace/'+name+'.json'}))},
  'Idempotency-Key':'same two-client prewarm key'}));
const queued = await Promise.all(clients.map((c,i)=>c.beta.agents.environments.create(bodies[i])));
assert.ok(queued.every(row=>row.status==='pending'));
assert.notEqual(queued[0].id,queued[1].id);
await Promise.all(clients.map((c,i)=>c.files.delete(sources[i].id)));
const ready = await Promise.all(clients.map(async(c,i)=>{
  const environments=c.beta.agents.environments;
  const end=Date.now()+30000;
  let row=await environments.retrieve(queued[i].id);
  while(row.status==='pending') {
    assert.ok(Date.now()<end,'two-client preparation timeout');
    await delay(100);
    row=await environments.retrieve(queued[i].id);
  }
  assert.equal(row.status,'ready');
  assert.deepEqual(row.files.map(f=>f.size_bytes),[documents[i].length,documents[i].length]);
  assert.deepEqual(await environments.create(bodies[i]),row);
  await assert.rejects(environments.retrieve(queued[1-i].id),err=>err.status===404);
  return row;
}));
const independent=await clients[1].chat.completions.create({model:p.model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
const answer=independent.choices[0].message.content.trim();
assert.equal(answer,'42');
process.stdout.write(JSON.stringify({sdk:version,peer:404,independent_chat:answer,
  original_deleted:2,ready_jobs:2,source_bytes:documents.map(b=>b.length),wire:[...queued,...ready]}));
