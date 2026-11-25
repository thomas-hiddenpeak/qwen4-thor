// Drive a real document through the configured browser upload entry and two
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
const agent = p.previous ? {id:p.previous.workflow} : await project.beta.agents.create({model:p.model,reasoning:{effort:'none'},
  instructions:'Answer questions about the uploaded project record with just the requested integer.'});
const create = (user,enabled)=>project.beta.chatkit.sessions.create({user,workflow:{id:agent.id},
  rate_limits:{max_requests_per_1_minute:40},chatkit_configuration:{file_upload:{
    enabled,max_files:1,max_file_size:1}}});
const owner=await create('document-reader',true);
const source=readFileSync(new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json',import.meta.url));
const expected=String(JSON.parse(source).counts.required_inference_or_application_service);
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
async function request(grant,path,body,expectedStatus,stream=false) {
  const isForm=body instanceof FormData;
  const res=await fetch(p.base+path,{method:'POST',signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+grant.client_secret,...(isForm?{}:{'Content-Type':'application/json'})},
    body:isForm?body:JSON.stringify(body)});
  if(res.status!==expectedStatus) throw new Error(path+' '+res.status+' '+(await res.text()).slice(0,320));
  if (body?.type==='attachments.create' && res.ok)
    assert.equal(res.headers.get('cache-control'),'no-store');
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
async function recoverDocument() {
  const {file,thread,source_sha256:originalHash} = p.previous;
  assert.equal(originalHash,sha(source));
  const bytes = await (await project.files.content(file)).arrayBuffer();
  assert.equal(sha(Buffer.from(bytes)),originalHash);
  await assert.rejects(peer.files.retrieve(file),e=>e.status===404);
  const history = await project.beta.chatkit.threads.listItems(thread,{order:'asc'});
  assert.equal(history.data[0].attachments[0].id,file);
  await rpc(owner,'threads.add_user_message',{thread_id:thread,input:user(
    'Repeat the previously requested integer from our project scope decision.')},200,true);
  const after = await project.beta.chatkit.threads.listItems(thread,{order:'asc'});
  assert.equal(after.data.length,history.data.length+2);
  assert.equal(after.data.at(-1).content[0].text.trim(),expected);
  const chat = await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],
    chat_template_kwargs:{enable_thinking:false}});
  const independent = chat.choices[0].message.content.trim();
  assert.equal(independent,'42');
  return {sdk:version,workflow:agent.id,thread,file,source_sha256:originalHash,source_bytes:bytes.byteLength,
    answers:[expected],items:after.data.length,peer:404,independent_chat:independent,upload_mode:'restored'};
}

