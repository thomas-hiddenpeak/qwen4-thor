// The continuous fixture keeps its key in this process. The capacity fixture
// transfers it through private pipes between the two short-lived subscribers.
import assert from 'node:assert/strict';
import {once} from 'node:events';
import {existsSync, readFileSync, writeFileSync} from 'node:fs';
import https from 'node:https';
import {join} from 'node:path';
import {setTimeout as wait} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const options = {baseURL:p.base, timeout:20000, maxRetries:0};
const owner = new OpenAI({...options, apiKey:p.key});
const peer = new OpenAI({...options, apiKey:p.other});
const prior = p.previous;
const deliveries = prior ? [prior.delivery] : [];
let secret = prior?.secret;
async function until(check, timeout = 45000) {
  const deadline = Date.now() + timeout;
  do {
    const value = await check();
    if (value) return value;
    await wait(300);
  } while (Date.now() < deadline);
  throw new Error('Timed out waiting for ' + check.name);
}
const server = https.createServer({
  key: readFileSync(p.tlsKey), cert: readFileSync(p.tlsCert),
}, async (req, res) => {
  try {
    assert.equal(req.url, '/lifecycle');
    assert.equal(req.method, 'POST');
    let wire = '';
    for await (const chunk of req) wire += chunk;
    const headers = new Headers(req.headers);
    const event = await owner.webhooks.unwrap(wire, headers, secret);
    const status = deliveries.length ? 202 : 503;
    deliveries.push({event, wire, message:headers.get('webhook-id'), status,
                     received:Math.floor(Date.now()/1000)});
    res.writeHead(status).end();
  } catch (error) {
    process.stderr.write('Lifecycle subscriber failed: ' + error.name + '\n');
    process.exitCode = 1;
    res.writeHead(500).end();
  }
});
try {
  server.listen(p.subscriberPort, '127.0.0.1');
  await once(server, 'listening');
  const sub = prior ? {id:prior.endpoint} : await owner.webhooks.create({name: 'Release lifecycle',
    url:'https://127.0.0.1:'+p.subscriberPort+'/lifecycle',event_types:['response.completed','batch.completed']});
  if (!prior) secret = sub.signing_secret;
  const task = prior ? {id:prior.response} : await owner.responses.create({model:p.model, background:true, store:true,
    reasoning:{effort:'none'}, temperature:0, max_output_tokens:48,
    input:'The verified release revision is '+p.revision+'. Return only that integer.'});
  const completed = await until(async function completedResponse() {
    const row = await owner.responses.retrieve(task.id);
    return row.status === 'completed' && row;
  });
  assert.equal(completed.output_text.trim(), String(p.revision));
  if (!prior) {
    await until(function firstDelivery() { return deliveries.length === 1; });
    assert.equal(deliveries[0].event.type,'response.completed');
    assert.equal(deliveries[0].event.data.id,task.id);
  }
  const marker = {response:task.id,endpoint:sub.id,event:deliveries[0].event.id};
  if (p.phase === 'prepare') {
    // The controller retains "handoff" only in memory. Its receipt is public.
    const handoff = {secret,delivery:deliveries[0],...marker};
    const receipt = {...marker,sdk:version,first:deliveries[0].event,
                     status:deliveries[0].status,responseAnswer:completed.output_text.trim()};
    process.stdout.write(JSON.stringify({handoff,receipt}));
  } else {
    if (!prior) {
      writeFileSync(join(p.control,'restart-request.json'), JSON.stringify(marker),
                    {flag:'wx',mode:0o600});
      await until(function secondGateway() { return existsSync(join(p.control,'second-ready.json')); },125000);
    }
    await until(function retriedDelivery() { return deliveries.length >= 2; }, 70000);
    assert.equal(deliveries[1].message,deliveries[0].message);
    assert.equal(deliveries[1].wire,deliveries[0].wire);
    assert.equal((await owner.webhooks.retrieve(sub.id)).id,sub.id);
    assert.equal((await owner.responses.retrieve(task.id)).output_text.trim(),String(p.revision));
    await assert.rejects(peer.webhooks.retrieve(sub.id), error => error.status===404);
    const line = {custom_id:'release-record', method:'POST', url:'/v1/chat/completions',
      body:{model:p.model,messages:[{role:'user',content:'Return only the number '+p.revision+'.'}],
            temperature:0,max_tokens:32,chat_template_kwargs:{enable_thinking:false}}};
    const file = await owner.files.create({purpose:'batch',file:new File([JSON.stringify(line)+'\n'],'release.jsonl',{type:'application/jsonl'})});
    const batch = await owner.batches.create({input_file_id:file.id,endpoint:line.url,completion_window:'24h'});
    const finished = await until(async function completedBatch() {
      const row = await owner.batches.retrieve(batch.id);
      assert.ok(!['failed','expired','cancelled'].includes(row.status),row.status);
      return row.status==='completed' && row;
    }, 60000);
    const output = JSON.parse((await (await owner.files.content(finished.output_file_id)).text()).trim());
    assert.equal(output.custom_id,line.custom_id);
    const batchAnswer = output.response.body.choices[0].message.content.trim();
    assert.equal(batchAnswer,String(p.revision));
    await until(function batchDelivery() { return deliveries.length >= 3; });
    assert.equal(deliveries[2].event.type,'batch.completed');
    assert.equal(deliveries[2].event.data.id,batch.id);
    const chat = await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
      messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
    assert.equal(chat.choices[0].message.content.trim(),'42');
    assert.equal((await owner.webhooks.delete(sub.id)).deleted,true);
    assert.equal(deliveries.length,3);
    process.stdout.write(JSON.stringify({sdk:version,endpoint:sub.id,response:task.id,
      responseAnswer:completed.output_text.trim(),batch:batch.id,batchAnswer,peer:404,independent_chat:'42',
      deliveries:deliveries.map(({wire,...fields})=>fields)}));
  }
} finally {
  server.closeAllConnections();
  server.close();
}
