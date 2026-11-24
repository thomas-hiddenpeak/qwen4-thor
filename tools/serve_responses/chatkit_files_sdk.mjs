// Drive a real document through the configured direct upload entry and two
// model turns. The model receives the document exclusively as an attachment.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI,version} = loadSdk(p.sdk);
const project = new OpenAI({baseURL:p.base,apiKey:p.key,maxRetries:0});
const peer = new OpenAI({baseURL:p.base,apiKey:p.other,maxRetries:0});
const agent = await project.beta.agents.create({model:p.model,reasoning:{effort:'none'},
  instructions:'Answer questions about the uploaded project record with just the requested integer.'});
const create = (user,enabled)=>project.beta.chatkit.sessions.create({user,workflow:{id:agent.id},
  rate_limits:{max_requests_per_1_minute:40},chatkit_configuration:{file_upload:{
    enabled,max_files:1,max_file_size:1}}});
const owner=await create('document-reader',true);
const sibling=await create('another-reader',true);
const disabled=await create('view-only',false);
const source=readFileSync(new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json',import.meta.url));
const expected=String(JSON.parse(source).counts.required_inference_or_application_service);
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
async function request(grant,path,body,expectedStatus,stream=false) {
  const isForm=body instanceof FormData;
  const res=await fetch(p.base+path,{method:'POST',signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+grant.client_secret,...(isForm?{}:{'Content-Type':'application/json'})},
    body:isForm?body:JSON.stringify(body)});
  if(res.status!==expectedStatus) throw new Error(path+' '+res.status+' '+(await res.text()).slice(0,320));
  if(!stream) return res.json();
  const events=(await res.text()).split(/\r?\n/).filter(x=>x.startsWith('data: ')).map(x=>JSON.parse(x.slice(6)));
  const err=events.find(x=>x.type==='error');
  assert.ok(!err,JSON.stringify(err));
  const answer=events.find(x=>x.type==='thread.item.done' && x.item.type==='assistant_message');
  assert.equal(answer?.item.content.map(x=>x.text).join('').trim(),expected);
  return events;
}
function upload(grant,bytes,expectedStatus) {
  const form=new FormData();
  form.set('file',new File([bytes],'decision.json',{type:'application/json'}));
  return request(grant,'/chatkit/attachments',form,expectedStatus);
}
const rpc=(grant,type,params,status=200,stream=false)=>request(grant,'/chatkit',{type,params},status,stream);
const user=(text,attachments=[])=>({content:[{type:'input_text',text}],attachments,inference_options:{}});
await upload(disabled,source,403);
const file=await upload(owner,source,200);
assert.equal(file.name,'decision.json');
assert.equal(file.mime_type,'application/json');
await upload(owner,source,409);
await assert.rejects(peer.files.retrieve(file.id),e=>e.status===404);
const downloaded=Buffer.from(await (await project.files.content(file.id)).arrayBuffer());
assert.equal(sha(downloaded),sha(source));
await rpc(sibling,'threads.create',{input:user('Read the file.',[file.id])},404);
await rpc(sibling,'attachments.delete',{attachment_id:file.id},404);
const imported=new FormData();
imported.set('file_id',file.id);
await request(sibling,'/chatkit/attachments',imported,400);
await project.beta.chatkit.sessions.cancel(sibling.id);
await upload(sibling,source,401);
const events=await rpc(owner,'threads.create',{input:user(
  'From the attached scope decision, what integer is counts.required_inference_or_application_service? Answer only the integer.',[file.id])},200,true);
const id=events.find(x=>x.type==='thread.created').thread.id;
const linked=events.find(x=>x.type==='thread.item.done' && x.item.type==='user_message').item;
assert.equal(linked.attachments[0].id,file.id);
await rpc(owner,'attachments.delete',{attachment_id:file.id});
await assert.rejects(project.files.retrieve(file.id),e=>e.status===404);
await rpc(owner,'threads.add_user_message',{thread_id:id,input:user('Repeat the previously requested integer from our project scope decision.')},200,true);
const history=await (await project.beta.chatkit.threads.listItems(id,{order:'asc'}).asResponse()).json();
assert.equal(history.data.length,4);
assert.equal(history.data[0].attachments[0].id,file.id);
assert.equal(history.data.at(-1).content[0].text.trim(),expected);
const reused=await upload(owner,source,200);
assert.notEqual(reused.id,file.id);
await rpc(owner,'attachments.delete',{attachment_id:reused.id});
const chat=await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
const independent=chat.choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,answers:[expected,expected], peer:404,other_user:404,
  disabled:403,revoked:401,project_file_import:400,count_limit:409,source_deleted:404,reused_slot:true,items:4,independent_chat:independent,
  source_bytes:source.length,source_sha256:sha(source),wire:[{path:'/chatkit/threads/{thread_id}/items',
    method:'get',body:history}]}));