async function createDocument() {
  const sibling=await create('another-reader',true);
  const disabled=await create('view-only',false);
  await upload(disabled,source,403);
  let file, revokedDescriptor;
  const reservationChecks = {};
  if (p.two_phase) {
    const metadata = {name:'decision.json',size:source.length,mime_type:'application/json'};
    const reserve = (grant,status) => rpc(grant,'attachments.create',metadata,status);
    const discard = await reserve(sibling,200);
    await rpc(sibling,'attachments.delete',{attachment_id:discard.id});
    const reserveAgain = await reserve(sibling,200);
    assert.notEqual(discard.id,reserveAgain.id);
    const reserved = await reserve(owner,200);
    const descriptor = reserved.upload_descriptor;
    assert.equal(descriptor.method,'PUT');
    assert.equal(new URL(descriptor.url).origin,new URL(p.base).origin);
    assert.equal(descriptor.url,p.base+'/chatkit/attachments/'+owner.id+'/'+reserved.id);
    assert.match(descriptor.headers.Authorization,/^Bearer ckup_/);
    assert.equal(descriptor.headers['Content-Type'],metadata.mime_type);
    async function send(descriptor,bytes,status,headers=descriptor.headers) {
      const response = await fetch(descriptor.url,{method:descriptor.method,headers,
        body:bytes,redirect:'error',signal:AbortSignal.timeout(15000)});
      assert.equal(response.status,status);
      return response.json();
    }
    await assert.rejects(project.files.retrieve(reserved.id),e=>e.status===404);
    await rpc(owner,'threads.create',{input:user('Read the file.',[reserved.id])},404);
    await reserve(disabled,403);
    await reserve(owner,409);
    await send(discard.upload_descriptor,source,401);
    await send(descriptor,source,401,reserveAgain.upload_descriptor.headers);
    await send(descriptor,source,415,{...descriptor.headers,'Content-Type':'text/plain'});
    await send(descriptor,source.subarray(1),400);
    await send(descriptor,source,401,{...descriptor.headers,Authorization:'Bearer '+owner.client_secret});
    const uploadOnly={client_secret:descriptor.headers.Authorization.slice(7)};
    await rpc(uploadOnly,'threads.list',{},401);
    file=await send(descriptor,source,200);
    assert.equal(file.id,reserved.id);
    assert.ok(!file.upload_descriptor);
    await send(descriptor,source,401);
    revokedDescriptor = reserveAgain.upload_descriptor;
    Object.assign(reservationChecks,{pending_file:404,pending_model:404,replay:401,
      discarded:401,revoked_upload:401,short:400,wrong_mime:415,wrong_session:401,
      full_session_on_upload:401,upload_on_browser:401,origin_verified:true});
  } else {
    file=await upload(owner,source,200);
  }
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
  if (revokedDescriptor) {
    const reply = await fetch(revokedDescriptor.url,{method:'PUT',headers:revokedDescriptor.headers,
      body:source,redirect:'error',signal:AbortSignal.timeout(15000)});
    assert.equal(reply.status,401);
    await reply.arrayBuffer();
  }
  const events=await rpc(owner,'threads.create',{input:user(
    'From the attached scope decision, what integer is counts.required_inference_or_application_service? Answer only the integer.',[file.id])},200,true);
  const id=events.find(x=>x.type==='thread.created').thread.id;
  const linked=events.find(x=>x.type==='thread.item.done' && x.item.type==='user_message').item;
  assert.equal(linked.attachments[0].id,file.id);
  if (!p.retain) {
    await rpc(owner,'attachments.delete',{attachment_id:file.id});
    await assert.rejects(project.files.retrieve(file.id),e=>e.status===404);
  }
  await rpc(owner,'threads.add_user_message',{thread_id:id,input:user('Repeat the previously requested integer from our project scope decision.')},200,true);
  const historyResponse=await project.beta.chatkit.threads.listItems(id,{order:'asc'}).asResponse();
  const history=await historyResponse.json();
  assert.equal(history.data.length,4);
  assert.equal(history.data[0].attachments[0].id,file.id);
  assert.equal(history.data.at(-1).content[0].text.trim(),expected);
  if (!p.retain) {
    const reused=await upload(owner,source,200);
    assert.notEqual(reused.id,file.id);
    await rpc(owner,'attachments.delete',{attachment_id:reused.id});
  }
  const chat=await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
  const independent=chat.choices[0].message.content.trim();
  assert.equal(independent,'42');
  return {sdk:version,workflow:agent.id,thread:id,file:file.id,answers:[expected,expected], peer:404,other_user:404,
    disabled:403,revoked:401,project_file_import:400,count_limit:409,source_deleted:p.retain?null:404,reused_slot:!p.retain,items:4,independent_chat:independent,
    upload_mode:p.two_phase?'two_phase':'direct',reservation_checks:reservationChecks,
    source_bytes:source.length,source_sha256:sha(source),wire:[{path:'/chatkit/threads/{thread_id}/items',
      method:'get',status:historyResponse.status,body:history}]};
}

const result = p.previous ? await recoverDocument() : await createDocument();
process.stdout.write(JSON.stringify(result));
