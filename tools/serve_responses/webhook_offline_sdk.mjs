// Exercise a service installed without operator-granted outbound delivery.
import assert from 'node:assert/strict';
import {setTimeout as wait} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
assert.equal(version,'7.47.0');
const client = apiKey => new OpenAI({baseURL:p.base,apiKey,maxRetries:0,timeout:20000});
const owner = client(p.key);
const peer = client(p.other);
const types = await owner.webhooks.eventTypes.list();
assert.ok(types.data.includes('response.completed'));
const created = p.previous ?? await owner.webhooks.create({name:'Release subscription',
  url:p.url,event_types:['response.completed']});
const endpoint = p.previous?.endpoint ?? created.id;
assert.equal((await owner.webhooks.retrieve(endpoint)).id,endpoint);
await assert.rejects(peer.webhooks.retrieve(endpoint), e => e.status===404);
await assert.rejects(owner.webhooks.test(endpoint,{event_type:'response.completed'}),
                     e => e.status===503 && e.code==='webhook_policy_unavailable');
const body = await (p.previous ? owner.responses.retrieve(p.previous.response) :
  owner.responses.create({model:p.model,background:true,store:true,temperature:0,
    reasoning:{effort:'none'},max_output_tokens:32,input:'Return only the release number '+p.revision+'.'}));
let completed = body;
for (let i=0;i<100 && ['queued','in_progress'].includes(completed.status);++i) {
  await wait(200);
  completed = await owner.responses.retrieve(body.id);
}
assert.equal(completed.status,'completed');
assert.equal(completed.output_text.trim(),String(p.revision));
if (p.previous) assert.equal((await owner.webhooks.delete(endpoint)).deleted,true);
process.stdout.write(JSON.stringify({sdk:version,endpoint,response:body.id,answer:completed.output_text.trim(),
  peer:404,disabled_test:503,code:'webhook_policy_unavailable',deleted:Boolean(p.previous)}));
