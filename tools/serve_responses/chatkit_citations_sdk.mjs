// Exercise the registered workflow through the ChatKit browser transport and
// compare its persisted sources with the official management SDK.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const bytes of process.stdin) input += bytes;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const client = key => new OpenAI({apiKey:key, baseURL:p.base, maxRetries:0});
const owner=client(p.key), other=client(p.other);
const instructions = 'Search once with the short query Q4T publication revision. ' +
  'Answer with the revision number from the retrieved publication record and copy its citation_marker exactly, including angle brackets.';
async function workflow(tools) {
  const agent = await owner.beta.agents.create({model:p.model,reasoning:{effort:'none'},instructions,tools});
  return owner.beta.chatkit.sessions.create({workflow:{id:agent.id},user:'reader'});
}
async function browser(grant,type,params,observe) {
  const r = await fetch(p.base+'/chatkit',{method:'POST',signal:AbortSignal.timeout(60000),
    headers:{Authorization:'Bearer '+grant.client_secret,'Content-Type':'application/json'},
    body:JSON.stringify({type,params})});
  if (r.status !== 200) throw Error(type+' '+r.status+' '+await r.text());
  if (!r.headers.get('content-type')?.includes('text/event-stream')) return r.json();
  const decoder = new TextDecoder();
  let carry = ''; const events = [];
  for await (const chunk of r.body) {
    carry += decoder.decode(chunk,{stream:true});
    let match;
    while ((match=/\r?\n\r?\n/.exec(carry)) !== null) {
      const line=carry.slice(0,match.index); carry=carry.slice(match.index+match[0].length);
      if (!line.startsWith('data: ')) continue;
      const event=JSON.parse(line.slice(6)); events.push(event);
      if (observe) await observe(event);
    }
  }
  assert.equal((carry+decoder.decode()).trim(),'');
  return events;
}
const message = (text,options={}) => ({input:{content:[{type:'input_text',text}],attachments:[],inference_options:options}});
const completed=[];
for (const mode of ['live','cached']) {
  const grant=await workflow([{type:'web_search',mode,context_size:'low'}]);
  const events=await browser(grant,'threads.create',message(
    'What publication revision does the Q4T publication record give? Include the exact source citation marker.',{tool_choice:{id:'web_search'}}));
  const thread=events.find(e=>e.type==='thread.created')?.thread.id;
  const answer=events.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message')?.item;
  assert.ok(answer,JSON.stringify(events));
  const delta=events.filter(e=>e.update?.type==='assistant_message.content_part.annotation_added');
  const part=answer.content[0], marker='[source](<'+p.source+'>)';
  assert.ok(part.text.includes(String(p.revision)) && part.text.includes(marker),JSON.stringify(part));
  const expected={type:'annotation',source:{type:'url',url:p.source,title:p.title},
    index:Array.from(part.text.slice(0,part.text.indexOf(marker)+marker.length)).length};
  assert.deepEqual(part.annotations,[expected]);
  assert.equal(delta.length,1);
  assert.equal(delta[0].item_id,answer.id);
  assert.deepEqual(delta[0].update,{type:'assistant_message.content_part.annotation_added',annotation_index:0,
    content_index:0,annotation:expected});
  const history=await browser(grant,'items.list',{thread_id:thread,order:'asc'});
  assert.deepEqual(history.data.find(item=>item.id===answer.id),answer);
  const management=await (await owner.beta.chatkit.threads.listItems(thread,{order:'asc'}).asResponse()).json();
  assert.deepEqual(management.data.find(item=>item.id===answer.id).content[0].annotations,
    [{type:'url',source:{type:'url',url:p.source}}]);
  await assert.rejects(other.beta.chatkit.threads.listItems(thread),e=>e.status===404);
  completed.push({mode,events,answer,history,management,foreign:404});
}
const grant=await workflow([]);
let deleted;
const events=await browser(grant,'threads.create',message(
  'Write a numbered list of thirty short observations about continuous integration.'), async event=>{
  if (event.type==='thread.created') {
    deleted=event.thread.id;
    await owner.beta.chatkit.threads.delete(deleted);
  }
});
assert.equal(events.at(-1).type,'error',JSON.stringify(events));
assert.equal(events.filter(e=>e.type==='error').length,1);
await assert.rejects(owner.beta.chatkit.threads.retrieve(deleted),e=>e.status===404);
const chat=await owner.chat.completions.create({model:p.model,messages:[{role:'user',content:'Compute 17 + 25. Reply only with the integer.'}],temperature:0,max_tokens:32,chat_template_kwargs:{enable_thinking:false}});
assert.equal(chat.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,completed,deleted,terminal:events,independent_chat:'42'}));
